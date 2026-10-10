# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every door that moves money on a document takes only a real calendar date.

Voiding a payment, applying a credit note, refunding a credit note and paying several
documents at once each post on the date given, so a date that is not a real day is
refused before anything is recorded.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from test_payment_refund_accounting import _books, _invoice, _pay


async def _entries(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


async def _void_payment(client, session, auth, when):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)
    return f"/docs/{inv}/void-payment", {"payment_index": index, "refund_date": when}


async def _apply_credit(client, session, auth, when):
    inv = await _invoice(client, auth, 100.0)
    cn = await _invoice(client, auth, 30.0, doc_type="credit_note")
    return f"/docs/{cn}/apply-to-invoice", {"target_doc_id": inv, "amount": 20.0, "date": when}


async def _credit_refund(client, session, auth, when):
    cn = await _invoice(client, auth, 30.0, doc_type="credit_note")
    return f"/docs/{cn}/cn-refund", {"amount": 10.0, "date": when, "bank_account": "1111"}


async def _bulk_payment(client, session, auth, when):
    docs = [await _invoice(client, auth, 60.0) for _ in range(2)]
    return "/docs/bulk-payment", {"doc_ids": docs, "amount": 90.0, "payment_date": when, "bank_account": "1111"}


DOORS = {"void-payment": _void_payment, "apply-to-invoice": _apply_credit,
         "cn-refund": _credit_refund, "bulk-payment": _bulk_payment}


@pytest.mark.parametrize("when", ["not-a-date", "2026-02-30"])
@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.asyncio
async def test_a_money_door_needs_a_real_date(client, session, auth, door, when):
    path, body = await DOORS[door](client, session, auth, when)
    before = await _entries(session, auth)

    r = await client.post(path, headers=auth["headers"], json=body)
    assert r.status_code == 422, r.text
    assert await _entries(session, auth) == before


@pytest.mark.asyncio
async def test_a_bad_date_does_not_slip_past_a_closed_period(client, session, auth):
    inv = await _invoice(client, auth, 100.0)
    index = await _pay(client, session, auth, inv, 100.0)
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    before = await _entries(session, auth)

    r = await client.post(f"/docs/{inv}/void-payment", headers=auth["headers"],
                          json={"payment_index": index, "refund_date": "15/03/2026"})
    assert r.status_code == 422, r.text
    assert await _entries(session, auth) == before
    assert await _books(session, auth, "1111") == {"1111": 100.0}
