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


def test_qty_each_piece_lot_is_one():
    """A piece-denominated lot has a per-piece measure of 1: the quantity IS the piece
    count, so dividing it by its own pieces is wrong. The unit map identifies the lot's
    sell unit as a pieces unit; an empty lot reports 0 with no divide-by-zero."""
    unit_map = {"piece": {"name": "piece", "unit_type": "pieces"}}
    lot = flatten_item({"quantity": 5, "sell_by": "piece"}, "itm_1", unit_map=unit_map)
    assert lot["qty_each"] == 1
    empty = flatten_item({"quantity": 0, "sell_by": "piece"}, "itm_2", unit_map=unit_map)
    assert empty["qty_each"] == 0


def test_qty_each_non_piece_lot_falls_back_to_quantity():
    """Regression guard: without a pieces unit, a lot whose pieces attribute is 1,
    missing, or empty reports the full quantity with no divide-by-zero. Green at
    merge-base; it guards the weight/carat path against over-division."""
    single = flatten_item({"quantity": 5, "sell_by": "carat", "attributes": {"pieces": 1}}, "itm_1")
    assert single["qty_each"] == 5
    missing = flatten_item({"quantity": 5, "sell_by": "carat"}, "itm_2")
    assert missing["qty_each"] == 5
    empty = flatten_item({"quantity": 5, "sell_by": "carat", "attributes": {"pieces": ""}}, "itm_3")
    assert empty["qty_each"] == 5
