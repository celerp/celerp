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


def apply_field_visibility(
    items: list[dict], role: str, field_schema: list[dict], can_see_costs: bool
) -> list[dict]:
    """Strip fields from item dicts that the caller is not allowed to see.

    Two sources of restrictions:
    1. Schema-driven: a field has visible_to_roles set and the caller's role is below
       its minimum. This stays role-based - it is per-company field configuration.
    2. Permission-driven: the cost fields require the view_inventory_costs permission.

    Cost keys are governed only by the permission, so the cost column's own
    visible_to_roles floor never double-gates them: a granted operator sees cost,
    an ungranted manager does not.
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
    if not can_see_costs:
        restricted |= COST_ITEM_KEYS
    if not restricted:
        return items
    return [{k: v for k, v in item.items() if k not in restricted} for item in items]
