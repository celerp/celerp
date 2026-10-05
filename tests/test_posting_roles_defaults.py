# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Defaults that pick an account come from the company's posting accounts, never a number.

An online payment with no deposit account chosen lands on the default deposit account.
A new write-off line starts on the shrinkage and write-off account. A bill or purchase
order line with no account of its own is left blank, so it posts to the account its
kind of line is set to, rather than to a fixed account number the chart may not have.
"""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml
from fastapi import HTTPException

from celerp.services.account_roles import set_role
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)


async def _account(client, auth, code: str, account_type: str, parent_code: str) -> None:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent_code})
    assert r.status_code == 200, r.text


async def _unmap(session, company_id, role: str) -> None:
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    roles = dict((company.settings or {}).get(ROLES_KEY) or {})
    roles.pop(role, None)
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.commit()


@pytest.mark.asyncio
async def test_an_online_payment_with_no_deposit_account_chosen_lands_on_the_default_deposit_account(
        session, client, auth):
    from celerp_docs.routes_payments import deposit_account

    cid = auth["company_id"]
    assert await deposit_account(session, cid) == "1111"
    await _account(client, auth, "1112", "asset", "1110")
    await set_role(session, cid, "default_deposit", "1112")
    await session.commit()
    assert await deposit_account(session, cid) == "1112"


@pytest.mark.asyncio
async def test_an_online_payment_is_refused_when_no_default_deposit_account_is_set(session, auth):
    from celerp_docs.routes_payments import deposit_account

    await _unmap(session, auth["company_id"], "default_deposit")
    with pytest.raises(HTTPException) as exc:
        await deposit_account(session, auth["company_id"])
    assert exc.value.status_code == 409
    assert "Default deposit account has no account set" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_a_channels_own_deposit_account_still_wins(session, auth):
    from celerp.services.company_lock import locked_company
    from celerp_docs.routes_payments import WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY, deposit_account

    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY: "1200"}
    await session.commit()
    assert await deposit_account(session, auth["company_id"], override_key=WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY) == "1200"


async def _item(client, auth) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "WO-1", "name": "Widget", "quantity": 3, "sell_by": "piece",
        "cost_price": 5.0})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _writeoff_line_account(client, auth, item_id: str):
    r = await client.post("/lists/writeoff", headers=auth["headers"], json={"entity_ids": [item_id]})
    assert r.status_code == 200, r.text
    r = await client.get(f"/lists/{r.json()['id']}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    [line] = r.json()["line_items"]
    return line["account"]


@pytest.mark.asyncio
async def test_a_new_write_off_line_starts_on_the_shrinkage_account(session, client, auth):
    item = await _item(client, auth)
    await _account(client, auth, "6971", "expense", "6000")
    await set_role(session, auth["company_id"], "stock_shrinkage", "6971")
    await session.commit()
    assert await _writeoff_line_account(client, auth, item) == "6971"


@pytest.mark.asyncio
async def test_a_new_write_off_line_starts_blank_when_no_shrinkage_account_is_set(session, client, auth):
    item = await _item(client, auth)
    await _unmap(session, auth["company_id"], "stock_shrinkage")
    assert await _writeoff_line_account(client, auth, item) is None


_CHART = [{"code": c, "name": f"Account {c}"} for c in ("1130", "1130-P", "6950", "5150")]


def _line_accounts(page: str) -> list[str]:
    return re.findall(r'<input type="hidden" name="account_code" data-name="account_code" value="([^"]*)"', page)


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order"])
def test_a_bill_line_with_no_account_of_its_own_is_left_blank(doc_type):
    from ui.routes import documents

    doc = {"id": f"doc:{doc_type}", "entity_id": f"doc:{doc_type}", "doc_type": doc_type, "status": "draft",
           "line_items": [
               {"description": "Stock", "quantity": 1, "unit_price": 10, "receive_as": "stock"},
               {"description": "Service", "quantity": 1, "unit_price": 10, "receive_as": "expense"},
               {"description": "Freight in", "quantity": 1, "unit_price": 10, "account_code": "5150"},
           ]}
    page = to_xml(documents._doc_detail(doc, chart_accounts=_CHART))
    accounts = _line_accounts(page)
    # The two lines with no account stay blank, the line with its own keeps it, and the
    # row added for a new line starts blank too.
    assert sorted(accounts) == ["", "", "", "5150"], accounts
