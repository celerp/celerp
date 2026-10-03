# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refund of an online payment is made in Stripe and delivered by Celerp Cloud. Each
refund gives its money back from the payment it refunds, found by the payment's
reference, once, on the books the payment was recorded on; one Stripe reverses posts
its exact mirror once. A refund that cannot be applied yet (its company, invoice or
payment is not there, or it gives back more than is left of the payment) is kept,
whole, with the unmatched payments, and applies, in the order Stripe reported, when its
payment is recorded on its invoice. A refund Stripe undoes and later puts through again
gives the money back again, each time once. A System Recovery restore has Celerp Cloud
deliver the payment, its refunds and its release again, which rebuilds the books
exactly. A payment Stripe holds the money for is never refunded, voided or deleted
here, until Stripe is disconnected: the payment is then no longer linked to Stripe,
for good, and is refunded here like any other."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from company_backup_support import token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import (_PAYMENT, BOOKS, _Cloud, _harbor, _invoice, _paid, _reset,
                                         _system_recovery, _unmatched)

pytestmark = pytest.mark.asyncio

_REFUND = ("company_id", "entity_id", "reference", "refund_id", "cycle", "transition", "amount_minor",
           "currency", "occurred_at", "context", "delivery_id")
_RELEASE = ("company_id", "entity_id", "reference", "released_at", "delivery_id")
PAID_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
STRIPE_OWNED = "This payment was received through Stripe, so it can only be refunded or reversed in Stripe."


def _at(hours: int) -> datetime:
    return PAID_AT + timedelta(hours=hours)


class _RefundCloud(_Cloud):
    """Celerp Cloud delivering payments and the changes to their refunds as one stream,
    each until the installation acknowledges it."""

    def refund(self, company_id, entity_id: str, refund_id: str, amount_minor: int, occurred_at: datetime, *,
               reference: str = "pi_1", transition: str = "applied", cycle: int = 1, books=BOOKS) -> None:
        self.deliveries.append({"kind": "refund", "company_id": str(company_id), "entity_id": entity_id,
                                "reference": reference, "refund_id": refund_id, "cycle": cycle,
                                "transition": transition,
                                "amount_minor": amount_minor, "currency": "usd",
                                "occurred_at": occurred_at.isoformat(), "context": books,
                                "delivery_id": str(uuid.uuid4()), "acked": False})

    def release(self, company_id, entity_id: str, released_at: datetime, *, reference: str = "pi_1") -> None:
        """Stripe was disconnected: the payment is no longer linked to it."""
        self.deliveries.append({"kind": "release", "company_id": str(company_id), "entity_id": entity_id,
                                "reference": reference, "released_at": released_at.isoformat(),
                                "delivery_id": str(uuid.uuid4()), "acked": False})

    async def deliver(self) -> None:
        for d in [d for d in self.deliveries if not d["acked"]]:
            d["acked"] = await _deliver_one(d)


async def _deliver_one(d: dict) -> bool:
    """Deliver *d* through the gateway as Celerp Cloud does; whether it was acknowledged."""
    from celerp.gateway.client import GatewayClient
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")
    gateway._ws = object()
    acked = []

    async def send(ws, message):
        assert message["type"] == "event.ack"
        acked.append(message["payload"]["delivery_id"])
    gateway._send = send
    if d.get("kind") == "refund":
        await gateway._handle_invoice_refund({k: v for k, v in d.items() if k in _REFUND})
    elif d.get("kind") == "release":
        await gateway._handle_invoice_payment_release({k: v for k, v in d.items() if k in _RELEASE})
    else:
        await gateway._handle_invoice_payment({k: v for k, v in d.items() if k in _PAYMENT})
    return acked == [d["delivery_id"]]


async def _paid_invoice(engine, client, monkeypatch, *, reference: str = "pi_1"):
    """A company whose invoice of 1,070.00 USD a customer paid online."""
    boss, a, b = await _harbor(engine)
    invoice = await _invoice(client, engine, boss, a)
    cloud = _RefundCloud(monkeypatch, engine)
    cloud.pay(a, invoice, reference, paid_at=PAID_AT, books=BOOKS)
    await cloud.deliver()
    assert await _paid(engine, invoice) == [(reference, 1070.0)]
    return boss, a, b, invoice, cloud


