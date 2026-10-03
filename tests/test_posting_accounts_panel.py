# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings > Accounting > Posting accounts.

Each posting role shows its current account and whether that account can take new
postings. Changing one is checked by the API itself: an account from another company,
an inactive one, or one of the wrong type is refused with the reason, whatever the
screen offered. The earlier account stays with the role for the balances already on it.
Older stock whose history proves no inventory account is listed lot by lot, and its
account can only be one that has held inventory and holds the lot's value.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


async def _account(client, auth, code: str, account_type: str, parent_code: str) -> None:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent_code})
    assert r.status_code == 200, r.text


async def _panel(client, auth) -> dict:
    r = await client.get("/accounting/posting-accounts", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _row(panel: dict, role: str) -> dict:
    return next(row for row in panel["roles"] if row["role"] == role)


async def _put(client, auth, role: str, code: str):
    return await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": code})


async def _set_settings(session, company_id, **changes) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    company.settings = {**company.settings, **changes}
    await session.commit()


async def _roles(session, company_id) -> dict:
    from celerp.accounting_roles import ROLES_KEY
    from celerp.models.company import Company

    company = await session.get(Company, company_id, populate_existing=True)
    return dict(company.settings[ROLES_KEY])


@pytest.mark.asyncio
async def test_every_role_shows_its_account_and_that_it_is_ready(client, auth):
    from celerp.accounting_roles import AccountRole

    panel = await _panel(client, auth)
    assert [row["role"] for row in panel["roles"]] == [r.value for r in AccountRole]
    receivable = _row(panel, "receivable")
    assert receivable["label"] == "Accounts receivable"
    assert (receivable["code"], receivable["status"]) == ("1120", "ready")
    assert receivable["name"]
    assert receivable["earlier"] == []
    assert all(row["status"] == "ready" for row in panel["roles"])


@pytest.mark.asyncio
async def test_a_missing_inactive_or_wrong_type_account_is_shown_as_such(session, client, auth):
    from celerp.accounting_roles import ROLES_KEY
    from celerp_accounting.models import Account

    cid = auth["company_id"]
    await _account(client, auth, "4990", "revenue", "4000")
    await session.execute(update(Account).where(Account.company_id == cid, Account.code == "4990")
                          .values(is_active=False))
    roles = await _roles(session, cid)
    roles.pop("general_expense")
    await _set_settings(session, cid, **{ROLES_KEY: {**roles, "sales_revenue": "4990", "cogs": "1120",
                                                     "payable": "9999"}})
    panel = await _panel(client, auth)
    assert _row(panel, "general_expense")["status"] == "missing"
    assert _row(panel, "general_expense")["code"] is None
    assert _row(panel, "payable")["status"] == "missing"
    assert _row(panel, "sales_revenue")["status"] == "inactive"
    assert _row(panel, "cogs")["status"] == "wrong_type"
    assert "it must be" in _row(panel, "cogs")["problem"]


@pytest.mark.asyncio
async def test_changing_an_account_keeps_the_earlier_one_for_existing_balances(session, client, auth):
    await _account(client, auth, "1125", "asset", "1100")
    r = await _put(client, auth, "receivable", "1125")
    assert r.status_code == 200, r.text
    row = _row(r.json(), "receivable")
    assert (row["code"], row["status"], row["earlier"]) == ("1125", "ready", ["1120"])
    assert (await _roles(session, auth["company_id"]))["receivable"] == "1125"


@pytest.mark.asyncio
async def test_a_wrong_type_inactive_or_other_company_account_is_refused(session, client, auth):
    from celerp.models.company import Company
    from celerp_accounting.models import Account
    from test_helpers import provision_company_books

    cid = auth["company_id"]
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other", slug=f"other-{other.hex[:8]}", settings={"currency": "USD"}))
    await session.flush()
    await provision_company_books(session, other)
    session.add(Account(id=uuid.uuid4(), company_id=other, code="1188", name="Elsewhere",
                        account_type="asset", is_active=True))
    await _account(client, auth, "1189", "asset", "1100")
    await session.execute(update(Account).where(Account.company_id == cid, Account.code == "1189")
                          .values(is_active=False))
    await session.commit()
    before = await _roles(session, cid)

    for code, reason in (("2110", "a liability account; it must be asset"), ("1189", "which is inactive"),
                         ("1188", "which is not in the chart of accounts")):
        r = await _put(client, auth, "receivable", code)
        assert r.status_code == 422, (code, r.text)
        assert reason in r.json()["detail"]
    r = await _put(client, auth, "not_a_role", "1120")
    assert r.status_code == 422
    assert await _roles(session, cid) == before


@pytest.mark.asyncio
async def test_only_users_who_manage_accounting_change_posting_accounts(session, client, auth):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from test_helpers import make_authed_token

    cid = auth["company_id"]
    viewer = uuid.uuid4()
    session.add(User(id=viewer, email=f"viewer-{viewer.hex[:8]}@test.co", name="Viewer", auth_hash="x",
                     is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=viewer, company_id=cid, role="viewer", is_active=True))
    await session.commit()
    headers = {"Authorization": f"Bearer {await make_authed_token(session, str(viewer), str(cid), 'viewer')}"}
    r = await client.put("/accounting/posting-accounts/receivable", headers=headers, json={"code": "1120"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_older_stock_lists_each_lot_and_the_accounts_that_held_inventory(session, client, auth):
    from test_posting_roles_lots import _forget_origin, _lot

    await _account(client, auth, "1135", "asset", "1100")
    assert (await _put(client, auth, "inventory_purchased", "1135")).status_code == 200
    assert (await _panel(client, auth))["older_stock"]["lots"] == []
    lot = await _lot(client, auth, 30.0, sku="OLD-1")
    await _forget_origin(session, auth, lot)
    older = (await _panel(client, auth))["older_stock"]
    assert older["lots"] == [{"item_id": lot, "sku": "OLD-1", "name": "Lot", "value": 30.0}]
    assert [c["code"] for c in older["candidates"]] == ["1130-OB", "1130-P", "1135"]

    r = await client.put(f"/accounting/posting-accounts/older-stock/{lot}", headers=auth["headers"],
                         json={"code": "1120"})
    assert r.status_code == 422
    assert "has never held inventory" in r.json()["detail"]
    r = await client.put("/accounting/posting-accounts/older-stock/item:missing", headers=auth["headers"],
                         json={"code": "1130-OB"})
    assert r.status_code == 404


# ── Screen ───────────────────────────────────────────────────────────────────

_PANEL = {
    "roles": [
        {"role": "receivable", "label": "Accounts receivable", "group": "core", "required": True,
         "code": "1120", "name": "Accounts Receivable", "status": "ready", "problem": None, "earlier": ["1121"],
         "candidates": [{"code": "1120", "name": "Accounts Receivable", "account_type": "asset"}]},
        {"role": "general_expense", "label": "General expenses", "group": "core", "required": True,
         "code": None, "name": None, "status": "missing", "problem": "No account is set for general expenses.",
         "earlier": [], "candidates": [{"code": f"6{i:03}", "name": f"Expense {i}", "account_type": "expense"}
                                       for i in range(12)]},
        {"role": "fx_gain", "label": "Exchange gain", "group": "fx", "required": False,
         "code": None, "name": None, "status": "unused", "problem": None,
         "earlier": [], "candidates": []},
    ],
    "older_stock": {"lots": [{"item_id": "item:old-1", "sku": "OLD-1", "name": "Older lot", "value": 30.0}],
                    "candidates": [{"code": "1130-OB", "name": "Inventory opening", "account_type": "asset"}]},
}


@pytest.fixture
async def ui_client():
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _cookies() -> dict:
    from test_helpers import make_test_token

    return {"celerp_token": make_test_token(role="owner")}


@pytest.mark.asyncio
async def test_the_panel_lists_each_role_with_its_account_and_status(ui_client):
    company = {"name": "Test Corp", "current_role": "owner", "settings": {}}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
         patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)):
        r = await ui_client.get("/settings/accounting?tab=posting-accounts", cookies=_cookies())
    assert r.status_code == 200
    body = r.content.decode()
    assert 'href="/settings/accounting?tab=posting-accounts"' in body
    assert "Changing this affects new documents or new balances. Existing receivables, payables, " \
           "inventory lots, and reversals continue in the accounts where they were originally posted." in body
    panel = body[body.index('class="section-title"'):body.index("</table>")]
    assert "undo" not in panel.lower() and "revert" not in panel.lower()
    assert "1120 Accounts Receivable" in body and ">Ready<" in body and ">Missing<" in body
    assert ">Not used yet<" in body
    assert ">1121<" in body
    assert ">--<" in body
    assert 'hx-get="/settings/accounting/posting-accounts/general_expense/edit"' in body
    assert "Older stock OLD-1 Older lot" in body and "Valued at 30.0." in body
    assert 'hx-get="/settings/accounting/posting-accounts/older-stock:item:old-1/edit"' in body


@pytest.mark.asyncio
async def test_editing_offers_a_searchable_picker_and_escape_cancels(ui_client):
    with patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)):
        r = await ui_client.get("/settings/accounting/posting-accounts/general_expense/edit", cookies=_cookies())
    assert r.status_code == 200
    body = r.content.decode()
    assert "combobox" in body
    assert 'hx-patch="/settings/accounting/posting-accounts/general_expense"' in body
    assert "Escape" in body and "/settings/accounting/posting-accounts/general_expense/display" in body


