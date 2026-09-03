# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure-function tests for the field-visibility invariants on flattened items.

apply_field_visibility is a pure function over item dicts plus a field schema, a
role, and the caller's cost permission, so companion-key stripping (a derived key
dropped whenever any of its sources is hidden) and the cost-derived companion set
are tested here without a database.
"""
from __future__ import annotations

from celerp.services.cost_visibility import apply_field_visibility
from celerp_inventory.routes import DERIVED_FIELD_DEPS


# location_name is a schema field with a role floor; every other role sees it.
_SCHEMA = [
    {"key": "location_name", "type": "text", "visible_to_roles": ["admin", "manager"]},
    {"key": "quantity", "type": "number", "visible_to_roles": []},
]


def _item(**overrides) -> dict:
    base = {
        "id": "i1", "sku": "WDGT", "name": "Widget",
        "location_id": "loc-1", "location_name": "Main Store",
        "quantity": 5, "status": "available",
        "cost_price": 100.0, "cost_total": 120.0,
        "cost_base": 100.0, "cost_landed": 20.0,
        "landed_contributions": [{"name": "freight", "amount": 20.0}],
    }
    base.update(overrides)
    return base


def test_visibility_strips_location_id_companion():
    """AC1: location_id is a synthetic mirror of the schema field location_name. A
    role denied location_name must not see location_id either, or the hidden location
    is recoverable from the id via /companies/me/locations. The dependency is owned by
    the caller's DERIVED_FIELD_DEPS and stripped whenever location_name is stripped."""
    out = apply_field_visibility(
        [_item()], "operator", _SCHEMA, can_see_costs=True,
        derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert "location_name" not in out
    assert "location_id" not in out
    # A role at/above the floor keeps both.
    mgr = apply_field_visibility(
        [_item()], "manager", _SCHEMA, can_see_costs=True,
        derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert mgr["location_name"] == "Main Store"
    assert mgr["location_id"] == "loc-1"


def test_visibility_strips_cost_derived_keys():
    """AC2: cost_base, cost_landed, and landed_contributions are cost-DATA companions
    of the goods cost. A caller without view_inventory_costs must not see them, or the
    goods cost leaks through the derived keys the base cost strip missed."""
    out = apply_field_visibility(
        [_item()], "manager", _SCHEMA, can_see_costs=False,
        derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert "cost_price" not in out
    assert "cost_total" not in out
    assert "cost_base" not in out
    assert "cost_landed" not in out
    assert "landed_contributions" not in out
    # A cost-permitted caller keeps every cost key.
    ok = apply_field_visibility(
        [_item()], "manager", _SCHEMA, can_see_costs=True,
        derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert ok["cost_base"] == 100.0
    assert ok["cost_landed"] == 20.0
    assert ok["landed_contributions"] == [{"name": "freight", "amount": 20.0}]


def test_visibility_draft_author_keeps_cost_derived():
    """AC2 draft carve-out: the existing carve-out keeps cost keys visible on a DRAFT
    item for its edit_inventory author, so authoring can finish. The cost-derived
    companions must follow the same carve-out - stripped on a non-draft item, kept on
    a draft one - not dropped unconditionally. Green at merge-base only because the
    derived keys were never stripped there; paired with the strip test to pin that the
    new strip honors the draft branch."""
    draft = apply_field_visibility(
        [_item(status="draft")], "manager", _SCHEMA, can_see_costs=False,
        can_author_drafts=True, derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert draft["cost_base"] == 100.0
    assert draft["cost_landed"] == 20.0
    assert draft["landed_contributions"] == [{"name": "freight", "amount": 20.0}]
    # A non-draft item is still stripped for the same author.
    avail = apply_field_visibility(
        [_item(status="available")], "manager", _SCHEMA, can_see_costs=False,
        can_author_drafts=True, derived_field_deps=DERIVED_FIELD_DEPS,
    )[0]
    assert "cost_base" not in avail
    assert "cost_landed" not in avail
    assert "landed_contributions" not in avail