async def _doc(engine, entity_id) -> dict:
    async with maker(engine)() as s:
        return await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})


async def _books(engine, entity_id) -> dict[str, Decimal]:
    """Net debit per account across the posted journal entries of the invoice's
    payments, its refunds and their reversals."""
    async with maker(engine)() as s:
        entries = (await s.scalars(text("SELECT state FROM projections WHERE entity_id LIKE :j"),
                                   {"j": f"je:auto:{entity_id}:pay%"})).all()
    net: dict[str, Decimal] = {}
    for je in entries:
        if je.get("status") != "posted":
            continue
        for line in je.get("entries", []):
            net[line["account"]] = (net.get(line["account"], Decimal(0)) + Decimal(str(line.get("debit") or 0))
                                    - Decimal(str(line.get("credit") or 0)))
    return {k: v for k, v in net.items() if v}


async def _journal(engine, entity_id) -> list[tuple[str, str]]:
    """(journal entry, day) of the invoice's payment, refunds and reversals."""
    async with maker(engine)() as s:
        return sorted((r[0], str(r[1].get("ts"))[:10]) for r in (await s.execute(text(
            "SELECT entity_id, state FROM projections WHERE entity_id LIKE :j"),
            {"j": f"je:auto:{entity_id}:pay%"})).all())


async def _ar_account(engine, entity_id) -> str:
    async with maker(engine)() as s:
        je = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :j"),
                            {"j": f"je:auto:{entity_id}:pay:0"})
    return next(line["account"] for line in je["entries"] if line.get("credit"))


async def _kept_refunds(engine) -> list[tuple]:
    async with maker(engine)() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT refund_id, cycle, transition, reference, amount_minor, former_company, document "
            "FROM unmatched_refunds ORDER BY occurred_at, refund_id, cycle, transition"))).all()]


async def _ledger(engine, entity_id) -> list[str]:
    async with maker(engine)() as s:
        return list((await s.scalars(text(
            "SELECT event_type FROM ledger WHERE entity_id = :e OR entity_id LIKE :j ORDER BY id"),
            {"e": entity_id, "j": f"je:auto:{entity_id}:%"})).all())


def _payment(doc: dict) -> dict:
    return next(p for p in doc["payments"] if p.get("reference") == "pi_1")


async def _assert_books(engine, entity_id, *, refunded: str) -> None:
    """The invoice and its books once *refunded* of its 1,070.00 payment is given back."""
    ar = await _ar_account(engine, entity_id)
    left = Decimal("1070") - Decimal(refunded)
    doc = await _doc(engine, entity_id)
    assert Decimal(str(_payment(doc).get("refunded") or 0)) == Decimal(refunded)
    assert Decimal(str(doc["amount_paid"])) == left
    assert Decimal(str(doc["amount_outstanding"])) == Decimal(refunded)
    assert await _books(engine, entity_id) == ({"1110": left, ar: -left} if left else {})


# ── One refund, applied once ─────────────────────────────────────────────────

async def test_a_refund_delivered_twice_is_applied_once(real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    await cloud.deliver()
    cloud.deliveries[-1]["acked"] = False  # the acknowledgement was lost
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="200")
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refunded") == 1
    assert await _kept_refunds(real_engine) == []


async def test_two_partial_refunds_each_give_back_their_share(real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.refund(a, invoice, "re_2", 30000, _at(30))
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="500")
    assert await _journal(real_engine, invoice) == [
        (f"je:auto:{invoice}:pay:0", "2026-09-01"),
        (f"je:auto:{invoice}:payrefund:refund_0_0", "2026-09-01"),
        (f"je:auto:{invoice}:payrefund:refund_0_1", "2026-09-02")]


