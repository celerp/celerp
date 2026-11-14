# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Role-gated cost redaction for activity/history payloads.

Cost (``cost_price`` / ``cost_total`` and any cost price list) is visible only to manager+
everywhere else in the app (item detail, list, dashboard cost basis). The ledger/activity feeds must honour
the same rule, enforced **server-side** so raw cost never ships to an under-threshold
role. This is the single source of truth for that redaction, used by both the ledger
router and the dashboard activity endpoints.

Every ledger reader (the event list, a single event, the dashboard activity and History)
shows an event only to a role holding the permission it is read with: the one its module names
on its ``projection_handler`` contribution, enabled or not, or core's own for the events no
module owns.
"""

from __future__ import annotations

from sqlalchemy import Select, case, false, true

from celerp.events.schemas import ItemPricingSet
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import is_cost_price_type

# Every amount an item.pricing.set carries (all its fields but price_type), so an amount
# field added to the event later is redacted with the rest.
_PRICING_AMOUNT_KEYS = frozenset(ItemPricingSet.model_fields) - {"price_type"}

# Every cost-bearing key that can appear in event data (top-level or in fields_changed):
# item cost, COGS on fulfillment, manufacturing unit cost. Sell prices (retail_price etc.)
# are deliberately excluded - they are visible to all roles, matching item-level visibility.
COST_DATA_KEYS = frozenset({"cost_price", "cost_total", "total_cogs", "cogs", "unit_cost"})


# The events no module owns, each read with the permission of the records it changes. The
# longest prefix an event type starts with decides.
_CORE_EVENT_PERMISSIONS = {
    "sys.older_stock.": "view_inventory",
    "sys.": "manage_company_settings",
    "payment_batch.": "view_payments",
    "line_action.": "view_documents",
    "sub.": "view_subscriptions",
    "bom.": "manage_manufacturing",
}
# An event that neither an installed module nor core names (a removed module's) is read by
# those who manage the company.
_UNOWNED_EVENT_PERMISSION = "manage_company_settings"


def _event_permissions() -> list[tuple[str, str]]:
    """(prefix, permission) for every known event prefix, longest first."""
    from celerp.modules import slots
    from celerp.modules.loader import event_read_permissions

    found = {**_CORE_EVENT_PERMISSIONS, **event_read_permissions(),
             **{c["prefix"]: c["permission"] for c in slots.get("projection_handler")}}
    return sorted(found.items(), key=lambda kv: -len(kv[0]))


def can_read_event(event_type: str, settings: dict | None, role: str | None) -> bool:
    """Whether this role may read an event of ``event_type``."""
    permission = next((perm for prefix, perm in _event_permissions() if event_type.startswith(prefix)),
                      _UNOWNED_EVENT_PERMISSION)
    return role_has_permission(settings, role or "", permission)


def readable_events(stmt: Select, settings: dict | None, role: str | None) -> Select:
    """``stmt`` over ledger entries narrowed to the events this role can read."""
    from celerp.models.ledger import LedgerEntry

    held = lambda perm: role_has_permission(settings, role or "", perm)  # noqa: E731
    rules = [(LedgerEntry.event_type.startswith(prefix, autoescape=True), true() if held(perm) else false())
             for prefix, perm in _event_permissions()]
    return stmt.where(case(*rules, else_=true() if held(_UNOWNED_EVENT_PERMISSION) else false()))


def can_see_costs(settings: dict | None, role: str | None) -> bool:
    """True when the role holds view_inventory_costs for this company."""
    return role_has_permission(settings, role or "", "view_inventory_costs")


def redact_event_costs(event_type: str, data: dict) -> dict:
    """Return ``data`` with every cost amount stripped. Pure (returns a copy only when it
    changes something); rows are kept (counts/pagination stay intact), only cost numbers go.

    Covers all cost-bearing event data: ``item.pricing.set`` for a cost price_type (a cost
    key or a cost price list: every amount, the new value and the one it replaced);
    top-level cost keys (item cost on item.created/snapshot/patched, ``total_cogs`` on
    doc fulfillment, manufacturing ``unit_cost``); ``fields_changed`` cost keys; transform
    parent/child cost totals; and split ``children_detail`` cost deltas. Sell prices
    (``*_price`` other than ``cost_price``) are deliberately left visible.

    Read-time only — projection replay reads the raw ledger directly, so cost stays intact
    for state rebuilds; this just keeps cost out of API responses to under-manager roles.
    """
    if not isinstance(data, dict):
        return data

    # Pricing event for a cost price_type (a cost key or a cost list) → drop every amount
    # (all fields but price_type), keep the label-only row.
    if event_type == "item.pricing.set" and is_cost_price_type(str(data.get("price_type") or "")):
        out = {k: v for k, v in data.items() if k not in _PRICING_AMOUNT_KEYS}
        out["cost_redacted"] = True
        return out

    fc = data.get("fields_changed")
    cd = data.get("children_detail")
    needs = (
        any(k in data for k in COST_DATA_KEYS)
        or any(k in data for k in ("parent_cost_total", "child_cost_total"))
        or (isinstance(fc, dict) and any(k in fc for k in COST_DATA_KEYS))
        or (isinstance(cd, list) and any(isinstance(c, dict) and any(str(kk).startswith("cost_") for kk in c) for c in cd))
    )
    if not needs:
        return data

    out = {k: v for k, v in data.items()
           if k not in COST_DATA_KEYS and k not in ("parent_cost_total", "child_cost_total")}
    if isinstance(fc, dict):
        out["fields_changed"] = {k: v for k, v in fc.items() if k not in COST_DATA_KEYS}
    if isinstance(cd, list):
        out["children_detail"] = [
            {kk: vv for kk, vv in c.items() if not str(kk).startswith("cost_")}
            if isinstance(c, dict) else c
            for c in cd
        ]
    return out


def redact_entries_for_role(entries: list[dict], settings: dict | None, role: str | None) -> list[dict]:
    """Apply :func:`redact_event_costs` to a list of serialized ledger entries unless the
    role may see costs. Entry dicts must carry ``event_type`` and ``data``."""
    if can_see_costs(settings, role):
        return entries
    for e in entries:
        e["data"] = redact_event_costs(str(e.get("event_type") or ""), e.get("data") or {})
    return entries
