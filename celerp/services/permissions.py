# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Roles-and-permissions registry: the single source of truth for who can do what.

ROLES derives from auth.ROLE_LEVELS so the numeric hierarchy stays
single-sourced; PERMISSIONS is the ordered catalogue of every role-gated
behavior, with per-company overrides stored under Company.settings
["role_permissions"] as {permission_key: minimum_role_key}. Only changed
permissions are stored; everything else resolves to its registry default.
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

# floor_role: viewer for the view keys, operator for every write-capable key.
# The operator floor is what keeps viewers read-only now that the router-level
# read-only baseline is gone: no override can set a write key below it.
PERMISSIONS: list[Permission] = [
    Permission("view_dashboards", "View dashboards", "viewer", True, "viewer"),
    Permission("view_documents", "View documents", "viewer", True, "viewer"),
    Permission("view_contacts", "View contacts", "viewer", True, "viewer"),
    Permission("view_inventory", "View inventory", "viewer", True, "viewer"),
    Permission("edit_documents", "Create & edit documents", "operator", True, "operator"),
    Permission("edit_contacts", "Create & edit contacts", "operator", True, "operator"),
    Permission("edit_inventory", "Create & edit inventory", "operator", True, "operator"),
    Permission("finalize_documents", "Finalize & void documents", "operator", True, "operator"),
    Permission("fulfill_documents", "Fulfill & receive documents", "operator", True, "operator"),
    Permission("record_payments", "Record payments", "operator", True, "operator"),
    Permission("manage_labels", "Manage label templates", "operator", True, "operator"),
    Permission("manage_manufacturing", "Manage manufacturing", "operator", True, "operator"),
    Permission("use_ai_assistant", "Use AI assistant", "operator", True, "operator"),
    Permission("run_backups", "Run backups", "operator", True, "operator"),
    Permission("view_subscriptions", "View subscriptions", "operator", True, "operator"),
    Permission("view_inventory_costs", "See inventory costs", "manager", True, "operator"),
    Permission("set_inventory_prices", "Set inventory prices", "manager", True, "operator"),
    Permission("set_sales_doc_prices", "Set sales document prices", "operator", True, "operator"),
    Permission("delete_documents", "Delete documents", "manager", True, "operator"),
    Permission("adjust_inventory", "Adjust stock & bulk operations", "manager", True, "operator"),
    Permission("import_export_data", "Import / export data", "manager", True, "operator"),
    Permission("view_payments", "View payments", "manager", True, "operator"),
    Permission("view_financial_reports", "View financial reports", "manager", True, "operator"),
    Permission("manage_accounting", "Manage accounting", "manager", True, "operator"),
    Permission("manage_module_settings", "Manage module settings", "manager", True, "operator"),
    Permission("manage_users", "Manage users", "admin", True, "operator"),
    Permission("manage_company_settings", "Manage company settings", "admin", True, "operator"),
    Permission("manage_integrations", "Manage integrations", "admin", True, "operator"),
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


def permission_min_level(settings: dict | None, key: str) -> int:
    """Resolve the minimum role level for *key*: override if valid, else default.

    An override naming a role that no longer exists in ROLE_LEVELS is ignored
    in favor of the default: never a crash, never an accidental grant.
    """
    perm = _PERMISSIONS_BY_KEY[key]
    if perm.grantable:
        overrides = (settings or {}).get("role_permissions") or {}
        override_role = overrides.get(key)
        if override_role in ROLE_LEVELS:
            return ROLE_LEVELS[override_role]
    return ROLE_LEVELS[perm.default_role]


def role_has_permission(settings: dict | None, role: str, key: str) -> bool:
    """True when *role* meets the permission's resolved minimum.

    *role* is the already-migrated role key from get_current_role; an
    unrecognized role resolves to level 0 and fails closed.
    """
    return ROLE_LEVELS.get(role, 0) >= permission_min_level(settings, key)


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
