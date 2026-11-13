# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Item and category income accounts, and the revenue lines a sale posts from them.

Initial conditions per test: a fresh company with the seeded chart (4000 Revenue is a
header, 4100 Sales Revenue, 4200 Service Revenue and 4300 Other Income are revenue
accounts, 1120 is the receivable). Items are service items made through POST /items;
invoices and credit notes are made and finalized through /docs. An item's own income
account wins over its category's; an item with neither sells on the sales revenue
account, as every sale did before. A credit note line linked to an invoice line reverses
the account that invoice line posted, read from the invoice's entry, never from the
item as it is now.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from income_books import _account, _category, _draft, _issued, _item, _line, _set_category_account
from test_credit_note_settlement_owned import _post
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio


async def _patch_item(client, auth, item: str, **changes):
    return await client.patch(f"/items/{item}", headers=auth["headers"], json={
        "fields_changed": {k: {"new": v} for k, v in changes.items()}})


async def _line_ids(client, auth, doc: str) -> list[str]:
    r = await client.get(f"/docs/{doc}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return [li["line_id"] for li in r.json()["line_items"]]


async def _revenue(session, auth) -> dict[str, float]:
    """Credit less debit on each revenue account over every posted entry."""
    return {code: -await _net(session, auth, code, prefix="je:") for code in ("4100", "4200", "4300", "4250")}


async def _finalize_entry(session, auth, doc: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": f"je:auto:{doc}:fin"})
    assert row is not None and row.state.get("status") == "posted"
    return row.state


async def test_income_account_validated_on_item_and_category_save(client, session, auth):
    await _account(client, auth, "1190", account_type="asset", parent=None)
    refused = {"9999": "not in the chart", "1190": "revenue", "4000": "header"}

    for code, why in refused.items():
        r = await client.post("/items", headers=auth["headers"], json={
            "name": "Workshop", "sell_by": "piece", "inventory_type": "service", "income_account": code})
        assert r.status_code == 422, (code, r.text)
        assert code in r.text and why in r.text.lower(), r.text
    item = await _item(client, auth, "Workshop", income_account="4200")
    session.expire_all()
    assert (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item})).state["income_account"] == "4200"

    for code in refused:
        r = await _patch_item(client, auth, item, income_account=code)
        assert r.status_code == 422, (code, r.text)
        assert code in r.text
    assert (await _patch_item(client, auth, item, income_account="4300")).status_code == 200
    assert (await _patch_item(client, auth, item, income_account="")).status_code == 200
    session.expire_all()
    assert "income_account" not in (await session.get(
        Projection, {"company_id": auth["company_id"], "entity_id": item})).state

    key = await _category(client, auth, "Workshops")
    for code in refused:
        r = await _set_category_account(client, auth, key, code)
        assert r.status_code == 422, (code, r.text)
        assert code in r.text
    assert (await _set_category_account(client, auth, "no_such_category", "4200")).status_code == 404
    for _ in range(2):  # set-to-state: a repeat is a no-op
        r = await _set_category_account(client, auth, key, "4200")
        assert r.status_code == 200, r.text
    schemas = (await client.get("/companies/me/category-income-accounts", headers=auth["headers"])).json()
    assert schemas == {key: "4200"}
    assert (await _set_category_account(client, auth, key, None)).status_code == 200
    assert (await client.get("/companies/me/category-income-accounts", headers=auth["headers"])).json() == {}


async def test_finalize_refuses_missing_or_non_revenue_income_account(client, session, auth):
    await _account(client, auth, "4250")
    await _account(client, auth, "4260")
    key = await _category(client, auth, "Workshops")
    own = await _item(client, auth, "Strategy day", income_account="4250")
    by_category = await _item(client, auth, "Coaching hour", category=key)
    assert (await _set_category_account(client, auth, key, "4260")).status_code == 200

    # Both accounts change after the items were saved: one stops being revenue, one is archived.
    r = await client.patch("/accounting/accounts/4250", headers=auth["headers"], json={"account_type": "expense"})
    assert r.status_code == 200, r.text
    r = await client.patch("/accounting/accounts/4260", headers=auth["headers"], json={"is_active": False})
    assert r.status_code == 200, r.text

    for item, name, code in ((own, "Strategy day", "4250"), (by_category, "Coaching hour", "4260")):
        doc = await _draft(client, auth, "invoice", [_line(item, name, 100.0)])
        before = await _revenue(session, auth)
        r = await _post(client, auth, f"/docs/{doc}/finalize")
        assert r.status_code == 422, r.text
        assert name in r.text and code in r.text, r.text
        assert await _revenue(session, auth) == before
        session.expire_all()
        state = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc})).state
        assert state["status"] == "draft"


