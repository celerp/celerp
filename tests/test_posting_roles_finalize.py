# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Finishing a migration sets the posting accounts the company's workflows need.

The source books' own control account is offered when there is exactly one; several
leave the choice to the user. Where the chart has no suitable account, an account
can be added, and its proposed code never reuses one the chart already holds. The
choices and any added accounts are saved with the finish itself, all or nothing,
and finishing again neither adds accounts twice nor changes a choice made since.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from test_posting_roles_migration_sinks import _SOURCE_CHART, _import_chart, _staged_context


def _rows(readiness: list[dict]) -> dict[str, dict]:
    return {row["role"]: row for row in readiness}


async def _readiness(context) -> dict[str, dict]:
    from celerp.services.posting_readiness import readiness

    return _rows(await readiness(context.session, context.company_id))


async def _ready_run(context, monkeypatch):
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations

    async def _verified(session, run):
        return {"blockers": 0}

    monkeypatch.setattr(migrations, "_verification", _verified)
    run = await context.session.get(MigrationRun, context.run_id)
    run.status = migrations._S.READY_TO_FINALIZE.value
    await context.session.commit()
    return run


async def _finalize(context, monkeypatch, choices=None):
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations

    run = await context.session.get(MigrationRun, context.run_id)
    return await migrations.finalize(context.session, run, choices)


async def _company(context):
    from celerp.models.company import Company

    return await context.session.get(Company, context.company_id, populate_existing=True)


async def _codes(context) -> set[str]:
    from celerp_accounting.models import Account

    return set((await context.session.execute(select(Account.code).where(
        Account.company_id == context.company_id))).scalars())


# Every account the source chart cannot supply is added; the rest are chosen from it.
_CHOICES = {
    "roles": {"sales_revenue": "400", "cash_and_equivalents": "100", "inventory": "130",
              "inventory_opening": "130", "cogs": "500"},
    "add_accounts": [
        {"code": "6950", "name": "General expenses", "account_type": "expense", "role": "general_expense"},
        {"code": "1111", "name": "Default deposit account", "account_type": "asset", "role": "default_deposit"},
        {"code": "4300", "name": "Stock gains", "account_type": "revenue", "role": "stock_gain"},
        {"code": "6970", "name": "Stock shrinkage and write-offs", "account_type": "expense",
         "role": "stock_shrinkage"},
    ],
}


@pytest.mark.asyncio
async def test_a_single_source_control_is_offered_as_the_account(session):
    context = await _staged_context(session)
    await _import_chart(context)
    rows = await _readiness(context)
    assert {r: rows[r]["preselect"] for r in ("receivable", "payable", "retained_earnings", "inventory_purchased",
                                               "tax_input", "tax_output")} == {
        "receivable": "120", "payable": "210", "retained_earnings": "320", "inventory_purchased": "130",
        "tax_input": "150", "tax_output": "220"}
    assert rows["sales_revenue"]["preselect"] is None
    assert rows["sales_revenue"]["candidates"][0]["code"] == "400"
    # Taxes and stock appear in the source books, so their accounts are needed; landed
    # cost, foreign currency and fixed assets do not.
    assert {r for r, row in rows.items() if row["required"]} == {
        "receivable", "payable", "sales_revenue", "general_expense", "default_deposit", "retained_earnings",
        "cash_and_equivalents", "tax_input", "tax_output", "inventory", "inventory_purchased",
        "inventory_opening", "cogs", "stock_gain", "stock_shrinkage"}


@pytest.mark.asyncio
async def test_several_source_controls_leave_the_choice_to_the_user(session):
    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("ar2", "121", "Other debtors", "asset", "receivable")])
    row = (await _readiness(context))["receivable"]
    assert row["preselect"] is None
    assert [c["code"] for c in row["candidates"][:2]] == ["120", "121"]


@pytest.mark.asyncio
async def test_a_proposed_account_never_reuses_a_code_the_chart_holds(session):
    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("odd", "6950", "Suspense", "liability", None)])
    rows = await _readiness(context)
    assert rows["general_expense"]["proposal"] == {
        "code": "6950-1", "name": "General expenses", "account_type": "expense"}
    assert rows["fx_gain"]["proposal"]["code"] != rows["fx_loss"]["proposal"]["code"]


