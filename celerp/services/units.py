# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Canonical unit-of-measure definitions and quantity precision validation.

Single source of truth imported by both celerp-inventory and celerp-docs.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

# Units sold by weight/volume/length allow fractional quantities.
# "piece" (decimals=0) enforces positive integers.
DEFAULT_UNITS: list[dict] = [
    {"name": "piece",  "label": "Piece",          "decimals": 0},
    {"name": "carat",  "label": "Carat (ct)",      "decimals": 2},
    {"name": "gram",   "label": "Gram (g)",         "decimals": 2},
    {"name": "kg",     "label": "Kilogram (kg)",    "decimals": 3},
    {"name": "oz",     "label": "Ounce (oz)",       "decimals": 2},
    {"name": "lb",     "label": "Pound (lb)",       "decimals": 2},
    {"name": "liter",  "label": "Liter (L)",        "decimals": 2},
    {"name": "meter",  "label": "Meter (m)",        "decimals": 2},
]

# sell_by values that represent services - quantity precision is not enforced.
SERVICE_SELL_BY: frozenset[str] = frozenset({"service", "hour"})


def build_unit_map(units: list[dict]) -> dict[str, dict]:
    """Return a name-keyed dict for O(1) lookup."""
    return {u["name"]: u for u in units}


def is_weight_unit(unit_name: str | None, unit_map: dict[str, dict]) -> bool:
    """Return True if the named unit has unit_type='weight'."""
    if not unit_name:
        return False
    return unit_map.get(unit_name, {}).get("unit_type") == "weight"


def is_pieces_unit(unit_name: str | None, unit_map: dict[str, dict]) -> bool:
    """Return True if the named unit has unit_type='pieces'."""
    if not unit_name:
        return False
    return unit_map.get(unit_name, {}).get("unit_type") == "pieces"


def validate_quantity(qty: float, decimals: int, *, label: str = "Quantity") -> None:
    """Raise HTTP 422 if *qty* has more decimal places than *decimals* allows.

    Uses Decimal round-trip to avoid float arithmetic artifacts
    (e.g. 2.55 * 100 = 254.999...).

    label: human-readable name included in the error message (e.g. item name).
    """
    d = Decimal(str(qty))
    quantizer = Decimal(10) ** -decimals
    rounded = d.quantize(quantizer, rounding=ROUND_HALF_UP)
    if d != rounded:
        raise HTTPException(
            status_code=422,
            detail=f"{label}: quantity {qty} exceeds allowed precision ({decimals} decimal places for this unit)",
        )


def validate_positive(qty: float, *, label: str = "Quantity") -> None:
    """Raise HTTP 422 if *qty* is not strictly positive."""
    if qty <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"{label}: quantity must be greater than zero, got {qty}",
        )


def validate_line_quantity(qty: float, sell_by: str | None, unit_map: dict[str, dict], *, label: str = "Quantity") -> None:
    """Validate a single line quantity against its sell_by unit.

    - Skips all validation when sell_by is absent, unknown, or a service type
      (legacy lines and free-text items are not constrained).
    - Enforces positive value when sell_by is a known stocked unit.
    - Enforces decimal precision according to the unit config.
    """
    if not sell_by or sell_by in SERVICE_SELL_BY or sell_by not in unit_map:
        return
    validate_positive(qty, label=label)
    validate_quantity(qty, unit_map[sell_by]["decimals"], label=label)
