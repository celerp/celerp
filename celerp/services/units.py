# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Canonical unit-of-measure definitions and quantity precision validation.

Single source of truth imported by both celerp-inventory and celerp-docs.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from ui.i18n import t

from celerp.accounting_roles import refusal

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
    """The company's units config (DEFAULT_UNITS when it has none), for a write that validates
    quantities against it.

    Read under the company lock, as the last holder committed it: a precision change commits
    under the same lock, so a quantity checked here can never be stored under decimals it was
    not checked against. A copy the session loaded earlier in the request is never trusted."""
    from celerp.models.company import Company
    from celerp.services.company_lock import holds_company_lock, locked_company

    if holds_company_lock(session, company_id):
        company = await session.get(Company, company_id)
    else:
        company = await locked_company(session, company_id)
    return _units_of(company)


async def read_company_units(session, company_id) -> list[dict]:
    """The company's units config for display only, without taking the company lock. A write
    validates with get_company_units instead."""
    from celerp.models.company import Company

    return _units_of(await session.get(Company, company_id))


def _units_of(company) -> list[dict]:
    return settings_units(company.settings if company else None)


def settings_units(settings: dict | None) -> list[dict]:
    """The units a company settings value holds (DEFAULT_UNITS when it has none)."""
    return (settings or {}).get("units") or DEFAULT_UNITS


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


def unit_decimals(sell_by: str | None, unit_map: dict[str, dict]) -> int | None:
    """The decimal places a quantity in this unit may have, or None when the unit is absent,
    unknown or a service, whose quantities are not constrained."""
    if not sell_by or sell_by in SERVICE_SELL_BY or sell_by not in unit_map:
        return None
    return int(unit_map[sell_by].get("decimals") or 0)


def quantity_step(unit_name: str | None, unit_map: dict[str, dict]) -> float | None:
    """The smallest quantity the named unit allows (1 for piece, 0.01 for gram), or None when
    the unit is unknown or a service, whose quantities are not constrained."""
    decimals = unit_decimals(unit_name, unit_map)
    return None if decimals is None else 10.0 ** -decimals


def validate_quantity(qty: float, decimals: int, *, label: str | None = None) -> None:
    """Raise HTTP 422 if *qty* has more decimal places than *decimals* allows.

    label: the item or line the quantity is for, named in the message when given.
    """
    if exceeds_precision(qty, decimals):
        rule = f"{qty:g} is more precise than this unit allows (at most {decimals} decimal places)."
        detail = (refusal("quantity.precision", f"{label}: {rule}", label=label, qty=f"{qty:g}", decimals=decimals)
                  if label else
                  refusal("quantity.precision_plain", rule, qty=f"{qty:g}", decimals=decimals))
        raise HTTPException(status_code=422, detail=detail)


def _finer_than(value, decimals: int) -> bool:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(qty) and exceeds_precision(qty, decimals)


def _places(value) -> int:
    """The decimal places a stored quantity uses (0.275 -> 3)."""
    return max(0, -Decimal(str(float(value))).normalize().as_tuple().exponent)


def _line_unit(line: dict, item_unit: dict[str, str]) -> str | None:
    """A line's unit: its own, else its item's, found by item id or SKU."""
    return line.get("unit") or line.get("sell_by") or item_unit.get(line.get("item_id")) \
        or item_unit.get(line.get("sku"))


def _item_units(items: dict[str, dict]) -> dict[str, str]:
    return {key: state.get("sell_by") for entity_id, state in items.items()
            for key in (entity_id, state.get("sku")) if key}


def _still_moving(entity_id: str, entity_type: str, state: dict, items: dict[str, dict]) -> list[tuple[dict, float]]:
    """Each line of a document, list or manufacturing run with the quantity on it that can still
    move: what a document can still receive, ship or carry into a converted document
    (quantities_still_moving), a list's lines while it is a draft or finalized (its closing action
    is still to come), and what an open run still has to issue and receive. ``items`` are the
    item states by id, to tell which goods a document already sent out."""
    from celerp_docs.doc_constants import quantities_still_moving, sent_out_by
    from celerp_manufacturing.movements import quantities_still_moving as run_still_moving

    from celerp.services.document_lines import line_item_id
    from celerp.services.list_behavior import DRAFT, FINALIZED

    if entity_type == "doc":
        return quantities_still_moving(
            state, lambda line: sent_out_by(items.get(line_item_id(line)) or {}, entity_id))
    if entity_type == "mfg_order":
        return run_still_moving(state)
    return [(line, line.get("quantity")) for line in state.get("line_items") or []
            if isinstance(line, dict)] if state.get("status") in (None, DRAFT, FINALIZED) else []


