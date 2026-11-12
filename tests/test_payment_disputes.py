# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A customer disputes a card payment with their bank. Celerp Cloud delivers each step of
the dispute (opened, the money taken from the account, the money given back, Stripe's
decision) as an invoice.dispute delivery, in order, after the payment. Each step is
recorded on the payment it disputes; the payment event is never deleted, so the invoice
stays paid. The money taken moves from the deposit account to Disputed payments, a won
dispute moves it back, and a lost one moves it to Payment dispute losses, each once, as an
automatic journal entry the company is told about. A delivery that names another company,
an invoice that is not there or does not hold the payment, another currency or more than
the payment posts nothing."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import BOOKS, _harbor, _invoice, _paid
from test_stripe_refunds import _RefundCloud, _deliver_one, _doc, _ledger, _payment
from test_system_recovery_exact import _migration_history  # noqa: F401

pytestmark = pytest.mark.asyncio

PAID_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
_DISPUTE = ("company_id", "entity_id", "reference", "dispute_id", "state", "outcome", "amount_minor",
            "currency", "occurred_at", "context", "delivery_id")
DISPUTED, LOSSES, BANK = "1170", "6945", "1111"


def _at(days: int) -> datetime:
    return PAID_AT + timedelta(days=days)


class _DisputeCloud(_RefundCloud):
    def dispute(self, company_id, entity_id: str, state: str, occurred_at: datetime | None, *,
                outcome: str | None = None, amount_minor: int = 107000, currency: str = "usd",
                dispute_id: str = "dp_1", reference: str = "pi_1") -> None:
        self.deliveries.append({"kind": "dispute", "company_id": str(company_id), "entity_id": entity_id,
                                "reference": reference, "dispute_id": dispute_id, "state": state,
                                "outcome": outcome, "amount_minor": amount_minor, "currency": currency,
                                "occurred_at": occurred_at.isoformat() if occurred_at else None,
                                "context": BOOKS, "delivery_id": str(uuid.uuid4()), "acked": False})

    async def deliver(self) -> None:
        for d in [d for d in self.deliveries if not d["acked"]]:
            d["acked"] = await (_deliver_dispute(d) if d.get("kind") == "dispute" else _deliver_one(d))


async def _deliver_dispute(d: dict) -> bool:
    from celerp.gateway.client import GatewayClient
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")
    gateway._ws = object()
    acked = []

    async def send(ws, message):
        acked.append(message["payload"]["delivery_id"])
    gateway._send = send
    await gateway._handle_invoice_dispute({k: v for k, v in d.items() if k in _DISPUTE})
    return acked == [d["delivery_id"]]


async def _paid_invoice(engine, client, monkeypatch):
    """A company whose invoice of 1,070.00 USD a customer paid online."""
    boss, a, b = await _harbor(engine)
    invoice = await _invoice(client, engine, boss, a)
    cloud = _DisputeCloud(monkeypatch, engine)
    cloud.pay(a, invoice, "pi_1", paid_at=PAID_AT, books=BOOKS)
    await cloud.deliver()
    assert await _paid(engine, invoice) == [("pi_1", 1070.0)]
    return a, invoice, cloud


async def _dispute_entries(engine, entity_id) -> list[tuple[str, str, dict]]:
    """(journal entry, day, net debit per account) of each posted dispute entry of the invoice."""
    async with maker(engine)() as s:
        rows = (await s.execute(text("SELECT entity_id, state FROM projections WHERE entity_id LIKE :j"),
                                {"j": f"je:auto:{entity_id}:dispute:%"})).all()
    out = []
    for entity, je in sorted(rows):
        if je.get("status") != "posted":
            continue
        net: dict[str, Decimal] = {}
        for line in je["entries"]:
            net[line["account"]] = (net.get(line["account"], Decimal(0)) + Decimal(str(line.get("debit") or 0))
                                    - Decimal(str(line.get("credit") or 0)))
        out.append((entity.rsplit(":", 1)[-1], str(je.get("ts"))[:10], {k: v for k, v in net.items() if v}))
    return out


async def _notices(engine, company_id) -> list[str]:
    async with maker(engine)() as s:
        return list((await s.scalars(text(
            "SELECT title FROM notifications WHERE company_id = :c AND title LIKE '%dispute%' ORDER BY created_at"),
            {"c": str(company_id)})).all())