async def test_account_delete_refused_while_items_reference_it(client, session, auth):
    from celerp_accounting.chart_rules import remove_account
    from fastapi import HTTPException

    await _account(client, auth, "4250")
    key = await _category(client, auth, "Workshops")
    await _item(client, auth, "Strategy day", income_account="4250")
    assert (await _set_category_account(client, auth, key, "4250")).status_code == 200

    with pytest.raises(HTTPException) as refused:
        await remove_account(session, auth["company_id"], "4250")
    assert refused.value.status_code == 409
    assert "Strategy day" in str(refused.value.detail) and "Workshops" in str(refused.value.detail)

    await session.rollback()
    await _account(client, auth, "4270")
    await remove_account(session, auth["company_id"], "4270")


async def _mixed_invoice(client, auth) -> tuple[str, str, str, list[str]]:
    """An invoice selling a 4200 item for 100.00, a 4300 item for 33.33 and a line with no item for 16.67."""
    a = await _item(client, auth, "Workshop", income_account="4200")
    b = await _item(client, auth, "Room hire", income_account="4300")
    doc = await _issued(client, auth, "invoice", [_line(a, "Workshop", 100.0), _line(b, "Room hire", 33.33),
                                                   _line(None, "Notes", 16.67)])
    return doc, a, b, await _line_ids(client, auth, doc)


async def test_mixed_income_accounts_split_exact(client, session, auth):
    doc, _, _, line_ids = await _mixed_invoice(client, auth)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 100.0, "4300": 33.33, "4250": 0.0}
    entry = await _finalize_entry(session, auth, doc)
    credits = [e for e in entry["entries"] if e["account"] in ("4100", "4200", "4300")]
    assert len(credits) == 3 and round(sum(e["credit"] for e in credits), 2) == 150.0
    session.expire_all()
    created = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == f"je:auto:{doc}:fin",
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().one()
    allocations = created.metadata_["line_allocations"]
    assert {a["line_id"]: a["account"] for a in allocations.values()} == dict(zip(line_ids, ("4200", "4300", "4100")))


async def test_credit_note_linked_line_reverses_posted_account(client, session, auth):
    doc, _, b, line_ids = await _mixed_invoice(client, auth)
    await _issued(client, auth, "credit_note", [_line(b, "Room hire", 33.33, source_line_id=line_ids[1])],
                  original_doc_id=doc)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 100.0, "4300": 0.0, "4250": 0.0}


async def test_credit_note_unlinked_keeps_pro_rata(client, session, auth):
    doc, _, _, _ = await _mixed_invoice(client, auth)
    await _issued(client, auth, "credit_note", [_line(None, "Goodwill", 30.0)], original_doc_id=doc)
    # 30.00 over 100.00 : 33.33 : 16.67 of 150.00 = 20.00 : 6.67 : 3.33
    assert await _revenue(session, auth) == {"4100": 13.34, "4200": 80.0, "4300": 26.66, "4250": 0.0}


async def test_credit_note_crediting_no_invoice_takes_each_line_back_on_its_income_account(client, session, auth):
    """A credit note with no invoice to follow takes each line back on the account an invoice
    line of the same item credits, so a credited item's income account goes back down instead
    of sales revenue going negative."""
    doc, a, b, _ = await _mixed_invoice(client, auth)
    await _issued(client, auth, "credit_note", [_line(a, "Workshop", 40.0), _line(b, "Room hire", 13.33),
                                                _line(None, "Notes", 6.67)])
    assert await _revenue(session, auth) == {"4100": 10.0, "4200": 60.0, "4300": 20.0, "4250": 0.0}


async def test_reversal_reads_posted_not_current_setting(client, session, auth):
    doc, a, _, line_ids = await _mixed_invoice(client, auth)
    assert (await _patch_item(client, auth, a, income_account="4300")).status_code == 200
    await _issued(client, auth, "credit_note", [_line(a, "Workshop", 40.0, source_line_id=line_ids[0])],
                  original_doc_id=doc)
    assert await _revenue(session, auth) == {"4100": 16.67, "4200": 60.0, "4300": 33.33, "4250": 0.0}


