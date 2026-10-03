# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A payment recorded by hand goes to one of the company's own active asset accounts.

An account that does not exist, is archived, belongs to another company, or cannot hold
money is refused with a message naming it, and nothing is recorded.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "PayCo", "email": f"pay-{uuid.uuid4().hex[:8]}@test.local", "name": "Admin",
        "password": "validpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _account(client, tok, code: str, account_type: str, *, active: bool = True) -> None:
    r = await client.post("/accounting/accounts", headers=_h(tok), json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": None})
    assert r.status_code == 200, r.text
    if not active:
        r = await client.patch(f"/accounting/accounts/{code}", headers=_h(tok), json={"is_active": False})
        assert r.status_code == 200, r.text


async def _invoice(client, tok) -> str:
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "line_items": [{"name": "Service", "quantity": 1, "unit_price": 100.0}],
        "total": 100.0})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    return doc_id


async def _payments(client, tok, doc_id: str) -> list:
    r = await client.get(f"/docs/{doc_id}", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json().get("payments") or []


async def _bad_account(client, tok, case: str) -> tuple[str, str]:
    """(account code, expected refusal) for each way an account cannot take a payment."""
    if case == "missing":
        return "1199", "Unknown account 1199."
    if case == "archived":
        await _account(client, tok, "1190", "asset", active=False)
        return "1190", "Account 1190 is inactive."
    if case == "not_asset":
        return "4100", "Account 4100 is a revenue account"
    r = await client.post("/companies", json={"name": "OtherCo"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    await _account(client, r.json()["access_token"], "1190", "asset")
    return "1190", "Unknown account 1190."


_CASES = ["missing", "archived", "not_asset", "other_company"]


@pytest.mark.parametrize("case", _CASES)
async def test_a_payment_to_an_account_that_cannot_take_it_is_refused(client, case):
    tok = await _register(client)
    doc_id = await _invoice(client, tok)
    code, reason = await _bad_account(client, tok, case)

    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(tok), json={
        "amount": 100.0, "payment_date": "2026-03-02", "bank_account": code})

    assert r.status_code == 422, r.text
    assert reason in r.json()["detail"]
    assert await _payments(client, tok, doc_id) == []


@pytest.mark.parametrize("case", _CASES)
async def test_a_bulk_payment_to_an_account_that_cannot_take_it_is_refused(client, case):
    tok = await _register(client)
    doc_ids = [await _invoice(client, tok), await _invoice(client, tok)]
    code, reason = await _bad_account(client, tok, case)

    r = await client.post("/docs/bulk-payment", headers=_h(tok), json={
        "doc_ids": doc_ids, "amount": 200.0, "payment_date": "2026-03-02", "bank_account": code})

    assert r.status_code == 422, r.text
    assert reason in r.json()["detail"]
    for doc_id in doc_ids:
        assert await _payments(client, tok, doc_id) == []


async def test_a_payment_to_an_active_asset_account_is_recorded(client):
    tok = await _register(client)
    doc_id = await _invoice(client, tok)
    await _account(client, tok, "1190", "asset")

    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(tok), json={
        "amount": 100.0, "payment_date": "2026-03-02", "bank_account": "1190"})

    assert r.status_code == 200, r.text
    assert [p["bank_account"] for p in await _payments(client, tok, doc_id)] == ["1190"]