@pytest.mark.asyncio
async def test_saving_updates_the_row_in_place_and_shows_a_refusal(ui_client):
    from ui.api_client import APIError

    saved = {**_PANEL, "roles": [{**_PANEL["roles"][1], "code": "6001", "name": "Expense 1", "status": "ready",
                                  "problem": None}]}
    put = AsyncMock(return_value=saved)
    with patch("ui.api_client.set_posting_account", new=put):
        r = await ui_client.patch("/settings/accounting/posting-accounts/general_expense", cookies=_cookies(),
                                  data={"value": "6001"})
    assert r.status_code == 200
    assert "hx-redirect" not in r.headers
    assert put.await_args.args[1:] == ("general_expense", "6001")
    assert "6001 Expense 1" in r.content.decode() and ">Ready<" in r.content.decode()

    refused = AsyncMock(side_effect=APIError(422, "General expenses is set to account 1120, a asset account; "
                                                  "it must be expense."))
    with patch("ui.api_client.set_posting_account", new=refused), \
         patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)):
        r = await ui_client.patch("/settings/accounting/posting-accounts/general_expense", cookies=_cookies(),
                                  data={"value": "1120"})
    assert r.status_code == 200
    assert "it must be expense." in r.content.decode()
    assert "<tr" in r.content.decode()