async def test_refunds_made_in_stripe_while_disconnected_arrive_weeks_later_into_exact_books(
        real_engine, real_client, monkeypatch):
    """Refunds made in Stripe while the company was disconnected reach Celerp when it
    reconnects the same Stripe account: weeks after the payment, one of them already
    failed and reversed, each dated when it happened."""
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(24 * 20))
    cloud.refund(a, invoice, "re_2", 30000, _at(24 * 21))
    cloud.refund(a, invoice, "re_2", 30000, _at(24 * 23), transition="reversed")
    cloud.refund(a, invoice, "re_3", 5000, _at(24 * 22))
    await cloud.deliver()
    for d in cloud.deliveries:  # reconnecting again finds the same refunds
        d["acked"] = False
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="250")
    assert await _kept_refunds(real_engine) == []
    assert await _journal(real_engine, invoice) == [
        (f"je:auto:{invoice}:pay:0", "2026-09-01"),
        (f"je:auto:{invoice}:payrefund:refund_0_0", "2026-09-21"),
        (f"je:auto:{invoice}:payrefund:refund_0_1", "2026-09-22"),
        (f"je:auto:{invoice}:payrefund:refund_0_2", "2026-09-23"),
        (f"je:auto:{invoice}:payrefundrev:refund_0_1", "2026-09-24")]
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refunded") == 3


async def test_a_refund_is_dated_and_posted_on_the_books_its_payment_was_recorded_on(
        real_engine, real_client, monkeypatch):
    from test_company_reset_payments import _company_settings
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    await _company_settings(real_engine, a, timezone="Pacific/Kiritimati", stripe_deposit_account="1111")
    cloud.refund(a, invoice, "re_1", 20000, datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="200")  # back from 1110, not 1111
    assert (f"je:auto:{invoice}:payrefund:refund_0_0", "2026-09-03") in await _journal(real_engine, invoice)


async def test_a_refund_larger_than_what_is_left_of_the_payment_is_kept_whole_and_not_cut_down(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 100000, _at(1))
    cloud.refund(a, invoice, "re_2", 10000, _at(2))
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="1000")
    assert await _kept_refunds(real_engine) == [("re_2", 1, "applied", "pi_1", 10000, str(a), invoice)]


# ── A refund Stripe reverses ─────────────────────────────────────────────────

async def test_a_refund_reversed_after_it_was_applied_restores_the_payment_once(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.refund(a, invoice, "re_1", 20000, _at(5), transition="reversed")
    await cloud.deliver()
    cloud.deliveries[-1]["acked"] = False
    await cloud.deliver()  # the reversal again

    await _assert_books(real_engine, invoice, refunded="0")
    doc = await _doc(real_engine, invoice)
    assert doc["status"] == "paid" and _payment(doc)["status"] == "active"
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refund_reversed") == 1
    assert (f"je:auto:{invoice}:payrefundrev:refund_0_0", "2026-09-01") in await _journal(real_engine, invoice)
    assert await _kept_refunds(real_engine) == []


async def test_a_reversal_of_a_refund_never_applied_changes_nothing_until_the_refund_arrives(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    before = await _ledger(real_engine, invoice)
    cloud.refund(a, invoice, "re_1", 20000, _at(5), transition="reversed")
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _ledger(real_engine, invoice) == before
    await _assert_books(real_engine, invoice, refunded="0")
    assert await _kept_refunds(real_engine) == [("re_1", 1, "reversed", "pi_1", 20000, str(a), invoice)]

    cloud.refund(a, invoice, "re_1", 20000, _at(1))  # the refund it reverses, delivered late
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="0")
    assert await _kept_refunds(real_engine) == []


async def test_a_refund_undone_and_put_through_again_gives_the_money_back_each_time_once(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.refund(a, invoice, "re_1", 20000, _at(2), transition="reversed")
    cloud.refund(a, invoice, "re_1", 20000, _at(3), cycle=2)
    await cloud.deliver()
    for d in cloud.deliveries:  # delivered again: nothing changes
        d["acked"] = False
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries) and await _kept_refunds(real_engine) == []
    await _assert_books(real_engine, invoice, refunded="200")
    ledger = await _ledger(real_engine, invoice)
    assert ledger.count("doc.payment.refunded") == 2 and ledger.count("doc.payment.refund_reversed") == 1

    cloud.refund(a, invoice, "re_1", 20000, _at(4), transition="reversed", cycle=2)
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="0")
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refund_reversed") == 2


