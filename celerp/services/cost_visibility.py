# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Cost-field visibility applied to serialized item dicts.

Shared by every surface that returns flattened item data (inventory API,
label printing), so cost stripping behaves identically everywhere. Cost
visibility itself is decided by the caller through the view_inventory_costs
permission (celerp.services.permissions) and passed in as ``can_see_costs``;
the schema-driven visible_to_roles restriction stays role-based because it is
per-company field configuration, not a hardcoded gate.
"""
from __future__ import annotations

from celerp.services.auth import ROLE_LEVELS

# Item-dict keys stripped when the caller lacks view_inventory_costs.
COST_ITEM_KEYS: frozenset[str] = frozenset({"cost_price", "cost_total"})
# Read-side cost companions: derived cost data that a cost-hidden caller must not see
# either, or the goods cost leaks through a value computed off it. Kept separate from
# COST_ITEM_KEYS because that set also drives write-path price classification; these are
# stripped only, never written or validated against.
COST_DERIVED_ITEM_KEYS: frozenset[str] = frozenset({"cost_base", "cost_landed", "landed_contributions"})


def apply_field_visibility(
    items: list[dict], role: str, field_schema: list[dict], can_see_costs: bool,
    can_author_drafts: bool = False,
    derived_field_deps: dict[str, tuple[str, ...]] | None = None,
) -> list[dict]:
    """Strip fields from item dicts that the caller is not allowed to see.

    Two sources of restrictions:
    1. Schema-driven: a field has visible_to_roles set and the caller's role is below
       its minimum. This stays role-based - it is per-company field configuration.
    2. Permission-driven: the cost fields require the view_inventory_costs permission.

    Cost keys are governed only by the permission, so the cost column's own
    visible_to_roles floor never double-gates them: a granted operator sees cost,
    an ungranted manager does not.

    Draft carve-out: cost stripping attaches when an item is committed to
    available, not at creation. When ``can_author_drafts`` (the caller holds
    edit_inventory), a DRAFT item keeps its cost keys so its creator can finish
    authoring it; every non-draft item is stripped as before.

    Derived fields: ``derived_field_deps`` maps a computed key to the source keys
    it is derived from (e.g. qty_each from quantity and pieces). A derived key is
    stripped whenever ANY of its sources is stripped, so a caller cannot recover a
    hidden source from a value computed off it. The rule is stated once by the
    caller; this service stays generic and holds no field names of its own.
    """
    caller_level = ROLE_LEVELS.get(role, 0)
    restricted = {
        f["key"]
        for f in field_schema
        if f.get("visible_to_roles") and caller_level < min(
            ROLE_LEVELS.get(r, 0) for r in f["visible_to_roles"]
        )
    }
    restricted -= COST_ITEM_KEYS
    cost_hidden = not can_see_costs
    if not restricted and not cost_hidden:
        return items
    out = []
    for item in items:
        drop = set(restricted)
        if cost_hidden and not (
            can_author_drafts and str(item.get("status") or "").lower() == "draft"
        ):
            drop |= COST_ITEM_KEYS | COST_DERIVED_ITEM_KEYS
        if derived_field_deps:
            for derived, sources in derived_field_deps.items():
                if any(s in drop for s in sources):
                    drop.add(derived)
        out.append({k: v for k, v in item.items() if k not in drop} if drop else item)
    return out
