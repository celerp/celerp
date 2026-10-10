# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A finalized invoice's cost claim is money: its cost is allocated once, to the cent.

The finalize entry posts each line's cost rounded to the currency's cents, and the claim
the invoice holds on goods it has not shipped is that posted figure, never the unrounded
cost it came from. Two invoices each holding one unit of a lot of 7 at 100.00 post 14.29
each and claim 14.29 each, so the books and the stock they hold agree to the cent through
holding, shipping, void and unvoid.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.services.auto_je import unshipped_claims
from stock_books import assert_settled
from test_invoice_unshipped_books import _invoice, _lot, _ok
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio


async def _gl(session, auth, code: str) -> float:
    return round(await _account_net(session, auth["company_id"], code), 2)


async def _two_holds(client, session, auth) -> tuple[str, str, str]:
    sku = f"CNT-{uuid.uuid4().hex[:6]}"
    lot = await _lot(client, auth, sku, 7, 100.0)
    first = await _invoice(client, auth, lot, sku, 1)
    second = await _invoice(client, auth, lot, sku, 1)
    return lot, first, second


async def test_two_holds_on_an_unrounded_unit_cost_claim_what_was_posted(client, session, auth):
    """Lot 7 at 100.00, one unit held for each of two invoices: each posts 14.29, each
    claim holds 14.29, and the books carry 71.42 for the 71.42 of stock not claimed."""
    lot, first, second = await _two_holds(client, session, auth)
    assert await _gl(session, auth, "5100") == 28.58
    assert await _gl(session, auth, "1130-OB") == 71.42
    claims = {c.doc_id: c.amount for c in await unshipped_claims(session, auth["company_id"]) if c.lot_id == lot}
    assert {k: round(v, 6) for k, v in claims.items()} == {first: 14.29, second: 14.29}, claims
    await assert_settled(client, session, auth)


async def test_shipping_held_goods_keeps_the_books_on_the_stock(client, session, auth):
    """Neighbour: each invoice ships its unit in turn; the books follow the stock to the cent
    at every step, and the lot's remaining 5 units carry what the books carry."""
    lot, first, second = await _two_holds(client, session, auth)
    await _ok(client, auth, f"/docs/{first}/fulfill-lines", {"line_entity_ids": [lot]})
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{second}/fulfill-lines", {"line_entity_ids": [lot]})
    await assert_settled(client, session, auth)
    assert round(await _gl(session, auth, "5100") + await _gl(session, auth, "1130-OB"), 2) == 100.0


async def test_void_and_unvoid_of_a_held_invoice_restore_the_cents(client, session, auth):
    """Neighbour: voiding one holder returns exactly its 14.29, unvoid takes it again."""
    lot, first, second = await _two_holds(client, session, auth)
    await _ok(client, auth, f"/docs/{second}/void")
    assert (await _gl(session, auth, "5100"), await _gl(session, auth, "1130-OB")) == (14.29, 85.71)
    await assert_settled(client, session, auth)
    await _ok(client, auth, f"/docs/{second}/unvoid")
    assert (await _gl(session, auth, "5100"), await _gl(session, auth, "1130-OB")) == (28.58, 71.42)
    await assert_settled(client, session, auth)


async def test_a_line_over_two_lots_posts_its_rounded_cost_once(client, session, auth):
    """Neighbour: one line of 4 over a lot of 3 at 10.00 and a lot of 3 at 20.00 takes 3 from
    the first and 1 from the second: each claim is whole cents and the claims sum to the
    16.67 the line posted."""
    sku = f"CNT-{uuid.uuid4().hex[:6]}"
    a = await _lot(client, auth, sku, 3, 10.0)
    b = await _lot(client, auth, sku, 3, 20.0)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}",
        "line_items": [{"entity_id": a, "sku": sku, "name": "Lot", "quantity": 4,
                        "unit_price": 40.0, "line_total": 160.0}], "total": 160.0})
    assert r.status_code == 200, r.text
    await _ok(client, auth, f"/docs/{r.json()['id']}/finalize")
    claims = [c for c in await unshipped_claims(session, auth["company_id"]) if c.lot_id in (a, b)]
    assert all(round(c.amount, 2) == c.amount for c in claims), claims
    assert round(sum(c.amount for c in claims), 2) == await _gl(session, auth, "5100")
    await assert_settled(client, session, auth)
