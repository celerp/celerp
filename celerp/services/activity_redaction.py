# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Role-gated cost redaction for activity/history payloads.

Cost (``cost_price`` / ``cost_total``) is visible only to manager+ everywhere else in
the app (item detail, list, dashboard cost basis). The ledger/activity feeds must honour
the same rule, enforced **server-side** so raw cost never ships to an under-threshold
role. This is the single source of truth for that redaction, used by both the ledger
router and the dashboard activity endpoints.
"""

from __future__ import annotations

from celerp.services.permissions import role_has_permission

# The cost fields gated by view_inventory_costs (mirrors the inventory item-visibility rule).
COST_FIELD_KEYS = frozenset({"cost_price", "cost_total"})

# Every cost-bearing key that can appear in event data (top-level or in fields_changed):
# item cost, COGS on fulfillment, manufacturing unit cost. Sell prices (retail_price etc.)
# are deliberately excluded - they are visible to all roles, matching item-level visibility.
COST_DATA_KEYS = frozenset({"cost_price", "cost_total", "total_cogs", "cogs", "unit_cost"})


def can_see_costs(settings: dict | None, role: str | None) -> bool:
    """True when the role holds view_inventory_costs for this company."""
    return role_has_permission(settings, role or "", "view_inventory_costs")


def redact_event_costs(event_type: str, data: dict) -> dict:
    """Return ``data`` with every cost amount stripped. Pure (returns a copy only when it
    changes something); rows are kept (counts/pagination stay intact), only cost numbers go.

    Covers all cost-bearing event data: ``item.pricing.set`` for a cost price_type;
    top-level cost keys (item cost on item.created/snapshot/patched, ``total_cogs`` on
    doc fulfillment, manufacturing ``unit_cost``); ``fields_changed`` cost keys; transform
    parent/child cost totals; and split ``children_detail`` cost deltas. Sell prices
    (``*_price`` other than ``cost_price``) are deliberately left visible.

    Read-time only — projection replay reads the raw ledger directly, so cost stays intact
    for state rebuilds; this just keeps cost out of API responses to under-manager roles.
    """
    if not isinstance(data, dict):
        return data

    # Pricing event for a cost price_type → drop the amount, keep the (label-only) row.
    if event_type == "item.pricing.set" and data.get("price_type") in COST_FIELD_KEYS:
        out = {k: v for k, v in data.items() if k != "new_price"}
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
