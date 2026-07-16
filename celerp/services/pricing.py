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

import math
import uuid as _uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.services.money import round_rate

# Price list names treated as cost (restricted to manager+ and never derivable).
COST_PRICE_LIST_NAMES: frozenset[str] = frozenset({"cost", "cost price", "landed", "landed cost"})

# The list every company starts pointing at, for both the default and the base list.
DEFAULT_PRICE_LIST_NAME: str = "Retail"

# Read fallback for companies whose settings were never seeded. Includes Cost so
# cost columns and valuation keep working before first settings access.
PRICE_LISTS_FALLBACK: list[dict] = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]

# Rounding increments offered by the settings UI; the API accepts any positive number.
ROUNDING_CHOICES: tuple[str, ...] = ("0.01", "0.05", "0.10", "0.50", "1", "5", "10")

# Ceiling for factors and rounding increments. Validation and the read path both evaluate
# through Decimal; unbounded values overflow its 28-digit context at read time, which would
# turn one bad config into a failure on every item read.
MAX_FACTOR: float = 1_000_000.0


def coerce_price(value) -> float | None:
    """A stored or submitted price-like value as a finite float, else None.

    Booleans are not prices (json ``true`` would otherwise coerce to 1.0), and neither are
    NaN/Infinity or unparseable strings.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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
    Returns 0.0 if no price is found for this list. A non-numeric or non-finite
    stored value reads as missing; item state is not schema-validated, and one
    garbage value must not take down every read that prices items.
    """
    val = coerce_price(item.get(price_list))
    if val is not None:
        return val
    val = coerce_price(item.get(price_key(price_list)))
    if val is not None:
        return val
    return 0.0


def round_half_up_to_increment(value: float, increment: float | None) -> float:
    """Round to the nearest multiple of ``increment``, halves up. ``None`` leaves it unrounded."""
    if increment is None:
        return float(value)
    inc = Decimal(str(increment))
    return float((Decimal(str(value)) / inc).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * inc)


def derived_price(base_value: float, pl: dict) -> float:
    """Compute a derived list's value from the base value: multiplier, then rounding.

    A positive price never rounds to zero: when the increment is coarser than the raw
    value (a 5-increment on a 1.47 raw price), the result floors at one increment
    instead of pricing the item free.
    """
    raw = float(Decimal(str(base_value)) * Decimal(str(pl["multiplier"])))
    rounding = pl.get("rounding")
    if rounding is None:
        return raw
    rounded = round_half_up_to_increment(raw, float(rounding))
    if rounded == 0 and raw > 0:
        return float(rounding)
    return rounded


def inject_derived_prices(flat: dict, price_lists: list[dict], base_name: str, currency: str) -> dict:
    """Set every derived list's computed price on a flattened item dict.

    The formula always wins: stored leftovers under a derived list's keys (from before
    the list became derived) are overwritten or removed, so a derived list can never
    show a stale stored value. With no positive base value the keys are absent and the
    list reads as unpriced, like any other missing price.

    Every injected value obeys the rate ceiling (``round_rate``): a unit price must not
    carry more decimals than ``rate_dp(currency)``, or ``round(rate × qty)`` loses the
    ability to reconcile against entered totals. A value that collapses to zero at the
    ceiling, or any numeric failure from an invalid config, reads as unpriced for that
    list only. Cost lists and the base list are never overwritten. Mutates and returns
    ``flat``.
    """
    base_value = resolve_price(flat, base_name)
    for pl in price_lists:
        if not is_derived(pl):
            continue
        name = pl.get("name", "")
        if is_cost_list_name(name) or name == base_name:
            # Never overwrite cost (it comes from purchases and recipes) or the base's own
            # key, even when an invalid config reaches storage through an unvalidated door.
            continue
        flat.pop(name, None)
        value = 0.0
        if base_value > 0:
            try:
                value = float(round_rate(derived_price(base_value, pl), currency))
            except (ArithmeticError, TypeError, ValueError):
                # A config the validator would reject (overflow-sized factor, junk types)
                # must degrade to "unpriced" per list, never fail the whole item read.
                value = 0.0
        if value > 0:
            flat[price_key(name)] = value
        else:
            flat.pop(price_key(name), None)
    return flat


