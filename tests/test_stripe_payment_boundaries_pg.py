# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online payments, their refunds and their release meet the same record and access
checks as every other write.

- A delivery that names an item, not an invoice, changes nothing on the item: the
  payment and the refund are kept with the unmatched payments, the release is not
  recorded.
- A refund entered here and a release that applies a refund Stripe made earlier reach
  the invoice at once: whichever goes first, Stripe's refund lands once and the one
  entered here is judged on what is left, so no more is given back than was paid.
- A payment taken on a page opened before the upgrade is the company's to manage, and
  voiding it is judged by the access its user holds once it reaches the company, as
  any other write is.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import member, owner, token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_company_reset_payments import BOOKS, _harbor, _invoice, _paid, _unmatched
from test_stripe_legacy_payments import _deliver, _released_page_payment
from test_stripe_refunds import (RELEASED_AT, STRIPE_OWNED, _assert_books, _at, _deliver_one, _doc, _kept_refunds,
                                 _ledger, _paid_invoice, _payment, _RefundCloud)
from test_stripe_release_race_pg import _waiting

pytestmark = pytest.mark.asyncio

_ITEM = "item:boundary"


async def _an_item(engine, company_id) -> None:
    from celerp.events.engine import emit_event
    async with maker(engine)() as s:
        await emit_event(s, company_id=company_id, entity_id=_ITEM, entity_type="item", event_type="item.created",
                         data={"sku": "BND-1", "name": "Boundary", "quantity": 3, "sell_by": "piece",
                               "status": "available", "cost_price": 1, "retail_price": 2},
                         actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
        await s.commit()


async def _record(engine, entity_id) -> tuple[dict, list[str]]:
    async with maker(engine)() as s:
        state = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})
    return state, await _ledger(engine, entity_id)


# ── A delivery naming an item ────────────────────────────────────────────────

@pytest.mark.parametrize("managed", [True, False], ids=["managed", "released-page"])
async def test_a_payment_delivered_for_an_item_is_kept_and_leaves_the_item_alone(
        real_engine, real_client, monkeypatch, managed):
    boss, a, b = await _harbor(real_engine)
    await _an_item(real_engine, a)
    before = await _record(real_engine, _ITEM)
    _RefundCloud(monkeypatch, real_engine)
    payment = _released_page_payment(a, _ITEM)
    if managed:
        payment.update(managed=True, context=BOOKS)

    assert await _deliver(payment) is True

    assert await _record(real_engine, _ITEM) == before
    assert await _unmatched(real_engine) == [("pi_7", 107000, "USD", str(a), _ITEM)]