async def require_moving_fits_units(session, company_id, entity_id: str, entity_type: str, state: dict,
                                    also: list[tuple[dict, float]] = (), template: str | None = None) -> None:
    """A document, list or manufacturing run about to move again (unvoided, reopened, back to
    draft, undone, or copied into a new draft or a new document) holds what it can then still
    move, and the item quantities ``also`` names, to its units' decimal places today: a precision
    change let them through while they were history, so they are checked as a line writer checks
    them, under the company lock. ``state`` is the state it is about to take. A quantity finer
    than its unit allows is refused 422 naming the line, the unit and the way out, before anything
    is written; a document a subscription makes names that subscription (``template``)."""
    from sqlalchemy import or_, select

    from celerp.models.projections import Projection
    from celerp.services.document_lines import line_item_id

    # A run's moving lines name only their items, so they are known before the items are read.
    named = [line for line, _ in _still_moving(entity_id, entity_type, state, {})] \
        if entity_type == "mfg_order" else [line for line in state.get("line_items") or [] if isinstance(line, dict)]
    keys = {key for line in [*named, *(line for line, _ in also)]
            for key in (line_item_id(line), line.get("sku")) if key}
    items = dict((await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        or_(Projection.entity_id.in_(keys), Projection.state["sku"].as_string().in_(keys))))).all()) if keys else {}
    lines = [*_still_moving(entity_id, entity_type, state, items), *also]
    item_unit = _item_units(items)
    unit_map = build_unit_map(await get_company_units(session, company_id))
    for line, qty in lines:
        unit = _line_unit(line, item_unit)
        decimals = unit_decimals(unit, unit_map)
        if decimals is None or not _finer_than(qty, decimals):
            continue
        item = items.get(line.get("item_id")) or {}
        label = str(line.get("name") or line.get("sku") or item.get("sku") or item.get("name") or "-")
        places = _places(qty)
        shown = plain_number(float(qty))
        way_out = f"set {unit} back to {places} decimal places in Global Config, Inventory, Units"
        if template:
            raise HTTPException(status_code=422, detail=refusal(
                "units.template_finer_than_unit",
                f"Subscription {template} lists {label} at {shown} {unit}, but {unit} now allows only "
                f"{decimals} decimal places. To go ahead, {way_out}, or cancel this subscription and set up "
                f"a new one with a quantity {unit} allows.",
                template=template, label=label, qty=shown, unit=unit, decimals=decimals, places=places))
        raise HTTPException(status_code=422, detail=refusal(
            "units.moving_finer_than_unit",
            f"{label} holds {shown} {unit}, but {unit} now allows only {decimals} decimal places. "
            f"To go ahead, {way_out}.",
            label=label, qty=shown, unit=unit, decimals=decimals, places=places))


def _partly_received(entity_type: str, state: dict) -> bool:
    """A purchase order or bill that received some of its goods and still expects the rest. It
    cannot be voided, closed or edited while it holds received goods, so a refusal it is named in
    gives it the way out that works: Return Goods, void, and issue it again."""
    return entity_type == "doc" and state.get("doc_type") in ("purchase_order", "bill") and any(
        float(line.get("quantity_received") or 0) > 0
        for line in state.get("line_items") or [] if isinstance(line, dict))


