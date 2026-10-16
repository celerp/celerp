# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The firm mapping template: the posting accounts a user chose finishing one client's
migration are offered preselected on their next client from the same source, marked
"From your previous clients", and are never applied without that client's own finish."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fasthtml.common import to_xml
from sqlalchemy import select
from starlette.requests import Request

from migration_support import auth, load_run, maker, real_client, real_engine, scan_upload  # noqa: F401
from test_helpers import make_authed_token, register_admin
from ui.i18n import t

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "manager_io"

# What the first client's accountant answers: none is the first account offered or the
# single source control, so a later preselection can only come from these answers.
FIRST_CLIENT = {
    "roles": {"cash_and_equivalents": "BANK-1", "default_deposit": "BANK-1", "inventory": "1010",
              "inventory_opening": "1010", "sales_revenue": "4010", "stock_gain": "4000",
              "general_expense": "6100", "stock_shrinkage": "6100"},
    "add_accounts": [{"role": "tax_input", "code": "1150", "name": "Input tax", "account_type": "asset"},
                     {"role": "cogs", "code": "5100", "name": "Cost of goods sold", "account_type": "cogs"}],
}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """Real adapters and sinks; a start is run to its stop by the test, not in the background."""
    from celerp.config import settings
    from celerp.services import migrations

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    scheduled: list = []
    monkeypatch.setattr(migrations, "schedule_run", scheduled.append)
    return scheduled


