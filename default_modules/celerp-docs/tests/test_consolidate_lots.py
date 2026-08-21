# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Consolidation of a splittable SKU's lots for a forward sales doc.

A fungible product that is produced or received as discrete lots keeps its catalog
row at quantity 0 while stock lives in newer lots. When such a SKU is sold, the
single consolidated option must bind to a lot that actually holds stock, not to the
empty pick-first row - otherwise fulfillment rejects the line despite in-stock lots.
"""
from __future__ import annotations

from ui.routes.documents import _consolidate_sales_lots


def _lot(entity_id: str, sku: str, created_at: str, quantity: float) -> dict:
    return {
        "entity_id": entity_id,
        "sku": sku,
        "created_at": created_at,
        "quantity": quantity,
        "allow_splitting": True,
    }


def test_consolidate_prefers_stocked_lot() -> None:
    """FIFO binds the pick-first lot WITH stock, not the empty older catalog row."""
    empty_old = _lot("item:catalog", "WIDGET", "2026-01-01T00:00:00", 0)
    stocked_new = _lot("item:lot-a", "WIDGET", "2026-02-01T00:00:00", 5)

    out = _consolidate_sales_lots([empty_old, stocked_new], {})

    assert len(out) == 1
    rep = out[0]
    assert rep["entity_id"] == "item:lot-a"  # the lot that holds stock, not the empty row
    assert rep["quantity"] == 5  # aggregate on-hand across the SKU's lots


def test_consolidate_all_empty_keeps_pick_first() -> None:
    """Genuine out of stock: every lot empty, the pick-first row is bound and 0 is honest."""
    empty_old = _lot("item:catalog", "WIDGET", "2026-01-01T00:00:00", 0)
    empty_new = _lot("item:lot-a", "WIDGET", "2026-02-01T00:00:00", 0)

    out = _consolidate_sales_lots([empty_old, empty_new], {})

    assert len(out) == 1
    assert out[0]["entity_id"] == "item:catalog"
    assert out[0]["quantity"] == 0
