# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Bulk payment allocation under concurrent payments.

A bulk payment takes every document it pays before it allocates, so it allocates
against what is really outstanding: a payment in flight on one of its documents
finishes first, and the bulk pays only what is left. Two bulk payments over the same
documents in opposite order both finish. A document it cannot take in time fails the
whole bulk payment with nothing recorded, rather than a partial batch.

Row locking is only observable across separately committed transactions, so these run
on real Postgres via the session-scoped _db_engine with independent sessions."""

from __future__ import annotations

import asyncio
import datetime
import types
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp_accounting.routes import seed_chart_of_accounts_hook
from celerp_accounting.models import Account, BankAccount
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_company(factory):
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="Bulk Co", slug=f"bulk-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"bulk-{user_id.hex[:8]}@bulk.test", name="Bulk User",
                   auth_hash="x"))
        await s.flush()
        await seed_chart_of_accounts_hook(session=s, company_id=company_id)
        await s.commit()
    return company_id, user_id, types.SimpleNamespace(id=user_id)


async def _cleanup(factory, company_id, user_id):
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(BankAccount).where(BankAccount.company_id == company_id))
        await s.execute(delete(Account).where(Account.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _seed_invoice(factory, company_id, user, *, ref_id, total, contact_id) -> str:
    """A committed, issued (status 'sent') invoice with a real payable total."""
    entity_id = f"doc:{ref_id}-{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": "invoice", "status": "draft", "ref_id": ref_id,
                  "contact_id": contact_id,
                  "line_items": [{"name": "X", "quantity": 1, "unit_price": total,
                                  "line_total": total}],
                  "subtotal": total, "total": total, "amount_outstanding": total},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await emit_event(
            s, company_id=company_id, entity_id=entity_id, entity_type="doc",
            event_type="doc.sent", data={},
            actor_id=user.id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    return entity_id


async def _state(factory, company_id, entity_id) -> dict:
    async with factory() as s:
        row = await s.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                          populate_existing=True)
        return dict(row.state)


async def _outstanding(factory, company_id, entity_id) -> float:
    st = await _state(factory, company_id, entity_id)
    return float(st.get("amount_outstanding", st.get("total", 0)) or 0)


@pytest.mark.asyncio
async def test_bulk_payment_pays_what_a_payment_in_flight_left(_db_engine):
    from celerp_docs.routes import bulk_payment, BulkPaymentBody, DocPaymentBody, record_payment

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        inv = await _seed_invoice(factory, company_id, user, ref_id="BULK-STALE",
                                  total=100.0, contact_id="contact:stale")
        first, second = factory(), factory()
        release = asyncio.Event()
        commit = first.commit

        async def held_commit() -> None:
            await release.wait()
            await commit()

        first.commit = held_commit
        try:
            single = asyncio.create_task(record_payment(
                inv, DocPaymentBody(amount=60.0, payment_date="2026-02-01", method="cash",
                                    bank_account="1111"),
                company_id=company_id, _=None, user=user, session=first))
            await asyncio.sleep(0.3)
            bulk = asyncio.create_task(bulk_payment(
                BulkPaymentBody(doc_ids=[inv], amount=100.0, payment_date="2026-02-02",
                                method="cash", bank_account="1111"),
                company_id=company_id, _=None, user=user, session=second))
            await asyncio.sleep(0.3)
            release.set()
            _, result = await asyncio.wait_for(asyncio.gather(single, bulk), timeout=10)
        finally:
            await first.close()
            await second.close()

        assert result["allocations"] == [{"doc_id": inv, "amount": 40.0}]
        assert (result["total_allocated"], result["remaining"]) == (40.0, 60.0)
        assert await _outstanding(factory, company_id, inv) == 0
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_stripe_overpay_is_refused_whole(_db_engine):
    """A confirmed online charge larger than what the invoice still owes is refused
    whole, like any other payment: nothing is clamped onto the invoice."""
    from fastapi import HTTPException
    from celerp_docs import routes_payments

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        inv = await _seed_invoice(factory, company_id, user, ref_id="STRIPE-OVR",
                                  total=50.0, contact_id="contact:stripe")
        # A confirmed online charge of 200 (minor units) against a 50-outstanding invoice.
        async with factory() as s:
            doc_state = (await s.get(
                Projection, {"company_id": company_id, "entity_id": inv},
                populate_existing=True)).state
            with pytest.raises(HTTPException) as refused:
                await routes_payments.record_stripe_payment(
                    s, company_id, inv, dict(doc_state),
                    reference="pi_test_123", amount_minor=20000, currency="usd",
                    paid_at=datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc),
                    context={"deposit_account": "1111", "timezone": "UTC", "base_currency": "USD", "rate": "1"},
                    managed=True)
        assert refused.value.status_code == 409

        st = await _state(factory, company_id, inv)
        assert abs(await _outstanding(factory, company_id, inv) - 50.0) < 0.01
        assert not [p for p in (st.get("payments") or []) if p.get("status") != "deleted"]
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_payment_that_cannot_take_a_document_records_nothing(_db_engine):
    from sqlalchemy.exc import DBAPIError
    from celerp_docs.routes import bulk_payment, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        held = await _seed_invoice(factory, company_id, user, ref_id="BULK-LOCK-H",
                                   total=100.0, contact_id="contact:lock")
        other = await _seed_invoice(factory, company_id, user, ref_id="BULK-LOCK-O",
                                    total=100.0, contact_id="contact:lock")
        s_hold, s_bulk = factory(), factory()
        try:
            await s_hold.execute(
                text("SELECT 1 FROM projections WHERE company_id = :c AND entity_id = :e FOR UPDATE"),
                {"c": str(company_id), "e": held})
            await s_bulk.execute(text("SET lock_timeout = '800ms'"))
            with pytest.raises(DBAPIError):
                await bulk_payment(
                    BulkPaymentBody(doc_ids=[held, other], amount=250.0, payment_date="2026-02-05",
                                    method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=s_bulk)
        finally:
            await s_hold.close()
            await s_bulk.close()
        assert await _outstanding(factory, company_id, held) == 100.0
        assert await _outstanding(factory, company_id, other) == 100.0
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_bulk_payments_in_opposite_order_both_finish(_db_engine):
    from celerp_docs.routes import bulk_payment, BulkPaymentBody

    factory = _factory(_db_engine)
    company_id, user_id, user = await _seed_company(factory)
    try:
        d0 = await _seed_invoice(factory, company_id, user, ref_id="BULK-DL-0", total=30.0,
                                 contact_id="contact:dl")
        d1 = await _seed_invoice(factory, company_id, user, ref_id="BULK-DL-1", total=30.0,
                                 contact_id="contact:dl")
        s_a, s_b = factory(), factory()
        try:
            results = await asyncio.wait_for(asyncio.gather(*(
                bulk_payment(
                    BulkPaymentBody(doc_ids=order, amount=30.0, payment_date="2026-02-06",
                                    method="cash", bank_account="1111"),
                    company_id=company_id, _=None, user=user, session=session)
                for session, order in ((s_a, [d0, d1]), (s_b, [d1, d0])))), timeout=10)
        finally:
            await s_a.close()
            await s_b.close()
        assert sorted(a["doc_id"] for r in results for a in r["allocations"]) == sorted([d0, d1])
        assert await _outstanding(factory, company_id, d0) == 0
        assert await _outstanding(factory, company_id, d1) == 0
    finally:
        await _cleanup(factory, company_id, user_id)
