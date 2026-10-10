# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refund gives back money from one payment, and the books follow it.

The refund names the payment it returns money from and moves the same bank and the same
receivable that payment moved, at that payment's rates, in proportion to the amount
refunded. It can give back at most what is left of that payment.
"""
from __future__ import annotations

import pytest

from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net


async def _invoice(client, auth, total: float, **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"name": "Service", "quantity": 1, "unit_price": total}],
        "total": total, **extra,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


async def _pay(client, session, auth, doc_id: str, amount: float, bank: str = "1111", **extra) -> int:
    r = await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": "2026-03-02", "bank_account": bank, **extra})
    assert r.status_code == 200, r.text
    return (await _state(session, auth, doc_id))["payments"][-1]["index"]


async def _refund(client, doc_id: str, auth, **body):
    return await client.post(f"/docs/{doc_id}/refund", headers=auth["headers"],
                             json={"payment_date": "2026-03-09", **body})


async def _books(session, auth, *accounts: str) -> dict[str, float]:
    return {a: await _account_net(session, auth["company_id"], a) for a in accounts}


@pytest.mark.asyncio
async def test_refunding_a_whole_payment_takes_the_money_back_out_of_the_bank(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)

    r = await _refund(client, inv, auth, payment_index=index, amount=100.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1120") == {"1111": 0.0, "1120": 100.0}
    doc = await _state(session, auth, inv)
    assert (doc["amount_paid"], doc["amount_outstanding"]) == (0.0, 100.0)


@pytest.mark.asyncio
async def test_a_partial_refund_moves_its_share_of_the_payment(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)

    r = await _refund(client, inv, auth, payment_index=index, amount=30.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1120") == {"1111": 70.0, "1120": 30.0}

    r = await _refund(client, inv, auth, payment_index=index, amount=70.01)
    assert r.status_code == 422, r.text
    assert await _books(session, auth, "1111", "1120") == {"1111": 70.0, "1120": 30.0}


@pytest.mark.asyncio
async def test_refunding_one_of_two_payments_reverses_only_that_one(client, session, auth):
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": "1112", "name": "Second bank", "account_type": "asset", "parent_code": "1110"})
    assert r.status_code == 200, r.text
    inv = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    first = await _pay(client, session, auth, inv, 60.0, bank="1111", conversion_rate=1.1)
    second = await _pay(client, session, auth, inv, 40.0, bank="1112", conversion_rate=1.25)
    assert await _books(session, auth, "1111", "1112", "1120", "6960") == \
        {"1111": 66.0, "1112": 50.0, "1120": 0.0, "6960": -6.0}

    r = await _refund(client, inv, auth, payment_index=second, amount=50.0)
    assert r.status_code == 422, r.text

    r = await _refund(client, inv, auth, payment_index=second, amount=40.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1112", "1120", "6960") == \
        {"1111": 66.0, "1112": 0.0, "1120": 44.0, "6960": 0.0}

    r = await _refund(client, inv, auth, payment_index=first, amount=30.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1112", "1120", "6960") == \
        {"1111": 33.0, "1112": 0.0, "1120": 77.0, "6960": 0.0}
    doc = await _state(session, auth, inv)
    assert (doc["amount_paid"], doc["amount_outstanding"]) == (30.0, 70.0)


@pytest.mark.asyncio
async def test_a_refund_must_say_which_payment_it_gives_back(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    await _pay(client, session, auth, inv, 100.0)

    r = await _refund(client, inv, auth, amount=30.0)
    assert r.status_code == 422, r.text
    r = await _refund(client, inv, auth, payment_index=99, amount=30.0)
    assert r.status_code == 422, r.text
    assert await _books(session, auth, "1111") == {"1111": 100.0}
    assert (await _state(session, auth, inv))["amount_paid"] == 100.0


@pytest.mark.asyncio
async def test_sending_the_same_refund_again_changes_nothing(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)

    first = await _refund(client, inv, auth, payment_index=index, amount=30.0, idempotency_key="refund-once")
    again = await _refund(client, inv, auth, payment_index=index, amount=30.0, idempotency_key="refund-once")
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert first.json() == again.json()
    assert await _books(session, auth, "1111", "1120") == {"1111": 70.0, "1120": 30.0}
    assert (await _state(session, auth, inv))["amount_paid"] == 70.0


@pytest.mark.asyncio
async def test_voiding_a_partly_refunded_payment_reverses_only_what_is_left(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)
    assert (await _refund(client, inv, auth, payment_index=index, amount=30.0)).status_code == 200

    r = await client.post(f"/docs/{inv}/void-payment", headers=auth["headers"], json={"payment_index": index})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1120") == {"1111": 0.0, "1120": 100.0}
    doc = await _state(session, auth, inv)
    assert (doc["amount_paid"], doc["amount_outstanding"]) == (0.0, 100.0)


@pytest.mark.parametrize("finish", ["void", "refund"])
@pytest.mark.asyncio
async def test_giving_back_a_foreign_currency_payment_in_pieces_leaves_nothing_behind(client, session, auth, finish):
    inv = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.105)
    index = await _pay(client, session, auth, inv, 100.0, conversion_rate=1.105)
    assert await _books(session, auth, "1111", "1120") == {"1111": 110.5, "1120": 0.0}

    for amount in (1.0, 1.0, 1.0):
        assert (await _refund(client, inv, auth, payment_index=index, amount=amount)).status_code == 200
    if finish == "void":
        r = await client.post(f"/docs/{inv}/void-payment", headers=auth["headers"], json={"payment_index": index})
    else:
        r = await _refund(client, inv, auth, payment_index=index, amount=97.0)
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1111", "1120", "6960") == {"1111": 0.0, "1120": 110.5, "6960": 0.0}


@pytest.mark.parametrize("when", ["not-a-date", "2026-02-30", ""])
@pytest.mark.asyncio
async def test_a_refund_needs_a_real_date(client, session, auth, when):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)

    r = await _refund(client, inv, auth, payment_index=index, amount=30.0, payment_date=when)
    assert r.status_code == 422, r.text
    assert await _books(session, auth, "1111") == {"1111": 100.0}
    assert (await _state(session, auth, inv))["amount_paid"] == 100.0


@pytest.mark.asyncio
async def test_a_refunded_payment_cannot_be_deleted(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)
    assert (await _refund(client, inv, auth, payment_index=index, amount=30.0)).status_code == 200

    r = await client.delete(f"/docs/{inv}/payments/{index}", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert await _books(session, auth, "1111", "1120") == {"1111": 70.0, "1120": 30.0}