async def test_a_reversal_waits_for_the_refund_of_its_own_cycle(real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.refund(a, invoice, "re_1", 20000, _at(4), transition="reversed", cycle=2)
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="200")
    assert await _kept_refunds(real_engine) == [("re_1", 2, "reversed", "pi_1", 20000, str(a), invoice)]


async def test_a_refund_with_no_time_it_happened_is_kept_not_posted(real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    before = await _ledger(real_engine, invoice)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.deliveries[-1]["occurred_at"] = None
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _ledger(real_engine, invoice) == before
    assert await _kept_refunds(real_engine) == [("re_1", 1, "applied", "pi_1", 20000, str(a), invoice)]


# ── Kept until its payment is on its invoice ─────────────────────────────────

async def test_a_refund_for_a_company_that_no_longer_exists_is_kept_with_its_payment(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(a), invoice)]
    assert await _kept_refunds(real_engine) == [("re_1", 1, "applied", "pi_1", 20000, str(a), invoice)]


async def test_refunds_delivered_before_their_payment_apply_in_order_when_it_is_recorded(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.refund(a, invoice, "re_b", 30000, _at(30))
    cloud.refund(a, invoice, "re_a", 20000, _at(1))
    await cloud.deliver()
    assert len(await _kept_refunds(real_engine)) == 2 and await _paid(real_engine, invoice) == []

    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="500")
    assert await _kept_refunds(real_engine) == []
    async with maker(real_engine)() as s:
        refunds = [(r["refund_id"], r["refund_number"]) for r in (await s.scalars(text(
            "SELECT data FROM ledger WHERE entity_id = :e AND event_type = 'doc.payment.refunded' ORDER BY id"),
            {"e": invoice})).all()]
    assert refunds == [("re_a", 0), ("re_b", 1)]


async def test_a_reset_and_recovery_replays_the_payment_then_its_refunds_into_exact_books(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    source = await backup_export.export_full()
    try:
        cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
        cloud.refund(a, invoice, "re_1", 20000, _at(1))
        cloud.refund(a, invoice, "re_2", 30000, _at(2))
        cloud.refund(a, invoice, "re_2", 30000, _at(3), transition="reversed")
        await cloud.deliver()
        await _assert_books(real_engine, invoice, refunded="200")
        books, journal = await _books(real_engine, invoice), await _journal(real_engine, invoice)

        assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
        cloud.refund(a, invoice, "re_3", 10000, _at(4))  # the company is gone: kept
        await cloud.deliver()
        assert await _kept_refunds(real_engine) == [("re_3", 1, "applied", "pi_1", 10000, str(a), invoice)]

        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert result.ok is True, result.error
    assert await _paid(real_engine, invoice) == []

    await cloud.deliver()  # Cloud delivers the payment and its refunds again, in order

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="300")
    assert await _kept_refunds(real_engine) == [] and await _unmatched(real_engine) == []
    assert set(journal) <= set(await _journal(real_engine, invoice))
    after_books, after_journal, after_ledger = (await _books(real_engine, invoice), await _journal(
        real_engine, invoice), await _ledger(real_engine, invoice))
    assert after_books["1110"] == books["1110"] - Decimal("100")

    for d in cloud.deliveries:  # and again: nothing changes
        d["acked"] = False
    await cloud.deliver()
    assert await _books(real_engine, invoice) == after_books
    assert await _journal(real_engine, invoice) == after_journal
    assert await _ledger(real_engine, invoice) == after_ledger


# ── Stripe holds the money ───────────────────────────────────────────────────

