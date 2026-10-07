# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""A bank account and the chart account it posts to stay consistent: an active bank
account always posts to an active asset account."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "BankCo", "email": f"bank-{uuid.uuid4().hex[:8]}@test.local", "name": "Admin",
        "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bank(client, tok, **payload) -> dict:
    r = await client.post("/accounting/bank-accounts", headers=_h(tok), json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking", "currency": "USD",
        **payload})
    assert r.status_code == 200, r.text
    return r.json()


async def _chart_account(client, tok, code: str) -> dict:
    items = (await client.get("/accounting/chart", headers=_h(tok))).json()["items"]
    return next(a for a in items if a["code"] == code)


async def _account(client, tok, code: str, account_type: str, *, active: bool = True) -> None:
    r = await client.post("/accounting/accounts", headers=_h(tok), json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": None})
    assert r.status_code == 200, r.text
    if not active:
        r = await client.patch(f"/accounting/accounts/{code}", headers=_h(tok), json={"is_active": False})
        assert r.status_code == 200, r.text


# ── creating a bank account on an existing chart account ─────────────────────

@pytest.mark.parametrize("account_type, active, reason", [
    ("asset", False, "Account 1190 is inactive."),
    ("revenue", True, "Account 1190 is a revenue account"),
    ("liability", True, "Account 1190 is a liability account"),
])
async def test_a_bank_account_on_an_existing_chart_account_needs_an_active_asset_account(
        client, account_type, active, reason):
    tok = await _register(client)
    await _account(client, tok, "1190", account_type, active=active)

    r = await client.post("/accounting/bank-accounts", headers=_h(tok), json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking", "currency": "USD",
        "account_code": "1190"})

    assert r.status_code == 422, r.text
    assert reason in r.json()["detail"]
    banks = (await client.get("/accounting/bank-accounts?include_inactive=true", headers=_h(tok))).json()
    assert "1190" not in [b["chart_account_code"] for b in banks["items"]]


async def test_a_bank_account_on_an_existing_active_asset_account_is_created(client):
    tok = await _register(client)
    await _account(client, tok, "1190", "asset")
    assert (await _bank(client, tok, account_code="1190"))["chart_account_code"] == "1190"


async def test_a_new_bank_account_gets_its_own_active_asset_account(client):
    tok = await _register(client)
    bank = await _bank(client, tok)
    acc = await _chart_account(client, tok, bank["chart_account_code"])
    assert (acc["account_type"], acc["is_active"], acc["parent_code"]) == ("asset", True, "1110")


# ── changing the chart account behind an active bank account ─────────────────

@pytest.mark.parametrize("change", [{"is_active": False}, {"account_type": "revenue"},
                                    {"account_type": "liability", "is_active": False}])
async def test_the_chart_account_of_an_active_bank_account_cannot_be_archived_or_retyped(client, change):
    tok = await _register(client)
    code = (await _bank(client, tok))["chart_account_code"]

    r = await client.patch(f"/accounting/accounts/{code}", headers=_h(tok), json=change)

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (
        f"Account {code} belongs to the active bank account Harbor Bank. Archive the bank account "
        "before archiving this account or changing its type.")
    acc = await _chart_account(client, tok, code)
    assert (acc["account_type"], acc["is_active"]) == ("asset", True)


async def test_the_chart_account_of_an_archived_bank_account_can_be_archived_and_retyped(client):
    tok = await _register(client)
    bank = await _bank(client, tok)
    code = bank["chart_account_code"]
    r = await client.patch(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok), json={"is_active": False})
    assert r.status_code == 200, r.text

    r = await client.patch(f"/accounting/accounts/{code}", headers=_h(tok),
                           json={"is_active": False, "account_type": "expense"})

    assert r.status_code == 200, r.text
    assert (r.json()["account_type"], r.json()["is_active"]) == ("expense", False)


async def test_the_chart_account_of_an_active_bank_account_can_still_be_edited(client):
    """The edit form sends the type and status back unchanged with every save."""
    tok = await _register(client)
    code = (await _bank(client, tok))["chart_account_code"]

    r = await client.patch(f"/accounting/accounts/{code}", headers=_h(tok), json={
        "name": "Harbor Bank operating", "account_type": "asset", "is_active": True, "parent_code": "1110"})

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Harbor Bank operating"


# ── restoring an archived bank account ────────────────────────────────────────

async def test_a_bank_account_is_restored_only_onto_an_active_asset_account(client):
    tok = await _register(client)
    bank = await _bank(client, tok)
    code = bank["chart_account_code"]
    assert (await client.patch(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok),
                               json={"is_active": False})).status_code == 200
    assert (await client.patch(f"/accounting/accounts/{code}", headers=_h(tok),
                               json={"is_active": False})).status_code == 200

    r = await client.patch(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok), json={"is_active": True})

    assert r.status_code == 422, r.text
    assert f"Account {code} is inactive." in r.json()["detail"]
    banks = (await client.get("/accounting/bank-accounts?include_inactive=true", headers=_h(tok))).json()["items"]
    assert [b["is_active"] for b in banks if b["id"] == bank["id"]] == [False]

    assert (await client.patch(f"/accounting/accounts/{code}", headers=_h(tok),
                               json={"is_active": True})).status_code == 200
    r = await client.patch(f"/accounting/bank-accounts/{bank['id']}", headers=_h(tok), json={"is_active": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True


# ── Cash (1110), which has no bank account ────────────────────────────────────

@pytest.mark.parametrize("change", [{"is_active": False}, {"account_type": "liability"}])
async def test_cash_in_a_company_that_never_connected_online_payments_is_kept_only_by_its_posting_role(
        client, change):
    """Online payments add no rule of their own to Cash: in a company that never connected
    them, the only thing keeping 1110 an active asset account is that it is the posting
    account for Cash and cash equivalents."""
    tok = await _register(client)

    r = await client.patch("/accounting/accounts/1110", headers=_h(tok), json=change)

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail.startswith("Account 1110 is the posting account for Cash and cash equivalents")
    assert "nline payments" not in detail
    acc = await _chart_account(client, tok, "1110")
    assert (acc["account_type"], acc["is_active"]) == ("asset", True)


# ── the rule every posting meets ──────────────────────────────────────────────

async def test_a_journal_entry_on_an_inactive_account_is_refused(client):
    tok = await _register(client)
    await _account(client, tok, "6190", "expense", active=False)

    r = await client.post("/accounting/journal-entries", headers=_h(tok), json={
        "ts": "2026-07-01", "memo": "Supplies", "idempotency_token": uuid.uuid4().hex, "entries": [
            {"account": "6190", "debit": 10, "credit": 0},
            {"account": "1111", "debit": 0, "credit": 10}]})

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "posting.destination.inactive"
    assert r.json()["detail"]["params"] == {"code": "6190"}
