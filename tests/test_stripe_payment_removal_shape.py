"""A payment removal that names no payment, or deletes by list position, could take a
Stripe payment off a document without the Stripe check seeing it: every writer must name
the payment by its index, and a deletion must keep the payment's place."""
from __future__ import annotations

import pytest

from migration_support import maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _harbor, _invoice, _pay_by_hand
from test_stripe_refunds import PAID_AT, BOOKS, _RefundCloud, _deleted_before_upgrade, _doc, _ledger, _paid_invoice

pytestmark = pytest.mark.asyncio


async def _emit(engine, company_id, entity_id, event_type, data, source="api"):
    from fastapi import HTTPException

    from celerp.events.engine import emit_event
    async with maker(engine)() as s:
        try:
            await emit_event(s, company_id=company_id, entity_id=entity_id, entity_type="doc",
                             event_type=event_type, data=data, actor_id=None, location_id=None,
                             source=source, idempotency_key=f"shape:{event_type}:{sorted(data.items())}")
        except HTTPException as refused:
            await s.rollback()
            return refused.status_code
        await s.commit()
        return 200


@pytest.mark.parametrize("event_type,data", [
    ("doc.payment.refunded", {"amount": 100.0, "refund_date": "2026-09-05"}),
    ("doc.payment.deleted", {"payment_index": 0, "ts": "2026-09-01"}),
], ids=["refund-naming-no-payment", "delete-by-position"])
async def test_a_removal_that_does_not_name_its_payment_is_refused(real_engine, real_client, monkeypatch,
                                                                   event_type, data):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    before, events = await _doc(real_engine, invoice), await _ledger(real_engine, invoice)
    assert await _emit(real_engine, a, invoice, event_type, data) == 422
    assert await _doc(real_engine, invoice) == before and await _ledger(real_engine, invoice) == events


async def test_a_delete_by_position_cannot_reach_a_stripe_payment_whose_index_differs_from_its_place(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    await _pay_by_hand(real_client, real_engine, boss, a, invoice, 100.0)
    await _pay_by_hand(real_client, real_engine, boss, a, invoice, 100.0)
    await _deleted_before_upgrade(real_engine, a, invoice, 0)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.pay(a, invoice, "pi_1", amount_minor=50000, paid_at=PAID_AT, books=BOOKS)
    await cloud.deliver()
    payments = (await _doc(real_engine, invoice))["payments"]
    place = next(i for i, p in enumerate(payments) if p.get("reference") == "pi_1")
    assert payments[place]["index"] != place
    assert await _emit(real_engine, a, invoice, "doc.payment.deleted", {"payment_index": place, "ts": "2026-10-01"}) == 422
    assert any(p.get("reference") == "pi_1" and p.get("status") == "active"
               for p in (await _doc(real_engine, invoice))["payments"])