_CHART = {"items": [
    {"code": "4000", "name": "Revenue", "account_type": "revenue", "is_active": True, "parent_code": None},
    {"code": "4100", "name": "Sales Revenue", "account_type": "revenue", "is_active": True, "parent_code": "4000"},
    {"code": "4300", "name": "Other Income", "account_type": "revenue", "is_active": True, "parent_code": "4000"},
    {"code": "4900", "name": "Old Income", "account_type": "revenue", "is_active": False, "parent_code": "4000"},
    {"code": "1120", "name": "Accounts Receivable", "account_type": "asset", "is_active": True, "parent_code": None},
]}
_ACCOUNTING = {"name": "Test Corp", "settings": {"modules": ["celerp-accounting"]}}


@pytest.fixture
async def ui_client():
    from httpx import ASGITransport, AsyncClient

    from test_helpers import make_test_token
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _ui_api(*, item: dict | None = None, mapping: dict | None = None, **more):
    """Patches for the API calls the income account cell makes."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    calls = {"get_chart": _CHART, "get_category_income_accounts": mapping or {}, "get_company": _ACCOUNTING,
             "get_item": item or {}}
    stack = ExitStack()
    for name, value in calls.items():
        stack.enter_context(patch(f"ui.api_client.{name}", new=AsyncMock(return_value=value)))
    for name, mock in more.items():
        stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
    return stack


async def test_income_account_cell_shows_own_inherited_or_none(ui_client):
    url = "/settings/accounting/income-account/display"
    with _ui_api(item={"category": "Gems"}, mapping={"Gems": "4300"}):
        inherited = (await ui_client.get(url, params={"kind": "item", "key": "it-1"})).text
    assert "4300 Other Income, from its category" in inherited
    assert 'hx-get="/settings/accounting/income-account/edit?kind=item&amp;key=it-1"' in inherited

    with _ui_api(item={"category": "Gems", "income_account": "4100"}, mapping={"Gems": "4300"}):
        own = (await ui_client.get(url, params={"kind": "item", "key": "it-1"})).text
    assert "4100 Sales Revenue" in own and "from its category" not in own

    with _ui_api():
        none = (await ui_client.get(url, params={"kind": "category", "key": "Gems"})).text
    assert ">--<" in none

    with _ui_api():
        assert (await ui_client.get(url, params={"kind": "invoice", "key": "x"})).status_code == 404
        assert (await ui_client.get("/settings/accounting/income-account/edit",
                                    params={"kind": "invoice", "key": "x"})).status_code == 404


async def test_income_account_picker_offers_only_usable_revenue_accounts(ui_client):
    with _ui_api(mapping={"Gems": "4300"}):
        r = await ui_client.get("/settings/accounting/income-account/edit", params={"kind": "category", "key": "Gems"})
    body = r.text
    assert "combobox" in body and "celerpCellKey" in body
    assert 'hx-patch="/settings/accounting/income-account?kind=category&amp;key=Gems"' in body
    assert "4100 Sales Revenue" in body and "4300 Other Income" in body
    assert "4000 Revenue" not in body and "4900" not in body and "1120" not in body


async def test_income_account_save_shows_cell_or_keeps_picker_with_reason(ui_client):
    from unittest.mock import AsyncMock

    from ui.api_client import APIError

    put = AsyncMock(return_value={})
    with _ui_api(mapping={"Gems": "4100"}, set_category_income_account=put):
        r = await ui_client.patch("/settings/accounting/income-account", params={"kind": "category", "key": "Gems"},
                                  data={"value": "4100"})
    assert put.await_args.args[1:] == ("Gems", "4100")
    assert "4100 Sales Revenue" in r.text and "combobox" not in r.text and "hx-redirect" not in r.headers

    cleared = AsyncMock(return_value={})
    with _ui_api(set_category_income_account=cleared):
        await ui_client.patch("/settings/accounting/income-account", params={"kind": "category", "key": "Gems"},
                              data={"value": ""})
    assert cleared.await_args.args[1:] == ("Gems", None)

    refused = AsyncMock(side_effect=APIError(422, "Account 1120 is not a revenue account."))
    with _ui_api(item={"category": None}, patch_item=refused):
        r = await ui_client.patch("/settings/accounting/income-account", params={"kind": "item", "key": "it-1"},
                                  data={"value": "1120"})
    assert refused.await_args.args[1:] == ("it-1", {"income_account": "1120"})
    assert r.status_code == 200
    assert "Account 1120 is not a revenue account." in r.text and "combobox" in r.text and "cell--error" in r.text