async def test_a_refund_delivered_for_an_item_is_kept_and_leaves_the_item_alone(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    await _an_item(real_engine, a)
    before = await _record(real_engine, _ITEM)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.refund(a, _ITEM, "re_1", 20000, _at(1))

    assert await _deliver_one(cloud.deliveries[-1]) is True

    assert await _record(real_engine, _ITEM) == before
    assert [r[0] for r in await _kept_refunds(real_engine)] == ["re_1"]


async def test_a_release_delivered_for_an_item_is_not_recorded(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    await _an_item(real_engine, a)
    before = await _record(real_engine, _ITEM)
    cloud = _RefundCloud(monkeypatch, real_engine)
    cloud.release(a, _ITEM, RELEASED_AT)

    assert await _deliver_one(cloud.deliveries[-1]) is False  # delivered again later, never on the item

    assert await _record(real_engine, _ITEM) == before


# ── A refund here racing the release that applies Stripe's earlier refund ────

@pytest.mark.parametrize("release_first", [True, False], ids=["release-first", "refund-here-first"])
async def test_a_refund_here_and_a_release_applying_stripes_refund_never_give_back_more_than_was_paid(
        real_engine, real_client, monkeypatch, release_first):
    from celerp.services.payments import receive_release
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    async with maker(real_engine)() as s:  # Stripe's refund of 200.00, made before the release, kept for it
        await s.execute(text(
            "INSERT INTO unmatched_refunds (refund_id, cycle, transition, reference, amount_minor, currency, "
            "former_company, document, occurred_at, context, received_at) VALUES ('re_1', 1, 'applied', "
            "'pi_1', 20000, 'USD', :c, :d, :at, CAST(:books AS json), now())"),
            {"c": str(a), "d": invoice, "at": _at(1), "books": json.dumps(BOOKS)})
        await s.commit()
    tok = auth(await token(real_engine, boss, a))
    release = {"company_id": str(a), "entity_id": invoice, "reference": "pi_1",
               "released_at": RELEASED_AT.isoformat()}

    index = _payment(await _doc(real_engine, invoice))["index"]

    def refund_here():
        return real_client.post(f"/docs/{invoice}/refund", headers=tok, json={
            "payment_index": index, "amount": 900.0, "payment_date": "2026-09-11"})

    first, second = (lambda: receive_release(dict(release))), refund_here
    if not release_first:
        first, second = second, first

    async with real_engine.connect() as watch, maker(real_engine)() as editing:
        # Someone is editing the invoice; each write queues behind them in turn.
        await editing.execute(text("SELECT 1 FROM projections WHERE entity_id = :e FOR UPDATE"), {"e": invoice})
        one = asyncio.create_task(first())
        await _waiting(watch, 1)
        two = asyncio.create_task(second())
        await _waiting(watch, 2)
        await editing.commit()
    outcomes = await asyncio.gather(one, two)
    released, here = outcomes if release_first else outcomes[::-1]

    assert released is True
    # Release first: 870.00 is left, so 900.00 is too much. Refund first: Stripe still holds it.
    assert here.status_code == 422, here.text
    assert here.json()["detail"] == (
        "At most 870.0 USD of this payment can still be refunded." if release_first else STRIPE_OWNED)
    await _assert_books(real_engine, invoice, refunded="200")
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refunded") == 1
    assert await _kept_refunds(real_engine) == []


# ── Voiding a payment from a page opened before the upgrade ──────────────────

async def _legacy_paid_invoice(engine, client, monkeypatch):
    """Company A's invoice paid on a page opened before the upgrade, and a manager of A."""
    boss, a, b = await _harbor(engine)
    invoice = await _invoice(client, engine, boss, a)
    _RefundCloud(monkeypatch, engine)
    assert await _deliver(_released_page_payment(a, invoice)) is True
    manager = await owner(engine, "manager@example.test", "Manager")
    await member(engine, manager, a, "manager")
    return boss, a, invoice, manager


def _revoke_record_payments(engine, company_id):
    async def _go():
        from celerp.routers import companies
        async with maker(engine)() as s:  # the owner's change, through the handler an owner uses
            return await companies.patch_role_permissions(
                companies.RolePermissionPatch(perm_key="record_payments", role_key="manager", granted=False),
                company_id=company_id, _=None, session=s)
    return _go


@pytest.mark.parametrize("revoked_first", [True, False], ids=["revocation-first", "void-first"])
async def test_voiding_a_payment_from_before_the_upgrade_is_judged_by_access_held_at_the_lock(
        real_engine, real_client, monkeypatch, revoked_first):
    boss, a, invoice, manager = await _legacy_paid_invoice(real_engine, real_client, monkeypatch)
    index = (await _doc(real_engine, invoice))["payments"][0]["index"]
    tok = auth(await token(real_engine, manager, a, "manager"))
    before = await _record(real_engine, invoice)

    def void():
        return real_client.post(f"/docs/{invoice}/void-payment", headers=tok, json={"payment_index": index})

    revoke = _revoke_record_payments(real_engine, a)
    first, second = (revoke, void) if revoked_first else (void, revoke)
    async with real_engine.connect() as watch, maker(real_engine)() as editing:
        # Someone holds the company; the two writes queue behind them in turn.
        await editing.execute(text("SELECT 1 FROM companies WHERE id = :c FOR NO KEY UPDATE"), {"c": a})
        one = asyncio.create_task(first())
        await _waiting(watch, 1)
        two = asyncio.create_task(second())
        await _waiting(watch, 2)
        await editing.commit()
    outcomes = await asyncio.gather(one, two)
    voided = outcomes[1] if revoked_first else outcomes[0]

    if revoked_first:
        assert voided.status_code == 403, voided.text
        assert await _record(real_engine, invoice) == before
        assert await _paid(real_engine, invoice) == [("pi_7", 1070.0)]
    else:
        assert voided.status_code == 200, voided.text
        assert (await _doc(real_engine, invoice))["amount_outstanding"] == 1070.0
