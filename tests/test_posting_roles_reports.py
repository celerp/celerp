# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Accounting reports and entries read posting roles, not account numbers.

A party's statement keeps the receivable and payable history it had before the
receivable or payable account moved. Opening balances, the year-end close and a
reconciliation write-off book to the company's chosen accounts. The cash flow
statement finds cash under the company's cash header at any depth and classifies
every other account by its own category or its parent's, never by its number.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.services.account_roles import set_role
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net


async def _account(client, auth, code: str, account_type: str, parent_code: str | None = None, **extra) -> None:
    body = {"code": code, "name": f"Account {code}", "account_type": account_type, **extra}
    if parent_code:
        body["parent_code"] = parent_code
    r = await client.post("/accounting/accounts", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text


async def _remap(session, auth, role: str, code: str) -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _contact(client, auth) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": "Cust", "contact_type": "customer"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(client, auth, contact_id: str, total: float, issue_date: str) -> None:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_id": contact_id, "issue_date": issue_date,
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total}], "total": total})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


async def _je(client, auth, entries, ts="2026-02-10"):
    import uuid
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": ts, "memo": "Entry", "entries": entries, "idempotency_token": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


async def _cash_flow(client, auth) -> dict:
    r = await client.get("/accounting/cash-flow", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def _section(data: dict, code: str) -> str | None:
    for cat in ("operating", "investing", "financing"):
        if code in [line["code"] for line in data["direct"][cat]["lines"]]:
            return cat
    return None


@pytest.mark.asyncio
async def test_a_statement_keeps_receivable_history_after_the_receivable_moves(session, client, auth):
    await _account(client, auth, "1121", "asset", "1100")
    contact = await _contact(client, auth)
    await _invoice(client, auth, contact, 100.0, "2026-01-05")
    await _remap(session, auth, "receivable", "1121")
    await _invoice(client, auth, contact, 40.0, "2026-02-05")
    r = await client.get(f"/accounting/soa/{contact}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    soa = r.json()
    assert [row["debit"] for row in soa["rows"]] == [100.0, 40.0]
    assert soa["closing_balance"] == pytest.approx(140.0)


@pytest.mark.asyncio
async def test_a_new_bank_account_sits_under_the_cash_header_and_its_opening_balance_books_to_retained_earnings(
        session, client, auth):
    await _account(client, auth, "1180", "asset", "1100")
    await _account(client, auth, "3210", "equity", "3000")
    await _remap(session, auth, "cash_and_equivalents", "1180")
    await _remap(session, auth, "retained_earnings", "3210")
    r = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
        "bank_name": "Second", "account_number": "1", "bank_type": "checking", "currency": "THB",
        "opening_balance": 500.0})
    assert r.status_code == 200, r.text
    code = r.json()["chart_account_code"]
    from celerp_accounting.models import Account
    acc = (await session.execute(select(Account).where(
        Account.company_id == auth["company_id"], Account.code == code))).scalar_one()
    assert acc.parent_code == "1180"
    cid = auth["company_id"]
    assert (await _account_net(session, cid, "3210"), await _account_net(session, cid, "3200")) == (-500.0, 0.0)


@pytest.mark.asyncio
async def test_the_year_end_close_moves_profit_to_the_chosen_retained_earnings_account(session, client, auth):
    await _account(client, auth, "3210", "equity", "3000")
    await _remap(session, auth, "retained_earnings", "3210")
    await _je(client, auth, [{"account": "1111", "debit": 70.0, "credit": 0.0},
                             {"account": "4100", "debit": 0.0, "credit": 70.0}], ts="2025-06-01")
    r = await client.post("/accounting/close-year", headers=auth["headers"], json={"fiscal_year_end": "2025-12-31"})
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert (await _account_net(session, cid, "3210"), await _account_net(session, cid, "3200")) == (-70.0, 0.0)
    je = await _state(session, auth, r.json()["je_id"])
    assert [e["account_roles"] for e in je["entries"] if e["account"] == "3210"] == [["retained_earnings"]]


@pytest.mark.asyncio
async def test_a_reconciliation_write_off_with_no_account_named_books_to_the_general_expense_account(
        session, client, auth):
    await _account(client, auth, "6951", "expense", "6000")
    await _remap(session, auth, "general_expense", "6951")
    [bank] = (await client.get("/accounting/bank-accounts", headers=auth["headers"])).json()["items"]
    r = await client.post("/accounting/reconciliation/start", headers=auth["headers"], json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 0.5})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{r.json()['id']}/write-off", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    cid = auth["company_id"]
    assert (abs(await _account_net(session, cid, "6951")), await _account_net(session, cid, "6950")) == (0.5, 0.0)


@pytest.mark.asyncio
async def test_cash_held_in_an_account_nested_below_the_cash_header_is_cash(client, auth):
    await _account(client, auth, "1115", "asset", "1110")
    await _account(client, auth, "1116", "asset", "1115")
    await _je(client, auth, [{"account": "1116", "debit": 80.0, "credit": 0.0},
                             {"account": "4100", "debit": 0.0, "credit": 80.0}])
    data = await _cash_flow(client, auth)
    assert data["net_change"] == pytest.approx(80.0)
    assert data["direct"]["operating"]["total"] == pytest.approx(80.0)
    assert _section(data, "1116") is None


@pytest.mark.asyncio
async def test_an_account_takes_its_section_from_its_parent_not_its_number(client, auth):
    # Numbered like a non-current asset but filed under current assets: operating.
    await _account(client, auth, "1250", "asset", "1100")
    # Numbered like a current asset but filed under non-current assets: investing.
    await _account(client, auth, "1050", "asset", "1200")
    await _je(client, auth, [{"account": "1250", "debit": 30.0, "credit": 0.0},
                             {"account": "1111", "debit": 0.0, "credit": 30.0}])
    await _je(client, auth, [{"account": "1050", "debit": 20.0, "credit": 0.0},
                             {"account": "1111", "debit": 0.0, "credit": 20.0}])
    data = await _cash_flow(client, auth)
    assert (_section(data, "1250"), _section(data, "1050")) == ("operating", "investing")


@pytest.mark.asyncio
async def test_a_cash_header_moved_to_another_account_moves_what_counts_as_cash(session, client, auth):
    await _account(client, auth, "1180", "asset", "1100")
    await _account(client, auth, "1181", "asset", "1180")
    await _remap(session, auth, "cash_and_equivalents", "1180")
    await _je(client, auth, [{"account": "1181", "debit": 60.0, "credit": 0.0},
                             {"account": "4100", "debit": 0.0, "credit": 60.0}])
    data = await _cash_flow(client, auth)
    assert data["direct"]["operating"]["total"] == pytest.approx(60.0)
    assert data["balanced"] is True


@pytest.mark.asyncio
async def test_accounts_under_a_former_cash_header_stop_counting_as_cash(session, client, auth):
    await _account(client, auth, "1115", "asset", "1110")
    await _account(client, auth, "1180", "asset", "1100")
    await _remap(session, auth, "cash_and_equivalents", "1180")
    await _je(client, auth, [{"account": "1115", "debit": 25.0, "credit": 0.0},
                             {"account": "4100", "debit": 0.0, "credit": 25.0}])
    data = await _cash_flow(client, auth)
    assert "1115" not in data["cash_accounts"]
    assert data["net_change"] == pytest.approx(0.0)
