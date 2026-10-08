# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Canonical unit-of-measure definitions and quantity precision validation.

Single source of truth imported by both celerp-inventory and celerp-docs.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

# Units sold by weight/volume/length allow fractional quantities.
# "piece" (decimals=0) enforces positive integers.
DEFAULT_UNITS: list[dict] = [
    {"name": "piece",  "label": "Piece",          "decimals": 0, "unit_type": "pieces"},
    {"name": "carat",  "label": "Carat (ct)",      "decimals": 2, "unit_type": "weight"},
    {"name": "gram",   "label": "Gram (g)",         "decimals": 2, "unit_type": "weight"},
    {"name": "kg",     "label": "Kilogram (kg)",    "decimals": 3, "unit_type": "weight"},
    {"name": "oz",     "label": "Ounce (oz)",       "decimals": 2, "unit_type": "weight"},
    {"name": "lb",     "label": "Pound (lb)",       "decimals": 2, "unit_type": "weight"},
    {"name": "liter",  "label": "Liter (L)",        "decimals": 2, "unit_type": "quantity"},
    {"name": "meter",  "label": "Meter (m)",        "decimals": 2, "unit_type": "quantity"},
]

# sell_by values that represent services - quantity precision is not enforced.
SERVICE_SELL_BY: frozenset[str] = frozenset({"service", "hour"})

# Inventory types that carry no pickable stock: the stock guard and fulfillment skip them.
# (non_stocked is intentionally NOT here - it keeps its existing fulfillment behaviour.)
NON_STOCK_INVENTORY_TYPES: frozenset[str] = frozenset({"service", "freight"})

# Landed-cost component kinds carried on a freight-typed charge line (refines reporting + GL routing).
LANDED_COST_KINDS: frozenset[str] = frozenset({"freight", "insurance", "duty", "import_vat"})


def is_non_stock_line(inventory_type: str | None, sell_by: str | None = None) -> bool:
    """True for charge/service lines that have no stock to pick (service or freight, by type or unit)."""
    return (inventory_type or "stocked") in NON_STOCK_INVENTORY_TYPES or (sell_by or "") in SERVICE_SELL_BY


def default_receive_as(inventory_type: str | None, sell_by: str | None = None) -> str:
    """How a purchase line for this item comes in unless the user says otherwise: an item
    that is not kept in stock (a service, a charge, a non-stocked item) is an expense."""
    if inventory_type == "non_stocked" or is_non_stock_line(inventory_type, sell_by):
        return "expense"
    return "stock"


# How a purchase line's goods come in.
RECEIVE_KINDS: tuple[str, ...] = ("stock", "expense", "asset")


def line_receive_kind(line: dict) -> str:
    """What a purchase line brings in: stock, an expense or an asset. The kind stored on the
    line wins; a line saved without one is stock when it names an item or SKU and an expense
    otherwise. A parcel a receipt created (``entity_id``) is not a catalog reference."""
    kind = str(line.get("receive_as") or "").strip().lower()
    if kind in RECEIVE_KINDS:
        return kind
    return "stock" if line.get("sku") or line.get("item_id") else "expense"


def is_landed_component(inventory_type: str | None) -> bool:
    """True for a freight-typed line (a landed-cost component: freight/insurance/duty/import_vat)."""
    return (inventory_type or "stocked") == "freight"


def build_unit_map(units: list[dict]) -> dict[str, dict]:
    """Return a name-keyed dict for O(1) lookup."""
    return {u["name"]: u for u in units}


async def get_company_units(session, company_id) -> list[dict]:
    """Return the company's units config, falling back to DEFAULT_UNITS."""
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    units = (company.settings or {}).get("units") if company else None
    return units or DEFAULT_UNITS


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


def exceeds_precision(qty: float, decimals: int) -> bool:
    """True if *qty* has more decimal places than *decimals* allows.

    Uses Decimal round-trip to avoid float arithmetic artifacts
    (e.g. 2.55 * 100 = 254.999...).
    """
    d = Decimal(str(qty))
    return d != d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP)


def validate_quantity(qty: float, decimals: int, *, label: str = "Quantity") -> None:
    """Raise HTTP 422 if *qty* has more decimal places than *decimals* allows.

    label: human-readable name included in the error message (e.g. item name).
    """
    if exceeds_precision(qty, decimals):
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


def validate_line_quantity(
    qty: float,
    sell_by: str | None,
    unit_map: dict[str, dict],
    *,
    label: str = "Quantity",
    require_positive: bool = True,
) -> None:
    """Validate a single line quantity against its sell_by unit.

    - Skips all validation when sell_by is absent, unknown, or a service type
      (legacy lines and free-text items are not constrained).
    - Enforces positive value when sell_by is a known stocked unit, unless
      require_positive is False (an audit list records a real zero on-hand count).
    - Enforces decimal precision according to the unit config.
    """
    if not sell_by or sell_by in SERVICE_SELL_BY or sell_by not in unit_map:
        return
    if require_positive:
        validate_positive(qty, label=label)
    validate_quantity(qty, unit_map[sell_by]["decimals"], label=label)


def format_qty(value, unit_name: str | None, unit_map: dict[str, dict]) -> str:
    """Format a quantity value according to its unit's decimal precision.

    Returns a string with exactly `decimals` decimal places for known units,
    or a plain str() conversion for unknown/absent units.
    Falls back to str() if value is not numeric.
    """
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (ValueError, TypeError):
        return str(value)
    if unit_name and unit_name in unit_map:
        decimals = unit_map[unit_name].get("decimals", None)
        if decimals is not None:
            return f"{f:.{decimals}f}"
    # Unknown unit: strip trailing zeros but keep at least one decimal if non-integer
    return f"{f:g}"