def _remove_payment(client, tok, eid, action):
    if action == "refund":
        return client.post(f"/docs/{eid}/refund", headers=tok, json={
            "payment_index": 0, "amount": 100.0, "payment_date": "2026-09-05"})
    if action == "void":
        return client.post(f"/docs/{eid}/void-payment", headers=tok, json={"payment_index": 0})
    return client.delete(f"/docs/{eid}/payments/0", headers=tok)


@pytest.mark.parametrize("action", ["refund", "void", "delete"])
async def test_a_stripe_payment_refunded_in_stripe_still_cannot_be_refunded_voided_or_deleted_here(
        real_engine, real_client, monkeypatch, action):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    await cloud.deliver()
    before, events = await _doc(real_engine, invoice), await _ledger(real_engine, invoice)

    r = await _remove_payment(real_client, auth(await token(real_engine, boss, a)), invoice, action)

    assert r.status_code == 422 and r.json()["detail"] == STRIPE_OWNED
    assert await _doc(real_engine, invoice) == before and await _ledger(real_engine, invoice) == events


# ── Stripe is disconnected: the payment is no longer linked to it ────────────

RELEASED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)


async def _notices(engine, company_id, entity_id) -> list[tuple[str, str]]:
    async with maker(engine)() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT title, priority FROM notifications WHERE company_id = :c AND action_url = :u"),
            {"c": company_id, "u": f"/docs/{entity_id}"})).all()]


async def _held_by(client, engine, boss, company, entity_id) -> list:
    r = await client.get(f"/docs/{entity_id}", headers=auth(await token(engine, boss, company)))
    assert r.status_code == 200, r.text
    return [p.get("held_by") for p in r.json()["payments"]]


@pytest.mark.parametrize("action", ["refund", "void", "delete"])
async def test_a_released_payment_is_refunded_voided_or_deleted_here(real_engine, real_client, monkeypatch, action):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    assert await _held_by(real_client, real_engine, boss, a, invoice) == ["stripe"]
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()
    assert await _held_by(real_client, real_engine, boss, a, invoice) == [None]

    r = await _remove_payment(real_client, auth(await token(real_engine, boss, a)), invoice, action)

    assert r.status_code == 200, r.text
    if action == "refund":
        await _assert_books(real_engine, invoice, refunded="100")
    else:
        doc = await _doc(real_engine, invoice)
        assert doc["amount_paid"] == 0 and doc["amount_outstanding"] == 1070.0
        assert await _books(real_engine, invoice) == {}


