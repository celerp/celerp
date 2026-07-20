# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Dynamic role-permission registry and resolver tests.

Covers:
- ROLES derived from auth.ROLE_LEVELS with i18n label keys
- PERMISSIONS catalogue: keys, defaults, floors, fixed rows
- Resolver: defaults, overrides, stale overrides, fail-closed roles
- assert_role_permission 403 shape
- Threshold monotonicity
"""
from __future__ import annotations

import pytest


# ── Registry shape ────────────────────────────────────────────────────────────

def test_roles_derived_from_role_levels():
    """ROLES keys and order match auth.ROLE_LEVELS; each carries a label_key."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import ROLES

    assert [r.key for r in ROLES] == sorted(ROLE_LEVELS, key=ROLE_LEVELS.get)
    for r in ROLES:
        assert r.level == ROLE_LEVELS[r.key]
        assert r.label_key == f"settings.{r.key}"


# The full 2.1 catalogue: key -> (default_min_role, grantable, floor_role).
# Defaults reproduce the thresholds the enforcement sites carry today.
EXPECTED_CATALOGUE = {
    # viewer defaults (view keys floor at viewer)
    "view_dashboards": ("viewer", True, "viewer"),
    "view_documents": ("viewer", True, "viewer"),
    "view_contacts": ("viewer", True, "viewer"),
    "view_inventory": ("viewer", True, "viewer"),
    # operator defaults (write-capable keys floor at operator)
    "edit_documents": ("operator", True, "operator"),
    "edit_contacts": ("operator", True, "operator"),
    "edit_inventory": ("operator", True, "operator"),
    "finalize_documents": ("operator", True, "operator"),
    "fulfill_documents": ("operator", True, "operator"),
    "record_payments": ("operator", True, "operator"),
    "set_sales_doc_prices": ("operator", True, "operator"),
    "manage_labels": ("operator", True, "operator"),
    "manage_manufacturing": ("operator", True, "operator"),
    "use_ai_assistant": ("operator", True, "operator"),
    "run_backups": ("operator", True, "operator"),
    "view_subscriptions": ("operator", True, "operator"),
    # manager defaults
    "set_inventory_prices": ("manager", True, "operator"),
    "delete_documents": ("manager", True, "operator"),
    "adjust_inventory": ("manager", True, "operator"),
    "import_export_data": ("manager", True, "operator"),
    "view_payments": ("manager", True, "operator"),
    "view_financial_reports": ("manager", True, "operator"),
    "manage_accounting": ("manager", True, "operator"),
    "manage_module_settings": ("manager", True, "operator"),
    # admin defaults
    "manage_users": ("admin", True, "operator"),
    "manage_company_settings": ("admin", True, "operator"),
    "manage_integrations": ("admin", True, "operator"),
    # fixed rows: owner-only, never grantable
    "manage_permissions": ("owner", False, "owner"),
    "manage_company_lifecycle": ("owner", False, "owner"),
    "manage_billing": ("owner", False, "owner"),
}


def test_permissions_registry_shape():
    """The registry holds exactly the catalogue keys with their defaults and floors."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert set(by_key) == set(EXPECTED_CATALOGUE)
    for key, (default, grantable, floor) in EXPECTED_CATALOGUE.items():
        p = by_key[key]
        assert p.default_min_role == default, key
        assert p.grantable is grantable, key
        assert p.floor_role == floor, key
        assert p.label, key


def test_fixed_rows_not_grantable():
    """manage_permissions, manage_company_lifecycle, manage_billing are fixed."""
    from celerp.services.permissions import PERMISSIONS

    fixed = {p.key for p in PERMISSIONS if not p.grantable}
    assert fixed == {"manage_permissions", "manage_company_lifecycle", "manage_billing"}


# ── Resolver ──────────────────────────────────────────────────────────────────

def test_permission_min_level_default():
    """Empty overrides resolve to the registry default level."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    assert permission_min_level({}, "set_inventory_prices") == ROLE_LEVELS["manager"]
    assert permission_min_level({}, "set_sales_doc_prices") == ROLE_LEVELS["operator"]
    assert permission_min_level({}, "view_inventory") == ROLE_LEVELS["viewer"]


def test_permission_min_level_override():
    """An override {perm_key: role_key} changes the resolved level."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"set_inventory_prices": "operator"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["operator"]
    settings = {"role_permissions": {"set_sales_doc_prices": "manager"}}
    assert permission_min_level(settings, "set_sales_doc_prices") == ROLE_LEVELS["manager"]


def test_permission_min_level_stale_override_ignored():
    """Overrides naming unknown roles or permissions fall back to the default."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"set_inventory_prices": "archduke"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["manager"]
    settings = {"role_permissions": {"no_such_permission": "operator"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["manager"]


def test_permission_min_level_fixed_rows_ignore_overrides():
    """Fixed rows resolve at owner even when the blob carries an override."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"manage_permissions": "viewer"}}
    assert permission_min_level(settings, "manage_permissions") == ROLE_LEVELS["owner"]


def test_role_has_permission_unknown_role_fails_closed():
    """An unmapped role resolves to level 0 and fails closed.

    The resolver receives already-migrated roles from get_current_role;
    legacy-alias migration itself stays covered by TestLegacyRoleMigration
    in tests/test_permissions.py.
    """
    from celerp.services.permissions import role_has_permission

    assert role_has_permission({}, "salesperson", "set_sales_doc_prices") is False
    assert role_has_permission({}, "operator", "set_sales_doc_prices") is True


def test_assert_role_permission_raises_403():
    """Denied caller gets a 403 whose message names the permission."""
    from fastapi import HTTPException
    from celerp.services.permissions import assert_role_permission

    with pytest.raises(HTTPException) as exc:
        assert_role_permission({}, "operator", "set_inventory_prices")
    assert exc.value.status_code == 403
    assert "set_inventory_prices" in exc.value.detail
    # A granted caller passes silently.
    assert_role_permission({}, "manager", "set_inventory_prices")


def test_threshold_monotonic():
    """Granting a lower role implies every higher role passes."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import role_has_permission

    settings = {"role_permissions": {"set_inventory_prices": "operator"}}
    for role, level in ROLE_LEVELS.items():
        expected = level >= ROLE_LEVELS["operator"]
        assert role_has_permission(settings, role, "set_inventory_prices") is expected, role


def test_defaults_match_current_behavior():
    """With empty overrides every registry entry resolves to the minimum role
    its enforcement sites carry today; the old static table's rows are a subset."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import PERMISSIONS, permission_min_level

    for p in PERMISSIONS:
        expected_role, _, _ = EXPECTED_CATALOGUE[p.key]
        assert permission_min_level({}, p.key) == ROLE_LEVELS[expected_role], p.key