async def _migrate(client, token: str, fixture: str | Path, runner) -> str:
    """Upload a Manager file as a new client company and run it to the finish step."""
    from celerp.services import migrations

    path = fixture if isinstance(fixture, Path) else FIXTURES / f"{fixture}.manager"
    fixture = path.stem
    r = await scan_upload(client, path.read_bytes(), token=token, name=path.name)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    r = await client.post("/migrations/scan/decisions", headers=auth(token), json={
        "scan_token": scan_token, "mode": "full_history", "cutover_date": None, "mappings": {}, "prepared_by": None})
    assert r.status_code == 200, r.text
    r = await client.post("/migrations/start-from-scan", headers=auth(token),
                          json={"scan_token": scan_token, "company_name": f"{fixture.title()} Client {uuid.uuid4().hex[:4]}"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    await migrations.run_migration(uuid.UUID(run_id))
    return run_id


async def _roles(client, token: str, run_id: str) -> dict[str, dict]:
    r = await client.get(f"/migrations/{run_id}/posting-accounts", headers=auth(token))
    assert r.status_code == 200, r.text
    return {row["role"]: row for row in r.json()["roles"]}


async def _finish(client, token: str, run_id: str, body: dict):
    return await client.post(f"/migrations/{run_id}/finalize", headers=auth(token), json=body)


def _from_previous(roles: dict[str, dict]) -> dict[str, dict]:
    return {role: row["previous"] for role, row in roles.items() if row.get("previous")}


async def _first_client(client, token, runner) -> str:
    run_id = await _migrate(client, token, "basic", runner)
    roles = await _roles(client, token, run_id)
    assert _from_previous(roles) == {}  # a firm's first client has nothing to offer
    r = await _finish(client, token, run_id, FIRST_CLIENT)
    assert r.status_code == 200, r.text
    return run_id


async def _company_settings(engine, run_id) -> dict:
    from celerp.models.company import Company
    run = await load_run(engine, uuid.UUID(run_id))
    async with maker(engine)() as s:
        return dict((await s.get(Company, run.company_id)).settings or {})


async def test_firm_template_repeat_setup(real_client, real_engine, runner):
    """The next client from the same source offers every answer the first client got,
    existing accounts by code and added ones as the account to add; nothing is set on the
    company until its own finish, which takes the answers as offered."""
    from celerp.services.account_roles import role_map

    token = await register_admin(real_client)
    await _first_client(real_client, token, runner)

    run_id = await _migrate(real_client, token, "orders", runner)
    roles = await _roles(real_client, token, run_id)
    previous = _from_previous(roles)
    assert previous == {
        **{role: {"code": code, "add": False} for role, code in FIRST_CLIENT["roles"].items()},
        # Single source controls the first finish took as offered are answers too.
        "receivable": {"code": "1020", "add": False}, "payable": {"code": "2020", "add": False},
        "inventory_purchased": {"code": "1010", "add": False}, "tax_output": {"code": "2010", "add": False},
        "retained_earnings": {"code": "3010", "add": False},
        "fx_gain": {"code": "6010", "add": False}, "fx_loss": {"code": "6010", "add": False},
        "tax_input": {"code": "1150", "add": True}, "cogs": {"code": "5100", "add": True},
    }
    assert roles["tax_input"]["proposal"] == {"code": "1150", "name": "Input tax", "account_type": "asset"}
    assert roles["cogs"]["proposal"] == {"code": "5100", "name": "Cost of goods sold", "account_type": "cogs"}
    # Offered, not applied: the staged company has none of these roles yet.
    assert not set(role_map(await _company_settings(real_engine, run_id))) & set(previous)

    body = {"roles": {role: p["code"] for role, p in previous.items() if not p["add"]},
            "add_accounts": [{**roles[role]["proposal"], "role": role} for role, p in previous.items() if p["add"]]}
    r = await _finish(real_client, token, run_id, body)
    assert r.status_code == 200, r.text
    chosen = role_map(await _company_settings(real_engine, run_id))
    assert {role: chosen[role] for role in previous} == {role: p["code"] for role, p in previous.items()}


async def test_firm_template_skips_an_account_that_is_not_the_same(real_client, real_engine, runner):
    """A code the next client uses for a different account is not offered: 4010 is
    "Sales" in the first client's books and "Inventory - sales" in the next."""
    token = await register_admin(real_client)
    await _first_client(real_client, token, runner)

    run_id = await _migrate(real_client, token, "stock", runner)
    roles = await _roles(real_client, token, run_id)
    assert "4010" in {c["code"] for c in roles["sales_revenue"]["candidates"]}
    assert "previous" not in roles["sales_revenue"] or roles["sales_revenue"]["previous"] is None
    assert roles["general_expense"]["previous"] == {"code": "6100", "add": False}


async def test_firm_template_belongs_to_the_user_who_finished(real_client, real_engine, runner):
    """Another person on the same installation gets no suggestions from someone else's clients."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User

    token = await register_admin(real_client)
    await _first_client(real_client, token, runner)
    async with maker(real_engine)() as s:
        company_id = await s.scalar(select(UserCompany.company_id).limit(1))
        other = User(email="colleague@example.com", name="Colleague")
        s.add(other)
        await s.flush()
        s.add(UserCompany(user_id=other.id, company_id=company_id, role="owner"))
        await s.commit()
        other_token = await make_authed_token(s, str(other.id), str(company_id), "owner")

    run_id = await _migrate(real_client, other_token, "orders", runner)
    assert _from_previous(await _roles(real_client, other_token, run_id)) == {}
    run_id = await _migrate(real_client, token, "orders", runner)
    assert _from_previous(await _roles(real_client, token, run_id))["sales_revenue"] == {"code": "4010", "add": False}


async def test_firm_template_forget(real_client, real_engine, runner):
    """Forgetting the saved answers for a source removes every suggestion, and doing it
    again is not an error."""
    token = await register_admin(real_client)
    first = await _first_client(real_client, token, runner)
    r = await real_client.get(f"/migrations/{first}", headers=auth(token))
    assert r.json()["saved_answers"] is True

    for _ in range(2):
        r = await real_client.delete("/migrations/templates/manager_io", headers=auth(token))
        assert r.status_code == 204, r.text
    r = await real_client.get(f"/migrations/{first}", headers=auth(token))
    assert r.json()["saved_answers"] is False
    r = await real_client.delete("/migrations/templates/not-a-source", headers=auth(token))
    assert r.status_code == 404 and r.json() == {"detail": t("migration.err_unknown_source", "en")}
    run_id = await _migrate(real_client, token, "orders", runner)
    assert _from_previous(await _roles(real_client, token, run_id)) == {}


async def test_firm_template_not_saved_from_the_sample(real_client, real_engine, runner):
    """Trying the sample migration is not a client: its answers are never saved."""
    from celerp.importers.sample import SAMPLE_ARTIFACT

    token = await register_admin(real_client)
    run_id = await _migrate(real_client, token, Path(SAMPLE_ARTIFACT), runner)
    assert (await real_client.get(f"/migrations/{run_id}", headers=auth(token))).json()["is_sample"] is True
    roles = await _roles(real_client, token, run_id)
    body = {"roles": {k: v for k, v in FIRST_CLIENT["roles"].items() if k in roles},
            "add_accounts": FIRST_CLIENT["add_accounts"]}
    assert (await _finish(real_client, token, run_id, body)).status_code == 200
    assert (await real_client.get(f"/migrations/{run_id}", headers=auth(token))).json()["saved_answers"] is False
    run_id = await _migrate(real_client, token, "orders", runner)
    assert _from_previous(await _roles(real_client, token, run_id)) == {}


# ── Pages ────────────────────────────────────────────────────────────────────

async def _sources():
    return [{"key": "manager_io", "display_name": "Manager.io"}]


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": []})


async def test_firm_template_verify_page_marks_previous_answers(real_client, real_engine, runner, monkeypatch):
    """The finish step shows each previous answer selected in its picker, marked, and
    still changeable; an added account is offered as the account to add."""
    import ui.routes.migrations as pages
    from ui.components.posting_accounts import NEW_ACCOUNT

    token = await register_admin(real_client)
    await _first_client(real_client, token, runner)
    run_id = await _migrate(real_client, token, "orders", runner)
    roles = list((await _roles(real_client, token, run_id)).values())
    run = await load_run(real_engine, uuid.UUID(run_id))

    async def reconciliation(_token, _run_id):
        return run.reconciliation

    async def get_run(_token, _run_id):
        return {"id": run_id, "lock_date": None, "source_system": "manager_io"}

    async def posting_accounts(_token, _run_id):
        return {"roles": roles}

    monkeypatch.setattr(pages.api, "migration_reconciliation", reconciliation)
    monkeypatch.setattr(pages.api, "get_migration_run", get_run)
    monkeypatch.setattr(pages.api, "migration_posting_accounts", posting_accounts)
    page = to_xml(await pages._verify_page(_request(f"/migrations/{run_id}/verify"), run_id))
    marked = t("sync_accounts.from_previous", "en")
    # The finish step lists the roles the company needs; each with an earlier answer is marked.
    assert page.count(marked) == len([r for r in roles if r.get("previous") and r["required"]]) > 0
    sales = page[page.index('name="role.sales_revenue"'):]
    sales = sales[:sales.index("</select>")]
    assert '<option value="4010" selected>' in sales and '<option value="4000">' in sales
    cogs = page[page.index('name="role.cogs"'):]
    cogs = cogs[:cogs.index("</select>")]
    assert f'<option value="{NEW_ACCOUNT}" selected>' in cogs and "5100" in cogs


async def test_firm_template_complete_page_says_answers_are_saved(monkeypatch):
    """Finishing tells the user their answers were kept for the next client from that
    source and offers to forget them, in place."""
    import ui.routes.migrations as pages

    async def reconciliation(_token, _run_id):
        return {"rows": []}

    monkeypatch.setattr(pages.api, "migration_reconciliation", reconciliation)
    monkeypatch.setattr(pages.api, "migration_sources", _sources)
    run = {"id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "company_name": "Orders Client",
           "source_system": "manager_io", "saved_answers": True, "is_sample": False}
    page = to_xml(await pages._complete_page(_request(f"/migrations/{run['id']}/complete"), run))
    assert t("migration.answers_saved", "en", source="Manager.io") in page
    assert 'hx-post="/migrations/templates/manager_io/forget"' in page
    assert t("migration.forget_answers", "en") in page

    page = to_xml(await pages._complete_page(_request("/"), {**run, "saved_answers": False}))
    assert t("migration.forget_answers", "en") not in page


async def test_firm_template_forget_route_swaps_in_the_result(monkeypatch):
    """Forget answers in place: the notice is replaced by what happened, or the reason it could not."""
    import ui.routes.migrations as pages
    from ui.api_client import APIError

    calls = []
    monkeypatch.setattr(pages.api, "migration_sources", _sources)

    async def forget(_token, source):
        calls.append(source)

    monkeypatch.setattr(pages.api, "migration_forget_template", forget)
    html = to_xml(await pages._forget_answers(_request("/migrations/templates/manager_io/forget"), "manager_io"))
    assert calls == ["manager_io"] and t("migration.answers_forgotten", "en") in html

    async def refused(_token, source):
        raise APIError(503, "The server is busy.")

    monkeypatch.setattr(pages.api, "migration_forget_template", refused)
    html = to_xml(await pages._forget_answers(_request("/migrations/templates/manager_io/forget"), "manager_io"))
    assert "The server is busy." in html and t("migration.forget_answers", "en") in html