async def check_precision_change(session, company_id, old_units: list[dict], new_units: list[dict]) -> None:
    """Fewer decimal places for a unit that still holds finer quantities: refused with 422, naming
    a few of the items and open documents that hold them and, apart, the subscriptions, with the
    way out of each: an item's quantity can be changed and a document finished, while an active or
    paused subscription's lines cannot be edited, so it is cancelled and set up again. Every item
    counts, whatever its status, since any item can come back into stock; a document, list or run
    counts what it can still move (_still_moving). What a document received or shipped is history
    and never blocks; a document, list or run that later moves again is held to the precision of
    that day by require_moving_fits_units. Raising precision always passes. A unit missing from
    the old list (deleted, then added back or re-seeded) had no precision to compare with, so it
    is checked against what is stored in it. A line with no unit of its own is in its item's unit,
    found by item id or SKU."""
    from sqlalchemy import or_, select

    from celerp_docs.doc_constants import TEMPLATE_DOC_TYPES, template_label
    from celerp_manufacturing.movements import run_name

    from celerp.models.projections import Projection
    from celerp.services.line_measures import FULL, stored_sell_qty

    old = {u["name"]: int(u.get("decimals") or 0) for u in old_units}
    lowered = {u["name"]: int(u["decimals"]) for u in new_units
               if u["name"] not in old or int(u["decimals"]) < old[u["name"]]}
    if not lowered:
        return
    status = Projection.state["status"].as_string()
    rows = (await session.execute(select(Projection.entity_id, Projection.entity_type, Projection.state).where(
        Projection.company_id == company_id,
        or_((Projection.entity_type == "item") & Projection.state["sell_by"].as_string().in_(list(lowered)),
            Projection.entity_type.in_(("doc", "list")) & or_(status.is_(None), status != "void"),
            Projection.entity_type == "mfg_order"),
    ))).all()
    items = {entity_id: state for entity_id, entity_type, state in rows if entity_type == "item"}
    item_unit = _item_units(items)

    rows = [(entity_id, entity_type, state,
             None if entity_type == "item" else _still_moving(entity_id, entity_type, state, items))
            for entity_id, entity_type, state in rows]
    for name, decimals in lowered.items():
        holders: list[str] = []
        templates: list[str] = []
        needed = decimals
        partly_received = False
        for entity_id, entity_type, state, lines in rows:
            template = state.get("doc_type") in TEMPLATE_DOC_TYPES
            if entity_type == "item":
                sell_qty = stored_sell_qty(state.get("default_sell_qty"))
                quantities = [state.get("quantity"), *([] if sell_qty in (None, FULL) else [sell_qty])] \
                    if state.get("sell_by") == name else []
                label = state.get("sku") or state.get("name")
            else:
                quantities = [qty for line, qty in lines if _line_unit(line, item_unit) == name]
                if entity_type == "mfg_order":
                    label = run_name(state)
                elif template:
                    label = template_label(state, entity_id)
                else:
                    label = state.get("doc_number") or state.get("ref_id") or state.get("name")
            finer = [q for q in quantities if _finer_than(q, decimals)]
            if finer:
                (templates if template else holders).append(str(label or "-"))
                needed = max(needed, *(_places(q) for q in finer))
                partly_received = partly_received or _partly_received(entity_type, state)
        if not (holders or templates):
            continue
        # Split pieces share their parent's SKU, so a name is given once, in the order found.
        examples, named = (", ".join(list(dict.fromkeys(labels))[:3]) for labels in (holders, templates))
        current = old.get(name, needed)
        lead = f"{name} cannot go to {decimals} decimal places"
        change = "Change those quantities or finish those documents"
        cancel = f"set up new ones with quantities {name} allows"
        received = ("A purchase order or bill that received only part of its goods is finished by sending them "
                    "back with Return Goods, voiding it, and issuing it again for what arrived.")
        if not templates and partly_received:
            detail = refusal(
                "units.precision_lowered_under_received",
                f"{lead}: items or open documents still hold quantities with more ({examples}). {change} first. "
                f"{received} Or keep {current} decimal places.",
                unit=name, decimals=decimals, examples=examples, current=current)
        elif not templates:
            detail = refusal(
                "units.precision_lowered_under_quantities",
                f"{lead}: items or open documents still hold quantities with more ({examples}). {change} first, "
                f"or keep {current} decimal places.",
                unit=name, decimals=decimals, examples=examples, current=current)
        elif not holders:
            detail = refusal(
                "units.precision_lowered_under_templates",
                f"{lead}: subscriptions still list quantities with more ({named}). Cancel them and {cancel}, "
                f"or keep {current} decimal places.",
                unit=name, decimals=decimals, examples=named, current=current)
        else:
            detail = refusal(
                "units.precision_lowered_under_received_and_templates" if partly_received
                else "units.precision_lowered_under_quantities_and_templates",
                f"{lead}: items or open documents ({examples}) and subscriptions ({named}) still hold quantities "
                f"with more. {change}, and cancel those subscriptions and {cancel}."
                f"{f' {received}' if partly_received else ''} Or keep {current} decimal places.",
                unit=name, decimals=decimals, examples=examples, templates=named, current=current)
        raise HTTPException(status_code=422, detail=detail)


def validate_positive(qty: float, *, label: str = "Quantity") -> None:
    """Raise HTTP 422 if *qty* is not strictly positive."""
    if qty <= 0:
        raise HTTPException(
            status_code=422,
            detail=t("error.qty_positive", label=label),
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
    decimals = unit_decimals(sell_by, unit_map)
    if decimals is None:
        return
    if require_positive:
        validate_positive(qty, label=label)
    validate_quantity(qty, decimals, label=label)


def plain_number(value: float) -> str:
    """A number with every digit it holds and no trailing zeros, never rounded to a few
    significant digits or written in exponent form (12345.678 stays 12345.678, 7.0 is 7).
    It is rounded to 10 places first, the precision quantity writers round to, so a float
    sum such as 0.1 + 0.2 reads 0.3."""
    return format(Decimal(str(round(value, 10) + 0.0)).normalize(), "f")


def format_qty(value, unit_name: str | None, unit_map: dict[str, dict]) -> str:
    """Format a quantity value according to its unit's decimal precision.

    Returns a string with exactly `decimals` decimal places for known units,
    or every digit of the value without trailing zeros for unknown/absent units.
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
    # Unknown unit: no precision to apply, so show the value as stored.
    return plain_number(f)