async def test_the_owner_is_told_once_that_a_payment_is_no_longer_linked_to_stripe(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()
    cloud.deliveries[-1]["acked"] = False  # the acknowledgement was lost
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert (await _ledger(real_engine, invoice)).count("doc.payment.stripe_released") == 1
    assert await _notices(real_engine, a, invoice) == [("A payment is no longer linked to Stripe", "high")]


async def test_a_refund_stripe_reports_for_a_released_payment_is_kept_not_posted(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()
    before = await _ledger(real_engine, invoice)
    cloud.refund(a, invoice, "re_1", 20000, _at(24 * 10))
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _ledger(real_engine, invoice) == before
    await _assert_books(real_engine, invoice, refunded="0")
    assert await _kept_refunds(real_engine) == [("re_1", 1, "applied", "pi_1", 20000, str(a), invoice)]


async def test_a_release_for_a_payment_not_recorded_yet_waits_for_it(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()
    assert [d["acked"] for d in cloud.deliveries] == [False]

    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
    await cloud.deliver()  # the payment lands; the release is still waiting
    assert [d["acked"] for d in cloud.deliveries] == [False, True]
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()
    assert await _held_by(real_client, real_engine, boss, a, invoice) == [None]


async def test_a_release_of_a_payment_kept_unmatched_applies_when_the_payment_lands_after_its_earlier_refunds(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    source = await backup_export.export_full()
    try:
        assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
        cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
        cloud.refund(a, invoice, "re_1", 20000, _at(1))
        cloud.release(a, invoice, RELEASED_AT)
        cloud.refund(a, invoice, "re_9", 5000, _at(24 * 12))  # after the release: never posted
        await cloud.deliver()
        assert all(d["acked"] for d in cloud.deliveries)
        async with maker(real_engine)() as s:
            assert await s.scalar(text("SELECT released_at FROM unmatched_payments")) == RELEASED_AT
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert result.ok is True, result.error

    cloud.deliveries[0]["acked"] = False  # Cloud delivers the payment again
    await cloud.deliver()

    assert await _unmatched(real_engine) == []
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()
    assert (await _ledger(real_engine, invoice)).count("doc.payment.stripe_released") == 1
    await _assert_books(real_engine, invoice, refunded="200")
    assert await _kept_refunds(real_engine) == [("re_9", 1, "applied", "pi_1", 5000, str(a), invoice)]


async def test_paid_refunded_in_stripe_disconnected_refunded_here_then_reconnected_counts_each_refund_once(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    """A payment is refunded in part in Stripe, Stripe is disconnected, the rest is
    refunded here, a System Recovery restore replays everything, and the same Stripe
    account is reconnected: every refund counts once."""
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    source = await backup_export.export_full()
    try:
        cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
        cloud.refund(a, invoice, "re_1", 20000, _at(1))
        cloud.release(a, invoice, RELEASED_AT)
        await cloud.deliver()
        r = await _remove_payment(real_client, auth(await token(real_engine, boss, a)), invoice, "refund")
        assert r.status_code == 200, r.text
        await _assert_books(real_engine, invoice, refunded="300")
        books, ledger = await _books(real_engine, invoice), await _ledger(real_engine, invoice)

        for d in cloud.deliveries:  # the same Stripe account is reconnected
            d["acked"] = False
        cloud.refund(a, invoice, "re_9", 5000, _at(24 * 12))  # refunded in Stripe after the release
        await cloud.deliver()
        assert all(d["acked"] for d in cloud.deliveries)
        assert await _books(real_engine, invoice) == books and await _ledger(real_engine, invoice) == ledger
        assert await _kept_refunds(real_engine) == [("re_9", 1, "applied", "pi_1", 5000, str(a), invoice)]

        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert result.ok is True, result.error
    for d in cloud.deliveries:  # Cloud delivers the payment, its refunds and its release again, in order
        d["acked"] = False
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="200")
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()
    assert await _held_by(real_client, real_engine, boss, a, invoice) == [None]
    r = await _remove_payment(real_client, auth(await token(real_engine, boss, a)), invoice, "refund")
    assert r.status_code == 200, r.text
    await _assert_books(real_engine, invoice, refunded="300")
    assert await _books(real_engine, invoice) == books


async def test_a_delivery_that_names_no_release_is_not_acknowledged(real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    for change in ({"reference": ""}, {"released_at": "yesterday"}, {"released_at": "2026-09-10T09:00:00"},
                   {"company_id": ""}, {"entity_id": ""}):
        cloud.release(a, invoice, RELEASED_AT)
        cloud.deliveries[-1].update(change)
    before = await _ledger(real_engine, invoice)

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries[1:]] == [False] * 5
    assert await _ledger(real_engine, invoice) == before


async def test_the_gateway_hands_a_release_to_the_release_intake_and_says_it_records_them(monkeypatch):
    from celerp.gateway.client import GatewayClient
    received = []

    async def receive(payload):
        received.append(payload)
        return True
    monkeypatch.setattr("celerp.services.payments.receive_release", receive)
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")

    await gateway._dispatch({"type": "invoice.payment_release", "payload": {"reference": "pi_1"}})
    await asyncio.gather(*gateway._bg_tasks)

    assert received == [{"reference": "pi_1"}]
    assert "invoice.payment_release" in GatewayClient._DELIVERIES


# ── Deliveries at once, and deliveries that name no refund ───────────────────

@pytest.mark.parametrize("transition", ["applied", "reversed"])
async def test_one_refund_delivered_twice_at_once_is_applied_once(real_engine, real_client, monkeypatch,
                                                                 transition):
    from celerp.services.payments import receive_refund
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    if transition == "reversed":
        cloud.refund(a, invoice, "re_1", 20000, _at(1))
        await cloud.deliver()
    cloud.refund(a, invoice, "re_1", 20000, _at(2), transition=transition)
    payload = {k: v for k, v in cloud.deliveries[-1].items() if k in _REFUND}

    assert await asyncio.gather(*(receive_refund(dict(payload)) for _ in range(4))) == [True] * 4

    await _assert_books(real_engine, invoice, refunded="200" if transition == "applied" else "0")
    assert (await _ledger(real_engine, invoice)).count(
        "doc.payment.refunded" if transition == "applied" else "doc.payment.refund_reversed") == 1
    assert await _kept_refunds(real_engine) == []


async def test_a_refund_and_its_payment_delivered_at_once_both_land(real_engine, real_client, monkeypatch):
    from celerp.services.payments import receive_payment, receive_refund
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    pay, refund = ({k: v for k, v in d.items() if k in keys}
                   for d, keys in zip(cloud.deliveries, (_PAYMENT, _REFUND)))

    assert await asyncio.gather(receive_refund(refund), receive_payment(pay)) == [True, True]

    await _assert_books(real_engine, invoice, refunded="200")
    assert await _kept_refunds(real_engine) == []


@pytest.mark.parametrize("change", [
    {"refund_id": ""}, {"reference": None}, {"transition": "pending"}, {"amount_minor": 0},
    {"amount_minor": "200"}, {"company_id": ""}, {"entity_id": ""}, {"cycle": 0}, {"cycle": "1"},
    {"cycle": True}, {"cycle": None}])
async def test_a_delivery_that_names_no_refund_is_not_acknowledged(real_engine, real_client, monkeypatch,
                                                                    change):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.deliveries[-1].update(change)
    before = await _ledger(real_engine, invoice)

    await cloud.deliver()

    assert cloud.deliveries[-1]["acked"] is False
    assert await _ledger(real_engine, invoice) == before and await _kept_refunds(real_engine) == []


async def test_a_refund_that_cannot_be_recorded_is_not_acknowledged(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.refund(b, "doc:gone", "re_1", 20000, _at(1))

    async def rename(old, new):
        async with real_engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {old} RENAME TO {new}"))
    await rename("unmatched_refunds", "unmatched_refunds_away")
    try:
        await cloud.deliver()
    finally:
        await rename("unmatched_refunds_away", "unmatched_refunds")

    assert [d["acked"] for d in cloud.deliveries] == [False]
    await cloud.deliver()
    assert [d["acked"] for d in cloud.deliveries] == [True]


async def test_the_gateway_hands_a_refund_delivery_to_the_refund_intake(monkeypatch):
    from celerp.gateway.client import GatewayClient
    received = []

    async def receive(payload):
        received.append(payload)
        return True
    monkeypatch.setattr("celerp.services.payments.receive_refund", receive)
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")

    await gateway._dispatch({"type": "invoice.refund", "payload": {"refund_id": "re_1"}})
    await asyncio.gather(*gateway._bg_tasks)

    assert received == [{"refund_id": "re_1"}]


async def test_the_installation_owner_sees_the_refunds_kept_for_later(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.refund(b, "doc:gone", "re_1", 20000, _at(1))
    await cloud.deliver()

    r = await real_client.get("/payments/unmatched", headers=auth(await token(real_engine, boss, a)))

    assert r.status_code == 200, r.text
    assert [{k: v for k, v in item.items() if k != "received_at"} for item in r.json()["refunds"]] == [{
        "refund_id": "re_1", "cycle": 1, "transition": "applied", "reference": "pi_1", "amount": 200.0, "currency": "USD",
        "company_id": str(b), "document_id": "doc:gone", "occurred_at": _at(1).isoformat()}]


# ── Hardening: the seams refunds open ────────────────────────────────────────

FX_BOOKS = {**BOOKS, "rate": "1.1"}


async def test_refunds_and_a_reversal_out_of_order_on_a_foreign_currency_invoice_leave_exact_books(
        real_engine, real_client, monkeypatch):
    """Each piece of the payment given back or restored converts what the payment's
    total refunded so far converts to, so the books always match what is refunded,
    whichever refund is reversed: refunding the whole payment leaves nothing behind."""
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a, currency="EUR", conversion_rate=1.1)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=FX_BOOKS)
    cloud.deliveries[-1]["currency"] = "eur"
    for refund_id, minor, hours, transition in [("re_a", 5, 1, "applied"), ("re_b", 5, 2, "applied"),
                                                ("re_a", 5, 3, "reversed"), ("re_c", 106995, 4, "applied")]:
        cloud.refund(a, invoice, refund_id, minor, _at(hours), transition=transition, books=FX_BOOKS)
        cloud.deliveries[-1]["currency"] = "eur"
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries) and await _kept_refunds(real_engine) == []
    doc = await _doc(real_engine, invoice)
    assert _payment(doc)["refunded"] == 1070.0 and doc["amount_paid"] == 0
    assert await _books(real_engine, invoice) == {}


async def test_a_refund_for_books_the_company_no_longer_keeps_is_kept_not_posted(
        real_engine, real_client, monkeypatch):
    from test_company_reset_payments import _company_settings
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    await _company_settings(real_engine, a, currency="EUR")
    before = await _ledger(real_engine, invoice)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    await cloud.deliver()

    assert await _ledger(real_engine, invoice) == before
    assert await _kept_refunds(real_engine) == [("re_1", 1, "applied", "pi_1", 20000, str(a), invoice)]


# ── Refunds Stripe made before the release, whenever they arrive ─────────────

async def test_a_refund_stripe_made_before_the_release_but_delivered_after_it_is_applied(
        real_engine, real_client, monkeypatch):
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.release(a, invoice, RELEASED_AT)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))  # made in Stripe before it was disconnected
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="200")
    assert await _kept_refunds(real_engine) == []
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()