async def test_dispute_delivery_posts_auditable_adjustment(real_engine, real_client, monkeypatch):
    """Opened, the dispute is shown on the payment and posts nothing; the money taken moves from
    the deposit account to Disputed payments, once however often it is delivered. The payment
    stays on the invoice, which stays paid, and the company is told."""
    a, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.dispute(a, invoice, "created", _at(3))
    await cloud.deliver()
    payment = _payment(await _doc(real_engine, invoice))
    assert payment["disputes"]["dp_1"]["state"] == "created"
    assert await _dispute_entries(real_engine, invoice) == []

    cloud.dispute(a, invoice, "funds_withdrawn", _at(4))
    await cloud.deliver()
    cloud.deliveries[-1]["acked"] = False  # the acknowledgement was lost
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _dispute_entries(real_engine, invoice) == [
        ("funds_withdrawn", "2026-09-05", {DISPUTED: Decimal("1070"), BANK: Decimal("-1070")})]
    doc = await _doc(real_engine, invoice)
    assert doc["status"] == "paid" and _payment(doc)["status"] == "active"
    assert _payment(doc)["disputes"]["dp_1"]["state"] == "funds_withdrawn"
    ledger = await _ledger(real_engine, invoice)
    assert ledger.count("doc.payment.received") == 1 and ledger.count("doc.payment.disputed") == 2
    assert len(await _notices(real_engine, a)) == 2


async def test_dispute_won_reinstates(real_engine, real_client, monkeypatch):
    """A won dispute gives the money back: the reinstatement mirrors the withdrawal and the
    decision posts nothing more."""
    a, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.dispute(a, invoice, "created", _at(3))
    cloud.dispute(a, invoice, "funds_withdrawn", _at(3))
    cloud.dispute(a, invoice, "funds_reinstated", _at(40))
    cloud.dispute(a, invoice, "closed", None, outcome="won")
    await cloud.deliver()

    assert all(d["acked"] for d in cloud.deliveries)
    assert await _dispute_entries(real_engine, invoice) == [
        ("funds_reinstated", "2026-10-11", {DISPUTED: Decimal("-1070"), BANK: Decimal("1070")}),
        ("funds_withdrawn", "2026-09-04", {DISPUTED: Decimal("1070"), BANK: Decimal("-1070")})]
    dispute = _payment(await _doc(real_engine, invoice))["disputes"]["dp_1"]
    assert (dispute["state"], dispute["outcome"]) == ("closed", "won")


async def test_dispute_lost_moves_the_money_to_dispute_losses(real_engine, real_client, monkeypatch):
    a, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.dispute(a, invoice, "created", _at(3))
    cloud.dispute(a, invoice, "funds_withdrawn", _at(3))
    cloud.dispute(a, invoice, "closed", _at(60), outcome="lost")
    await cloud.deliver()

    assert await _dispute_entries(real_engine, invoice) == [
        ("funds_withdrawn", "2026-09-04", {DISPUTED: Decimal("1070"), BANK: Decimal("-1070")}),
        ("lost", "2026-10-31", {LOSSES: Decimal("1070"), DISPUTED: Decimal("-1070")})]
    doc = await _doc(real_engine, invoice)
    assert doc["status"] == "paid" and _payment(doc)["disputes"]["dp_1"]["outcome"] == "lost"


async def test_dispute_deliveries_validated_at_app(real_engine, real_client, monkeypatch):
    """Another company, an invoice that is not there, another currency or more than the payment:
    acknowledged and logged, nothing posted or recorded on the payment. One without its fields is
    not taken."""
    a, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.dispute(uuid.uuid4(), invoice, "funds_withdrawn", _at(3))
    cloud.dispute(a, "doc:not-there", "funds_withdrawn", _at(3))
    cloud.dispute(a, invoice, "funds_withdrawn", _at(3), currency="eur", dispute_id="dp_eur")
    cloud.dispute(a, invoice, "funds_withdrawn", _at(3), amount_minor=107001, dispute_id="dp_big")
    await cloud.deliver()
    assert all(d["acked"] for d in cloud.deliveries)
    assert await _dispute_entries(real_engine, invoice) == []
    assert not _payment(await _doc(real_engine, invoice)).get("disputes")

    from celerp.services.payments import receive_dispute
    assert await receive_dispute({"company_id": str(a), "entity_id": invoice, "reference": "pi_1"}) is False
    assert await receive_dispute({"company_id": str(a), "entity_id": invoice, "reference": "pi_1",
                                  "dispute_id": "dp_1", "state": "bogus", "amount_minor": 1,
                                  "currency": "usd"}) is False