def validate_price_lists(price_lists: list[dict], base_name: str, base_explicit: bool = True) -> str | None:
    """First human-readable rule violation in a price-lists config, else None.

    ``base_explicit`` is False when the base name is only the unconfigured fallback; the
    base-must-exist rule then binds only once a list actually derives from it, so a company
    that renamed the fallback list long ago can still edit its manual lists freely.
    """

    def _valid_factor(v) -> bool:
        # Bool would pass float() but detonate as Decimal("True") at read time; the read
        # path evaluates through Decimal, so the gate must be at least as strict as it.
        f = coerce_price(v)
        return f is not None and 0 < f <= MAX_FACTOR

    names = [str(pl.get("name", "")).strip() for pl in price_lists]
    if any(not n for n in names):
        return "Price list names cannot be empty"
    lowered = [n.lower() for n in names]
    duplicates = sorted({n for n in lowered if lowered.count(n) > 1})
    if duplicates:
        return f"Price list names must be unique ('{duplicates[0]}' appears more than once)"
    for pl, name in zip(price_lists, names):
        multiplier = pl.get("multiplier")
        rounding = pl.get("rounding")
        if multiplier is not None:
            if not _valid_factor(multiplier):
                return f"Factor for '{name}' must be a number greater than 0 and at most {MAX_FACTOR:,.0f}"
            if is_cost_list_name(name):
                return f"'{name}' cannot be derived; cost comes from purchases and recipes"
            if name == base_name:
                return f"'{name}' is the base price list; a base list cannot have a factor"
        if rounding is not None:
            if multiplier is None:
                return f"Rounding on '{name}' requires a factor"
            if not _valid_factor(rounding):
                return f"Rounding for '{name}' must be a number greater than 0 and at most {MAX_FACTOR:,.0f}"
    if base_name not in names and (base_explicit or any(is_derived(pl) for pl in price_lists)):
        return f"Base price list '{base_name}' does not exist"
    return None


def new_cost_name_error(price_lists: list[dict], stored_price_lists: list[dict]) -> str | None:
    """Reject cost-family names that were not already present in the stored config.

    Renaming a sell list to e.g. "landed cost" would silently convert it into a
    manager-restricted system column and hide it from the settings table; the cost
    list is managed by the system, not created through this surface.
    """
    stored_cost = {
        str(pl.get("name", "")).strip().lower()
        for pl in stored_price_lists
        if is_cost_list_name(str(pl.get("name", "")).strip())
    }
    for pl in price_lists:
        name = str(pl.get("name", "")).strip()
        if is_cost_list_name(name) and name.lower() not in stored_cost:
            return f"'{name}' is a reserved cost name; the cost list is managed by the system"
    return None


def normalized_price_lists(price_lists: list[dict]) -> list[dict]:
    """Canonicalize a validated config for storage: factors and rounding as plain floats,
    so the read path never re-parses strings or exotic JSON types."""
    out = []
    for pl in price_lists:
        pl = dict(pl)
        if pl.get("multiplier") is not None:
            pl["multiplier"] = float(pl["multiplier"])
        if pl.get("rounding") is not None:
            pl["rounding"] = float(pl["rounding"])
        out.append(pl)
    return out


async def get_price_config(session: AsyncSession, company_id) -> tuple[list[dict], str, str]:
    """The company's ``(price_lists, base_price_list, currency)`` in one settings read.

    An id that cannot reference a company row resolves to the same fallback config as a
    missing company: no derived lists, default base, USD.
    """
    from celerp.models.company import Company

    if isinstance(company_id, str):
        try:
            company_id = _uuid.UUID(company_id)
        except ValueError:
            return PRICE_LISTS_FALLBACK, DEFAULT_PRICE_LIST_NAME, "USD"
    co = await session.get(Company, company_id)
    settings = (co.settings if co else {}) or {}
    price_lists = settings.get("price_lists") or PRICE_LISTS_FALLBACK
    base_name = settings.get("base_price_list") or DEFAULT_PRICE_LIST_NAME
    currency = settings.get("currency") or "USD"
    return price_lists, base_name, currency
