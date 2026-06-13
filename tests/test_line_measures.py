# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the shared doc-line measure rendering."""

from __future__ import annotations

from celerp.services.line_measures import (
    item_measure_meta,
    measure_locks,
    measure_sublines,
    qty_label,
    resolve_line_measures,
)
from celerp.services.units import DEFAULT_UNITS, build_unit_map

UM = build_unit_map(DEFAULT_UNITS)


def test_skip_pieces_for_piece_sold_item():
    # qty IS the pieces → no pieces sub-line; no weight stored → no weight line.
    line = {"unit": "piece", "quantity": 2, "pieces": 2}
    assert measure_sublines(line, unit_map=UM) == []


def test_skip_weight_for_weight_sold_item_but_show_pieces():
    line = {"unit": "carat", "quantity": 4.5, "pieces": 3, "weight": 4.5, "weight_unit": "carat"}
    assert measure_sublines(line, unit_map=UM) == ["- 3 pcs."]


def test_generic_unit_shows_both():
    line = {"unit": "liter", "quantity": 2, "pieces": 3, "weight": 4.5, "weight_unit": "gram"}
    assert measure_sublines(line, unit_map=UM) == ["- 3 pcs.", "- 4.5 gram"]


def test_weight_without_unit_has_no_trailing_space():
    line = {"unit": "liter", "quantity": 2, "weight": 4.5}
    assert measure_sublines(line, unit_map=UM) == ["- 4.5"]


def test_number_formatting_strips_trailing_zeros():
    line = {"unit": "liter", "quantity": 1, "pieces": 3.0, "weight": 4.50, "weight_unit": "gram"}
    assert measure_sublines(line, unit_map=UM) == ["- 3 pcs.", "- 4.5 gram"]


def test_fallback_to_parcel_when_line_lacks_measures():
    # Legacy line with no pieces/weight; the parcel (item_meta) supplies them.
    line = {"unit": "liter", "quantity": 2}
    meta = {"qty_is_pieces": False, "qty_is_weight": False, "pieces": 5,
            "weight": 10.0, "weight_unit": "gram", "quantity": 2}
    assert measure_sublines(line, item_meta=meta) == ["- 5 pcs.", "- 10 gram"]


def test_fallback_uses_quantity_for_the_sell_by_measure():
    # Piece-sold legacy line: parcel pieces come from its quantity, then skipped.
    meta = {"qty_is_pieces": True, "qty_is_weight": False, "quantity": 7,
            "pieces": None, "weight": 3.0, "weight_unit": "carat"}
    line = {"unit": "piece", "quantity": 7}
    # pieces skipped (qty is pieces), weight shown from parcel.
    assert measure_sublines(line, item_meta=meta) == ["- 3 carat"]


def test_weight_sold_uses_sell_by_as_weight_unit():
    # A weight-sold item has no separate weight_unit; its unit IS the sell_by, and
    # the weight value mirrors the quantity (shown locked in the draft cell).
    line = {"unit": "carat", "quantity": 4.5}
    meta = {"qty_is_weight": True, "qty_is_pieces": False, "weight": None,
            "weight_unit": None, "sell_by": "carat", "quantity": 4.5}
    _p, w, wu, _qip, qiw = resolve_line_measures(line, item_meta=meta)
    assert qiw is True and w == 4.5 and wu == "carat"


def test_qty_label():
    assert qty_label({"quantity": 2, "unit": "carat"}) == "2 carat"
    assert qty_label({"quantity": 4.50, "sell_by": "gram"}) == "4.5 gram"
    assert qty_label({"quantity": 3}) == "3"
    assert qty_label({}) == ""


def test_item_measure_meta():
    item = {"sell_by": "carat", "allow_splitting": True, "quantity": 4.5,
            "weight": 4.5, "weight_unit": "carat", "pieces": 3}
    meta = item_measure_meta(item, UM)
    assert meta["qty_is_weight"] is True
    assert meta["qty_is_pieces"] is False
    assert meta["weight_unit"] == "carat"
    assert meta["pieces"] == 3


def test_measure_locks():
    # piece-sold, splittable: pcs locked (qty is pieces), no weight, qty editable.
    meta = {"sell_by": "piece", "qty_is_pieces": True, "qty_is_weight": False,
            "pieces": None, "weight": None, "allow_splitting": True}
    locks = measure_locks(meta)
    assert locks["pcs_locked"] is True
    assert locks["weight_locked"] is True   # no weight tracked
    assert locks["qty_locked"] is False
    # non-splittable: everything locked.
    locks2 = measure_locks({**meta, "allow_splitting": False})
    assert locks2 == {"pcs_locked": True, "weight_locked": True, "qty_locked": True}