@pytest.mark.asyncio
async def test_finishing_is_refused_until_every_needed_account_is_set(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.migrations import MigrationError

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    before = await _codes(context)
    with pytest.raises(MigrationError) as exc:
        await _finalize(context, monkeypatch)
    assert exc.value.status_code == 409
    assert "Sales revenue" in exc.value.detail["message"] and "General expenses" in exc.value.detail["message"]
    company = await _company(context)
    assert company.is_migration_staged
    assert ROLES_KEY not in (company.settings or {})
    assert await _codes(context) == before


@pytest.mark.asyncio
async def test_finishing_sets_the_accounts_and_adds_the_new_ones_together(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY, SCOPES_KEY

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    await _finalize(context, monkeypatch, _CHOICES)
    company = await _company(context)
    assert not company.is_migration_staged
    roles = company.settings[ROLES_KEY]
    assert {r: roles[r] for r in ("receivable", "payable", "sales_revenue", "general_expense",
                                  "default_deposit", "inventory_purchased", "cogs")} == {
        "receivable": "120", "payable": "210", "sales_revenue": "400", "general_expense": "6950",
        "default_deposit": "1111", "inventory_purchased": "130", "cogs": "500"}
    assert company.settings[SCOPES_KEY]["receivable"] == ["120"]
    assert {"6950", "1111", "4300", "6970"} <= await _codes(context)
    # Roles no workflow needs yet stay unset until first use.
    assert "fx_gain" not in roles and "landed_freight" not in roles


@pytest.mark.asyncio
async def test_every_source_control_joins_its_roles_history(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY, SCOPES_KEY

    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("ar2", "121", "Other debtors", "asset", "receivable")])
    await _ready_run(context, monkeypatch)
    await _finalize(context, monkeypatch, {**_CHOICES, "roles": {**_CHOICES["roles"], "receivable": "121"}})
    settings = (await _company(context)).settings
    assert settings[ROLES_KEY]["receivable"] == "121"
    assert sorted(settings[SCOPES_KEY]["receivable"]) == ["120", "121"]


@pytest.mark.asyncio
async def test_an_unsuitable_choice_is_refused_with_the_reason(session, monkeypatch):
    from celerp.services.migrations import MigrationError

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    with pytest.raises(MigrationError) as exc:
        await _finalize(context, monkeypatch, {**_CHOICES, "roles": {**_CHOICES["roles"], "sales_revenue": "210"}})
    assert exc.value.status_code == 409
    assert "Sales revenue is set to account 210, of type liability; it must be of type revenue." in exc.value.detail["message"]
    assert (await _company(context)).is_migration_staged


@pytest.mark.asyncio
async def test_finishing_again_adds_nothing_and_keeps_the_choices(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.migrations import MigrationError
    from celerp_accounting.models import Account

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    await _finalize(context, monkeypatch, _CHOICES)
    roles = dict((await _company(context)).settings[ROLES_KEY])
    count = await session.scalar(select(func.count()).select_from(Account).where(
        Account.company_id == context.company_id))
    with pytest.raises(MigrationError) as exc:
        await _finalize(context, monkeypatch, {**_CHOICES, "roles": {**_CHOICES["roles"], "sales_revenue": "500"}})
    assert exc.value.status_code == 409
    assert (await _company(context)).settings[ROLES_KEY] == roles
    assert await session.scalar(select(func.count()).select_from(Account).where(
        Account.company_id == context.company_id)) == count


@pytest.mark.asyncio
async def test_an_account_set_before_finishing_is_kept(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.account_roles import set_role
    from celerp.services.migrations import MigrationError

    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("other", "410", "Other sales", "revenue", None)])
    await set_role(session, context.company_id, "sales_revenue", "410")
    row = (await _readiness(context))["sales_revenue"]
    assert (row["current"], row["current_account"]["name"]) == ("410", "Other sales")
    await _ready_run(context, monkeypatch)
    with pytest.raises(MigrationError) as exc:
        await _finalize(context, monkeypatch, _CHOICES)
    assert "Sales revenue is already set to account 410" in exc.value.detail["message"]
    choices = {**_CHOICES, "roles": {k: v for k, v in _CHOICES["roles"].items() if k != "sales_revenue"}}
    await _finalize(context, monkeypatch, choices)
    assert (await _company(context)).settings[ROLES_KEY]["sales_revenue"] == "410"


@pytest.mark.asyncio
async def test_startup_after_finishing_leaves_the_migrated_accounts_alone(session, monkeypatch):
    from celerp.accounting_roles import ROLES_KEY
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    await _finalize(context, monkeypatch, _CHOICES)
    before = dict((await _company(context)).settings[ROLES_KEY])
    await backfill_chart_of_accounts_hook(session=session)
    await session.commit()
    assert (await _company(context)).settings[ROLES_KEY] == before


@pytest.mark.asyncio
async def test_a_migrated_draft_records_the_opening_account_chosen_at_finish_when_made_available(
        session, monkeypatch):
    import uuid
    from decimal import Decimal

    from celerp.events.engine import emit_event
    from celerp.importers.schema import CIFItem
    from celerp.models.projections import Projection
    from test_migration_sinks import _PROVENANCE
    from test_money_stock_and_contact_invariants import _account_net
    from test_posting_roles_migration_sinks import _via

    context = await _staged_context(session)
    await _import_chart(context)
    item = CIFItem(**_PROVENANCE, source_type="InventoryItem", source_external_id="draft-1", sku="DFT-1",
                   name="Draft", status="draft", total_cost=Decimal("40"))
    result = await _via(context, "items", [item])
    assert result.errors == []
    draft = result.mappings[0].target_entity_id
    row = await session.get(Projection, (context.company_id, draft))
    assert "inventory_account_code" not in row.state
    await _ready_run(context, monkeypatch)
    roles = {k: v for k, v in _CHOICES["roles"].items() if k != "inventory_opening"}
    await _finalize(context, monkeypatch, {"roles": roles, "add_accounts": [
        *_CHOICES["add_accounts"],
        {"code": "1135", "name": "Opening stock", "account_type": "asset", "role": "inventory_opening"}]})
    await emit_event(session, company_id=context.company_id, entity_id=draft, entity_type="item",
                     event_type="item.status.set", data={"new_status": "available"}, actor_id=None,
                     location_id=None, source="api", idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    row = await session.get(Projection, (context.company_id, draft), populate_existing=True)
    assert row.state["inventory_account_code"] == "1135"
    assert await _account_net(session, context.company_id, "1135") == 40.0


@pytest.mark.asyncio
async def test_an_account_the_source_gave_no_code_is_offered_by_its_name(session):
    """The code Celerp gives such an account is internal: finishing lists the account,
    and shows it once set, by its name."""
    from celerp.accounting_roles import account_label
    from celerp.services.account_roles import set_role

    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("charges", None, "Bank charges", "expense", None)])
    [code] = await _codes(context) - {code for _, code, *_ in _SOURCE_CHART}
    [offered] = [c for c in (await _readiness(context))["general_expense"]["candidates"] if c["code"] == code]
    assert account_label(offered) == "Bank charges"

    await set_role(session, context.company_id, "general_expense", code)
    row = (await _readiness(context))["general_expense"]
    assert account_label(row["current_account"]) == "Bank charges"


@pytest.mark.asyncio
async def test_an_account_the_source_coded_like_a_generated_one_is_offered_with_its_code(session):
    """The source's own code is shown, however it is spelled; only a code Celerp made up
    is hidden."""
    from celerp.accounting_roles import account_label

    context = await _staged_context(session)
    await _import_chart(context, [*_SOURCE_CHART, ("charges", "Mdeadbeef", "Bank charges", "expense", None)])
    [offered] = [c for c in (await _readiness(context))["general_expense"]["candidates"]
                 if c["code"] == "Mdeadbeef"]
    assert account_label(offered) == "Mdeadbeef Bank charges"
