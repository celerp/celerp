# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refund Stripe made before it was disconnected and the release of its payment
reach the invoice at the same moment: whichever takes the invoice first, the books
end the same, with the refund given back once and the payment released."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from migration_support import code_config, maker, real_client, real_engine  # noqa: F401
from test_stripe_refunds import (_REFUND, _RELEASE, RELEASED_AT, _assert_books, _at, _doc, _kept_refunds,
                                 _ledger, _paid_invoice, _payment)

pytestmark = pytest.mark.asyncio


async def _waiting(watch, count: int) -> None:
    for _ in range(400):
        waiting = await watch.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock'"))
        await watch.rollback()  # the activity view holds still for the length of a transaction
        if waiting >= count:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"{count} deliveries never waited for the invoice")


@pytest.mark.parametrize("release_first", [True, False], ids=["release-first", "refund-first"])
async def test_a_refund_and_the_release_reaching_the_invoice_at_once_leave_the_same_books(
        real_engine, real_client, monkeypatch, release_first):
    from celerp.services.payments import receive_refund, receive_release
    boss, a, b, invoice, cloud = await _paid_invoice(real_engine, real_client, monkeypatch)
    cloud.refund(a, invoice, "re_1", 20000, _at(1))
    cloud.release(a, invoice, RELEASED_AT)
    refund, release = ({k: v for k, v in d.items() if k in keys}
                       for d, keys in zip(cloud.deliveries[-2:], (_REFUND, _RELEASE)))
    first, second = ((receive_release, release), (receive_refund, refund))
    if not release_first:
        first, second = second, first

    # Connections taken up front, so watching never waits for one the deliveries hold.
    async with real_engine.connect() as watch, maker(real_engine)() as editing:
        # Someone is editing the invoice; each delivery queues behind them in turn.
        await editing.execute(text("SELECT 1 FROM projections WHERE entity_id = :e FOR UPDATE"), {"e": invoice})
        one = asyncio.create_task(first[0](dict(first[1])))
        await _waiting(watch, 1)
        two = asyncio.create_task(second[0](dict(second[1])))
        await _waiting(watch, 2)
        await editing.commit()
    assert await asyncio.gather(one, two) == [True, True]

    await _assert_books(real_engine, invoice, refunded="200")
    assert _payment(await _doc(real_engine, invoice))["stripe_released_at"] == RELEASED_AT.isoformat()
    assert (await _ledger(real_engine, invoice)).count("doc.payment.refunded") == 1
    assert await _kept_refunds(real_engine) == []