async def test_a_reversal_kept_across_the_release_follows_its_refund_once(real_engine, real_client, monkeypatch):
    """Stripe undid a refund before it was disconnected; the reversal arrives first, then
    the release, then the refund it undoes: each applies once, in Stripe's order."""
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(2), transition="reversed")
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()
    assert await _kept_refunds(real_engine) == [("re_1", 1, "reversed", "pi_1", 20000, str(a), invoice)]

    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    await cloud.deliver()
    for d in cloud.deliveries:
        d["acked"] = False  # every acknowledgement was lost
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    await _assert_books(real_engine, invoice, refunded="0")
    ledger = await _ledger(real_engine, invoice)
    assert (ledger.count("doc.payment.refunded"), ledger.count("doc.payment.refund_reversed")) == (1, 1)
    assert await _kept_refunds(real_engine) == []


async def test_a_refund_kept_until_the_release_is_applied_by_it_once(real_engine, real_client, monkeypatch):
    """A refund Stripe made before the release that was kept for its payment (as a
    refund delivered after the release was by the version before this one) applies
    when the release is recorded, once however often the release is delivered."""
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    async with maker(real_engine)() as s:
        await s.execute(text(
            "INSERT INTO unmatched_refunds (refund_id, cycle, transition, reference, amount_minor, currency, "
            "former_company, document, occurred_at, context, received_at) VALUES ('re_1', 1, 'applied', "
            "'pi_1', 20000, 'USD', :c, :d, :at, CAST(:books AS json), now())"),
            {"c": str(a), "d": invoice, "at": _at(1), "books": json.dumps(BOOKS)})
        await s.commit()
    cloud.release(a, invoice, RELEASED_AT)
    await cloud.deliver()
    cloud.deliveries[-1]["acked"] = False
    await cloud.deliver()

    await _assert_books(real_engine, invoice, refunded="200")
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refunded") == 1
    assert await _kept_refunds(real_engine) == []
