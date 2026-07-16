# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Kernel service: price list resolution and derived pricing.

Single source of truth for how a price list name maps to an item price. A price
list is either manual (per-item values stored under its ``<name>_price`` key) or
derived (a ``multiplier`` of the company's base price list, optionally rounded to
an increment). Derived values are computed when item state is flattened for
reading and are never stored per item.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

# Price list names treated as cost (restricted to manager+ and never derivable).
COST_PRICE_LIST_NAMES: frozenset[str] = frozenset({"cost", "cost price", "landed", "landed cost"})

# The list every company starts pointing at, for both the default and the base list.
DEFAULT_PRICE_LIST_NAME: str = "Retail"

# Read fallback for companies whose settings were never seeded. Includes Cost so
# cost columns and valuation keep working before first settings access.
PRICE_LISTS_FALLBACK: list[dict] = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]

# Rounding increments offered by the settings UI; the API accepts any positive number.
ROUNDING_CHOICES: tuple[str, ...] = ("0.01", "0.05", "0.10", "0.50", "1", "5", "10")


def price_key(name: str) -> str:
    """The conventional item key for a price list name (``Retail`` -> ``retail_price``)."""
    return f"{name.lower()}_price"


def is_cost_list_name(name: str) -> bool:
    return name.lower() in COST_PRICE_LIST_NAMES


def is_derived(pl: dict) -> bool:
    """A price list is derived when it carries a multiplier."""
    return pl.get("multiplier") is not None


def derived_price_keys(price_lists: list[dict]) -> set[str]:
    """Item keys that belong to derived lists (computed, never stored)."""
    return {price_key(pl.get("name", "")) for pl in price_lists if is_derived(pl)}


def resolve_price(item: dict, price_list: str) -> float:
    """Deterministic price lookup. No fallback chain.

    Checks the price list name directly on the item, then the conventional
    {name.lower()}_price key (e.g. "retail_price" for "Retail").
    Returns 0.0 if no price is found for this list.
    """
    val = item.get(price_list)
    if val is not None:
        return float(val)
    val = item.get(price_key(price_list))
    if val is not None:
        return float(val)
    return 0.0


def round_half_up_to_increment(value: float, increment: float | None) -> float:
    """Round to the nearest multiple of ``increment``, halves up. ``None`` leaves it unrounded."""
    if increment is None:
        return float(value)
    inc = Decimal(str(increment))
    return float((Decimal(str(value)) / inc).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * inc)


def derived_price(base_value: float, pl: dict) -> float:
    """Compute a derived list's value from the base value: multiplier, then rounding."""
    raw = float(Decimal(str(base_value)) * Decimal(str(pl["multiplier"])))
    rounding = pl.get("rounding")
    return round_half_up_to_increment(raw, float(rounding) if rounding is not None else None)


def inject_derived_prices(flat: dict, price_lists: list[dict], base_name: str) -> dict:
    """Set every derived list's computed price on a flattened item dict.

    The formula always wins: stored leftovers under a derived list's keys (from before
    the list became derived) are overwritten or removed, so a derived list can never
    show a stale stored value. With no positive base value the keys are absent and the
    list reads as unpriced, like any other missing price. Mutates and returns ``flat``.
    """
    base_value = resolve_price(flat, base_name)
    for pl in price_lists:
        if not is_derived(pl):
            continue
        name = pl.get("name", "")
        flat.pop(name, None)
        if base_value > 0:
            flat[price_key(name)] = derived_price(base_value, pl)
        else:
            flat.pop(price_key(name), None)
    return flat


def validate_price_lists(price_lists: list[dict], base_name: str, base_explicit: bool = True) -> str | None:
    """First human-readable rule violation in a price-lists config, else None.

    ``base_explicit`` is False when the base name is only the unconfigured fallback; the
    base-must-exist rule then binds only once a list actually derives from it, so a company
    that renamed the fallback list long ago can still edit its manual lists freely.
    """

    def _positive_number(v) -> bool:
        try:
            return float(v) > 0
        except (TypeError, ValueError):
            return False

    names = [str(pl.get("name", "")).strip() for pl in price_lists]
    for pl, name in zip(price_lists, names):
        multiplier = pl.get("multiplier")
        rounding = pl.get("rounding")
        if multiplier is not None:
            if not _positive_number(multiplier):
                return f"Factor for '{name}' must be a number greater than 0"
            if is_cost_list_name(name):
                return f"'{name}' cannot be derived; cost comes from purchases and recipes"
            if name == base_name:
                return f"'{name}' is the base price list; a base list cannot have a factor"
        if rounding is not None:
            if multiplier is None:
                return f"Rounding on '{name}' requires a factor"
            if not _positive_number(rounding):
                return f"Rounding for '{name}' must be a number greater than 0"
    if base_name not in names and (base_explicit or any(is_derived(pl) for pl in price_lists)):
        return f"Base price list '{base_name}' does not exist"
    return None


async def get_price_config(session: AsyncSession, company_id) -> tuple[list[dict], str]:
    """The company's ``(price_lists, base_price_list)`` in one settings read."""
    from celerp.models.company import Company

    co = await session.get(Company, company_id)
    settings = (co.settings if co else {}) or {}
    price_lists = settings.get("price_lists") or PRICE_LISTS_FALLBACK
    base_name = settings.get("base_price_list") or DEFAULT_PRICE_LIST_NAME
    return price_lists, base_name
