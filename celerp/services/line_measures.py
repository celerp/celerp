# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Shared rendering of a doc line's pieces/weight measures.

A document line carries an invoiced quantity plus, for splittable inventory, a
pieces and/or weight breakdown. Three render paths (the finalized on-screen
table, the HTML print view, and the reportlab PDF) show that breakdown the same
way, so the rules live here:

- the value comes from the line, falling back to the parcel item for legacy
  lines that predate the pieces/weight fields;
- the measure that the quantity already *is* (a piece-sold item's pieces, a
  weight-sold item's weight) is skipped to avoid repeating the qty column.
"""

from __future__ import annotations

from celerp.services.units import is_pieces_unit, is_weight_unit


def _g(value) -> str:
    """Format a measure number without trailing zeros (3.0 -> '3', 4.5 -> '4.5')."""
    return f"{float(value):g}"


def item_measure_meta(item: dict, unit_map: dict) -> dict:
    """Project a (flattened) inventory item to the measure-meta a doc line needs.

    Single source for both the doc page's item_meta_map and the line-item picker.
    """
    sell_by = item.get("sell_by")
    return {
        "sell_by": sell_by,
        "allow_splitting": bool(item.get("allow_splitting")),
        "quantity": item.get("quantity"),
        "weight": item.get("weight"),
        "weight_unit": item.get("weight_unit"),
        # _flatten_item lifts attributes.* to the top level, so pieces is here.
        "pieces": item.get("pieces"),
        "qty_is_weight": is_weight_unit(sell_by, unit_map),
        "qty_is_pieces": is_pieces_unit(sell_by, unit_map),
    }


def _classify(line: dict, unit_map: dict | None, meta: dict) -> tuple[bool, bool]:
    """Return (qty_is_pieces, qty_is_weight) — prefer the precomputed meta flags,
    else classify the line's own unit against unit_map."""
    if meta and "qty_is_pieces" in meta:
        return bool(meta["qty_is_pieces"]), bool(meta["qty_is_weight"])
    unit = line.get("unit") or line.get("sell_by") or ""
    return is_pieces_unit(unit, unit_map or {}), is_weight_unit(unit, unit_map or {})


def resolve_line_measures(line: dict, *, unit_map: dict | None = None, item_meta: dict | None = None):
    """Resolve the displayable (pieces, weight, weight_unit, qty_is_pieces,
    qty_is_weight) for a line, falling back to the parcel item when the line
    itself has no stored measure."""
    meta = item_meta or {}
    qty_is_pieces, qty_is_weight = _classify(line, unit_map, meta)
    # For the measure the quantity IS, the value is the line's own quantity (the
    # invoiced amount); the other measure falls back to the parcel item.
    line_qty = line.get("quantity")
    pieces = line.get("pieces")
    if pieces is None:
        pieces = line_qty if qty_is_pieces else meta.get("pieces")
    weight = line.get("weight")
    if weight is None:
        weight = line_qty if qty_is_weight else meta.get("weight")
    weight_unit = line.get("weight_unit") or meta.get("weight_unit") or ""
    if not weight_unit and qty_is_weight:
        # A weight-sold item has no separate weight_unit — its unit IS the sell_by.
        weight_unit = line.get("unit") or line.get("sell_by") or meta.get("sell_by") or ""
    return pieces, weight, weight_unit, qty_is_pieces, qty_is_weight


def measure_sublines(line: dict, *, unit_map: dict | None = None, item_meta: dict | None = None) -> list[str]:
    """Dash-prefixed measure lines for a line's description cell, e.g.
    ``["- 3 pcs.", "- 4.5 carat"]``. The measure the quantity already is gets
    skipped; absent measures are omitted."""
    pieces, weight, weight_unit, qty_is_pieces, qty_is_weight = resolve_line_measures(
        line, unit_map=unit_map, item_meta=item_meta
    )
    out: list[str] = []
    if pieces is not None and not qty_is_pieces:
        out.append(f"- {_g(pieces)} pcs.")
    if weight is not None and not qty_is_weight:
        out.append(f"- {_g(weight)} {weight_unit}".rstrip())
    return out


def qty_label(line: dict) -> str:
    """The quantity with its unit, e.g. "2 carat" — clearer than a bare number."""
    qty = line.get("quantity")
    if qty is None:
        qty = line.get("qty")
    if qty is None:
        return ""
    unit = line.get("unit") or line.get("sell_by") or ""
    return f"{_g(qty)} {unit}".strip()


def measure_locks(meta: dict, *, allow_split: bool | None = None) -> dict:
    """Editability of the draft PCS / WEIGHT / QTY cells for a parcel.

    A cell is locked when the parcel doesn't track that measure, when the
    quantity already IS that measure, or when splitting is disallowed.
    """
    allow = allow_split if allow_split is not None else bool(meta.get("allow_splitting", True))
    return {
        "pcs_locked": meta.get("pieces") is None or bool(meta.get("qty_is_pieces")) or not allow,
        "weight_locked": meta.get("weight") is None or bool(meta.get("qty_is_weight")) or not allow,
        "qty_locked": not allow,
    }