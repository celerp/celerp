# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Money paid or refunded moves through an asset account that can hold it.

A new payment, a credit note refund and a payment across several documents each
name the account the money moves through. It must be an active asset account with
nothing under it: a bank, a card processor's clearing account, undeposited funds.
A header, a switched-off account, a liability or an income account is refused
before anything is recorded. Money already recorded can still be given back
through the account it came in through, even after that account is switched off.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_cost_restatement import _state
from test_payment_refund_accounting import _books, _invoice, _pay, _refund

pytestmark = pytest.mark.asyncio


async def _entries(session, auth) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _account(client, auth, code: str, name: str, parent: str = "1110") -> str:
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": code, "name": name, "account_type": "asset", "parent_code": parent})
    assert r.status_code == 200, r.text
    return code


async def _switch_off(client, auth, code: str) -> None:
    r = await client.patch(f"/accounting/accounts/{code}", headers=auth["headers"], json={"is_active": False})
    assert r.status_code == 200, r.text


async def _inactive(client, auth) -> str:
    code = await _account(client, auth, "1113", "Closed bank")
    await _switch_off(client, auth, code)
    return code


async def _header(client, auth) -> str:
    return "1110"


async def _liability(client, auth) -> str:
    return "2110"


async def _revenue(client, auth) -> str:
    return "4100"


async def _missing(client, auth) -> str:
    return "1199"


UNFIT = {"inactive": _inactive, "header": _header, "liability": _liability, "revenue": _revenue,
         "missing": _missing}


async def _payment(client, session, auth, bank):
    inv = await _invoice(client, auth, 100.0)
    return f"/docs/{inv}/payment", {"amount": 100.0, "payment_date": "2026-03-02", "bank_account": bank}


async def _credit_refund(client, session, auth, bank):
    cn = await _invoice(client, auth, 30.0, doc_type="credit_note")
    return f"/docs/{cn}/cn-refund", {"amount": 10.0, "date": "2026-03-02", "bank_account": bank}


async def _bulk_payment(client, session, auth, bank):
    docs = [await _invoice(client, auth, 60.0) for _ in range(2)]
    return "/docs/bulk-payment", {"doc_ids": docs, "amount": 90.0, "payment_date": "2026-03-02",
                                  "bank_account": bank}


DOORS = {"payment": _payment, "cn-refund": _credit_refund, "bulk-payment": _bulk_payment}


@pytest.mark.parametrize("kind", sorted(UNFIT))
@pytest.mark.parametrize("door", sorted(DOORS))
async def test_money_cannot_move_through_an_account_that_cannot_hold_it(client, session, auth, door, kind):
    bank = await UNFIT[kind](client, auth)
    path, body = await DOORS[door](client, session, auth, bank)
    before = await _entries(session, auth)

    r = await client.post(path, headers=auth["headers"], json=body)
    assert r.status_code == 422, r.text
    assert f"Account {bank}" in r.json()["detail"]
    assert await _entries(session, auth) == before


@pytest.mark.parametrize("door", sorted(DOORS))
async def test_money_moves_through_a_clearing_asset_account(client, session, auth, door):
    clearing = await _account(client, auth, "1160", "Undeposited funds", parent="1100")
    path, body = await DOORS[door](client, session, auth, clearing)

    r = await client.post(path, headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    assert (await _books(session, auth, clearing))[clearing] != 0.0


async def test_a_payment_is_given_back_through_its_account_after_that_account_is_switched_off(
        client, session, auth):
    bank = await _account(client, auth, "1112", "Old bank")
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0, bank=bank)
    await _switch_off(client, auth, bank)

    r = await _refund(client, inv, auth, payment_index=index, amount=30.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, bank, "1120") == {bank: 70.0, "1120": 30.0}

    r = await client.post(f"/docs/{inv}/void-payment", headers=auth["headers"],
                          json={"payment_index": index, "refund_date": "2026-03-10"})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, bank, "1120") == {bank: 0.0, "1120": 100.0}
    doc = await _state(session, auth, inv)
    assert (doc["amount_paid"], doc["amount_outstanding"]) == (0.0, 100.0)
