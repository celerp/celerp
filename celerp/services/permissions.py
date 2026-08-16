# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Roles-and-permissions registry: the single source of truth for who can do what.

ROLES derives from auth.ROLE_LEVELS so the numeric hierarchy stays
single-sourced; PERMISSIONS is the ordered catalogue of every role-gated
behavior, with per-company overrides stored under Company.settings
["role_grants"] as {permission_key: [role_key, ...]} - the explicit set of
roles granted each overridden permission. Only changed permissions are stored;
everything else resolves to its registry default set. Each role is granted
independently: a grant to one role never implies a grant to any other.
"""
from __future__ import annotations

import uuid
from typing import NamedTuple

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.company import Company
from celerp.services.auth import ROLE_LEVELS, get_current_company_id, get_current_role


class Role(NamedTuple):
    key: str
    label_key: str  # i18n key rendered through t() wherever a role name shows
    level: int


class Permission(NamedTuple):
    key: str
    label: str  # plain untranslated string, like the matrix rows it labels
    default_role: str
    grantable: bool  # False: fixed at default_role, overrides rejected
    floor_role: str  # lowest role an owner may set as this permission's minimum


ROLES: list[Role] = [
    Role(key=key, label_key=f"settings.{key}", level=level)
    for key, level in sorted(ROLE_LEVELS.items(), key=lambda kv: kv[1])
]

# floor_role: the lowest role an owner may set as a key's minimum. Every grantable
# key floors at viewer, so the owner-editable matrix can grant any read or write down
# to viewer. The one exception is manage_company_settings, whose floor is admin: it
# reaches the module data purge, which drops tables, so no override may hand it below
# admin. The floor also clamps every stored override up to at least the floor on read,
# so the invariant holds for grandfathered overrides, not only at save time.
PERMISSIONS: list[Permission] = [
    Permission("view_dashboards", "View dashboards", "viewer", True, "viewer"),
    Permission("view_documents", "View documents", "viewer", True, "viewer"),
    Permission("view_contacts", "View contacts", "viewer", True, "viewer"),
    Permission("view_inventory", "View inventory", "viewer", True, "viewer"),
    Permission("edit_documents", "Create & edit documents", "operator", True, "viewer"),
    Permission("edit_contacts", "Create & edit contacts", "operator", True, "viewer"),
    Permission("edit_inventory", "Create & edit inventory", "operator", True, "viewer"),
    Permission("edit_inventory_amounts", "Edit quantities & weights", "operator", True, "viewer"),
    Permission("finalize_documents", "Finalize & void documents", "operator", True, "viewer"),
    Permission("fulfill_documents", "Fulfill & receive documents", "operator", True, "viewer"),
    Permission("record_payments", "Record payments", "operator", True, "viewer"),
    Permission("manage_labels", "Manage label templates", "operator", True, "viewer"),
    Permission("manage_manufacturing", "Manage manufacturing", "operator", True, "viewer"),
    Permission("use_ai_assistant", "Use AI assistant", "operator", True, "viewer"),
    Permission("run_backups", "Run backups", "operator", True, "viewer"),
    Permission("view_subscriptions", "View subscriptions", "operator", True, "viewer"),
    Permission("view_inventory_costs", "See inventory costs", "manager", True, "viewer"),
    Permission("set_inventory_prices", "Set inventory prices", "manager", True, "viewer"),
    Permission("set_sales_doc_prices", "Set sales document prices", "operator", True, "viewer"),
    Permission("delete_documents", "Delete documents", "manager", True, "viewer"),
    Permission("adjust_inventory", "Adjust stock & bulk operations", "manager", True, "viewer"),
    Permission("revert_items_to_draft", "Revert items to draft", "manager", True, "viewer"),
    Permission("import_export_data", "Import / export data", "manager", True, "viewer"),
    Permission("view_payments", "View payments", "manager", True, "viewer"),
    Permission("view_financial_reports", "View financial reports", "manager", True, "viewer"),
    Permission("manage_accounting", "Manage accounting", "manager", True, "viewer"),
    Permission("manage_module_settings", "Manage module settings", "manager", True, "viewer"),
    Permission("manage_users", "Manage users", "admin", True, "viewer"),
    Permission("manage_company_settings", "Manage company settings", "admin", True, "admin"),
    Permission("manage_integrations", "Manage integrations", "admin", True, "viewer"),
    # Fixed rows. manage_permissions gates the matrix save itself: owner-only so
    # no owner can revoke their own ability to edit permissions. Lifecycle stays
    # with the account owner. Billing is enforced by the cloud account, not by
    # any endpoint here; the row renders as a fixed reference so the matrix
    # stays a complete statement of who can do what.
    Permission("manage_permissions", "Edit role permissions", "owner", False, "owner"),
    Permission("manage_company_lifecycle", "Deactivate, reset & reseed company", "owner", False, "owner"),
    Permission("manage_billing", "Manage billing", "owner", False, "owner"),
]

_PERMISSIONS_BY_KEY: dict[str, Permission] = {p.key: p for p in PERMISSIONS}


def is_permission_key(key: str) -> bool:
    """True when *key* is in the closed permission registry. Gating surfaces
    index the registry directly, so anything naming a key from outside core
    (a module manifest) must be checked here before it reaches them."""
    return key in _PERMISSIONS_BY_KEY


def resolved_grant_roles(settings: dict | None, key: str) -> set[str]:
    """The set of role keys granted *key* for this company.

    Stored override (Company.settings["role_grants"][key]) if present, else the
    registry default set {r : level(r) >= level(default_role)}. A stored set is
    intersected with the roles at or above floor_role, so a grandfathered or
    hand-edited grant can never drop a permission below its floor. A stored role
    that no longer exists in ROLE_LEVELS is ignored: never a crash, never an
    accidental grant. A fixed (non-grantable) permission never consults stored
    grants, so no crafted role_grants entry can widen an owner-only permission.
    Each role is resolved independently: membership carries no hierarchy.
    """
    perm = _PERMISSIONS_BY_KEY[key]
    floor = ROLE_LEVELS[perm.floor_role]
    if perm.grantable:
        grants = (settings or {}).get("role_grants") or {}
        if key in grants:
            stored = grants.get(key) or []
            return {r for r in stored if r in ROLE_LEVELS and ROLE_LEVELS[r] >= floor}
    default = ROLE_LEVELS[perm.default_role]
    return {r for r in ROLE_LEVELS if ROLE_LEVELS[r] >= default}


def role_has_permission(settings: dict | None, role: str, key: str) -> bool:
    """True when *role* is in the permission's resolved grant set.

    *role* is the already-migrated role key from get_current_role; an
    unrecognized role is absent from every set and so fails closed.
    """
    return role in resolved_grant_roles(settings, key)


def assert_role_permission(settings: dict | None, role: str, key: str) -> None:
    """Raise 403 naming the missing permission when *role* is not granted it."""
    if not role_has_permission(settings, role, key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires the {key} permission",
        )


async def get_current_company_settings(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """FastAPI dependency: the caller's company settings dict, read fresh.

    The single vehicle every route uses to resolve dynamic permissions, so a
    toggle takes effect on the next request; a missing company row yields {}.
    """
    company = await session.get(Company, company_id)
    return (company.settings if company else {}) or {}


def require_permission(key: str):
    """FastAPI dependency: 403 unless the caller's role holds *key*.

    Reads Company.settings fresh each request, so a permission toggle takes
    effect on the very next request; a missing company row resolves with
    registry defaults.
    """
    if key not in _PERMISSIONS_BY_KEY:
        raise KeyError(f"Unknown permission key: {key}")

    async def _guard(
        role: str = Depends(get_current_role),
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> None:
        company = await session.get(Company, company_id)
        assert_role_permission(company.settings if company else {}, role, key)

    # Recorded on the guard so a test can read back what each route requires.
    # Without it the key is only visible inside this closure, and "did exactly the
    # intended endpoints change?" stays a question for review rather than a test.
    _guard.required_permission = key
    return Depends(_guard)