@pytest.mark.asyncio
async def test_choosing_an_older_lots_account_shows_it_recorded_or_the_refusal(ui_client):
    from ui.api_client import APIError

    key = "older-stock:item:old-1"
    with patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)):
        edit = await ui_client.get(f"/settings/accounting/posting-accounts/{key}/edit", cookies=_cookies())
    assert edit.status_code == 200
    assert f'hx-patch="/settings/accounting/posting-accounts/{key}"' in edit.content.decode()

    put = AsyncMock(return_value={**_PANEL, "older_stock": {**_PANEL["older_stock"], "lots": []}})
    with patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)), \
         patch("ui.api_client.set_older_stock_account", new=put):
        r = await ui_client.patch(f"/settings/accounting/posting-accounts/{key}", cookies=_cookies(),
                                  data={"value": "1130-OB"})
    body = r.content.decode()
    assert put.await_args.args[1:] == ("item:old-1", "1130-OB")
    assert "1130-OB Inventory opening" in body and "Recorded." in body
    assert "hx-get" not in body  # the choice is final

    refused = AsyncMock(side_effect=APIError(422, "Account 1130-P does not hold this stock's value of 30.00"))
    with patch("ui.api_client.get_posting_accounts", new=AsyncMock(return_value=_PANEL)), \
         patch("ui.api_client.set_older_stock_account", new=refused):
        r = await ui_client.patch(f"/settings/accounting/posting-accounts/{key}", cookies=_cookies(),
                                  data={"value": "1130-P"})
    body = r.content.decode()
    assert "does not hold this stock" in body and f"/settings/accounting/posting-accounts/{key}/edit" in body
