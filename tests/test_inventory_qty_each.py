# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the derived per-piece measure (`qty_each`) that flatten_item
computes alongside quantity, so a weight or carat lot exposes its per-stone value
to search and display.
"""
from __future__ import annotations

from celerp_inventory.routes import flatten_item


def test_qty_each_carat_lot():
    """A 6 carat box of 4 stones has a per-stone measure of 1.5, computed as
    quantity divided by pieces."""
    flat = flatten_item({"quantity": 6, "sell_by": "carat", "attributes": {"pieces": 4}}, "itm_1")
    assert flat["qty_each"] == 1.5


def test_qty_each_single_or_missing_pieces_no_div():
    """A one-piece lot, a lot with no pieces, and a lot with empty pieces all fall
    back to the full quantity with no divide-by-zero."""
    single = flatten_item({"quantity": 5, "sell_by": "piece", "attributes": {"pieces": 1}}, "itm_1")
    assert single["qty_each"] == 5
    missing = flatten_item({"quantity": 5, "sell_by": "piece"}, "itm_2")
    assert missing["qty_each"] == 5
    empty = flatten_item({"quantity": 5, "sell_by": "piece", "attributes": {"pieces": ""}}, "itm_3")
    assert empty["qty_each"] == 5
