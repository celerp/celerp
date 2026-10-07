# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A customer who opened a payment page before the upgrade pays after it. Celerp
Cloud delivers the payment as it delivers every payment taken on such a page: with
no books and ``managed`` false (or, from a Celerp Cloud not yet upgraded, with
neither, and no time it was paid). It is recorded once, on the books the invoice is
paid on now, and is the company's to manage like a payment entered by hand: Stripe
never sends its refunds, so it is refunded, voided or deleted here."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from company_backup_support import token
from migration_support import auth, code_config, real_client, real_engine  # noqa: F401
from test_company_reset_payments import _Cloud, _harbor, _invoice, _paid
from test_stripe_refunds import _RefundCloud, _deliver_one, _doc, _kept_refunds, _ledger

pytestmark = pytest.mark.asyncio

PAID_AT = "2026-09-01T09:00:00+00:00"


def _released_page_payment(company_id, entity_id: str) -> dict:
    """The payment as Celerp Cloud delivers one taken on a page opened before the upgrade."""
    return {"company_id": str(company_id), "entity_id": entity_id, "amount_minor": 107000, "currency": "usd",
            "reference": "pi_7", "paid_at": PAID_AT, "occurred_at": PAID_AT, "context": None,
            "managed": False, "delivery_id": str(uuid.uuid4())}


async def _deliver(payload: dict) -> bool:
    from celerp.gateway.client import GatewayClient
    gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")
    gateway._ws = object()
    acked = []

    async def send(ws, message):
        acked.append(message["payload"]["delivery_id"])
    gateway._send = send
    await gateway._handle_invoice_payment(dict(payload))
    return acked == [payload["delivery_id"]]


async def _held_by(client, engine, boss, company, entity_id) -> list:
    r = await client.get(f"/docs/{entity_id}", headers=auth(await token(engine, boss, company)))
    assert r.status_code == 200, r.text
    return [p.get("held_by") for p in r.json()["payments"]]


async def test_a_page_opened_before_the_upgrade_and_paid_after_is_recorded_once_for_the_company(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    _Cloud(monkeypatch, real_engine)
    payment = _released_page_payment(a, invoice)

    assert await _deliver(payment) is True
    assert await _deliver({**payment, "delivery_id": str(uuid.uuid4())}) is True  # Stripe reported it twice

    assert await _paid(real_engine, invoice) == [("pi_7", 1070.0)]
    (recorded,) = (await _doc(real_engine, invoice))["payments"]
    assert (recorded["bank_account"], recorded["payment_date"], recorded["method"]) == (
        "1110", "2026-09-01", "stripe")
    assert await _held_by(real_client, real_engine, boss, a, invoice) == [None]
    r = await real_client.post(f"/docs/{invoice}/void-payment", json={"payment_index": recorded["index"]},
                               headers=auth(await token(real_engine, boss, a)))
    assert r.status_code == 200, r.text
    assert (await _doc(real_engine, invoice))["amount_outstanding"] == 1070.0


async def test_a_payment_from_a_celerp_cloud_not_yet_upgraded_is_recorded_for_the_company(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    _Cloud(monkeypatch, real_engine)
    payment = {"company_id": str(a), "entity_id": invoice, "amount_minor": 107000, "currency": "usd",
               "reference": "pi_7", "delivery_id": str(uuid.uuid4())}

    assert await _deliver(payment) is True

    assert await _paid(real_engine, invoice) == [("pi_7", 1070.0)]
    assert await _held_by(real_client, real_engine, boss, a, invoice) == [None]


async def test_a_refund_named_for_a_payment_the_company_manages_is_kept_not_posted(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _RefundCloud(monkeypatch, real_engine)
    assert await _deliver(_released_page_payment(a, invoice)) is True
    before = await _ledger(real_engine, invoice)

    cloud.refund(a, invoice, "re_1", 20000, datetime.fromisoformat(PAID_AT),
                 reference="pi_7")
    assert await _deliver_one(cloud.deliveries[-1]) is True

    assert await _ledger(real_engine, invoice) == before
    assert [r[0] for r in await _kept_refunds(real_engine)] == ["re_1"]
