# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""expand the role_permissions threshold overrides into explicit role_grants sets

Revision ID: b3c4d5e6f7a8
Revises: f8a9b0c1d2e3
Create Date: 2026-08-09

The data half of the per-role permissions change. Role overrides used to be a
single minimum-role threshold per permission (Company.settings["role_permissions"]
= {permission_key: minimum_role_key}); every role at or above that threshold was
granted, so the roles moved as a block. They are now an explicit set of granted
roles per permission (Company.settings["role_grants"] = {permission_key:
[role_key, ...]}), each role granted independently. This migration rewrites every
stored threshold into the exact set of roles it used to grant, so no company's
effective permissions change, then drops the retired key.

Deltas only: it materializes a grant set for the keys a company actually
overrode, never the whole catalogue; unoverridden keys keep resolving to their
registry default. Each threshold expands to every role at or above it, clamped up
to the permission's floor (so a grandfathered sub-floor override lands on the
floor exactly as the resolver reads it). A threshold on a permission that is no
longer overridable, unknown to the catalogue, or naming a role that no longer
exists never resolved to anything but the default, so it becomes no delta and is
left absent.

Signature-less (data only), so the develop-to-release reconcile replays it on
every create_all/restore database. Idempotent: guarded on the presence of the
retired key, so a second application is a no-op. The downgrade is the best-effort
inverse: it collapses each grant set back to a single threshold at its lowest
granted role and restores the retired key, so a rollback to the threshold-reading
code keeps a company's effective permissions. This round-trips exactly for the
contiguous sets the upgrade produces; a hand-authored non-contiguous set is
documented lossy (it collapses to its lowest role, which the older resolver then
re-expands as a block).

The catalogue (floor role and overridability per permission) and the role levels
are snapshotted here as literals, because a migration is immutable history: it
must expand exactly as the rules stood when it was written, even after the
registry in celerp.services.permissions later changes.
"""

from __future__ import annotations

from alembic import op

from celerp.migrations._json_compat import update_company_settings

revision = "b3c4d5e6f7a8"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

# Snapshot of auth.ROLE_LEVELS at this revision.
_ROLE_LEVELS = {"viewer": 1, "operator": 2, "manager": 3, "admin": 4, "owner": 5}

# Snapshot of the permission catalogue at this revision: key -> (floor_role,
# grantable). default_role is irrelevant here because every key processed carries
# an explicit override; only the floor (which clamps the expansion) and whether
# the key is overridable at all matter.
_CATALOGUE = {
    "view_dashboards": ("viewer", True),
    "view_documents": ("viewer", True),
    "view_contacts": ("viewer", True),
    "view_inventory": ("viewer", True),
    "edit_documents": ("viewer", True),
    "edit_contacts": ("viewer", True),
    "edit_inventory": ("viewer", True),
    "edit_inventory_amounts": ("viewer", True),
    "finalize_documents": ("viewer", True),
    "fulfill_documents": ("viewer", True),
    "record_payments": ("viewer", True),
    "manage_labels": ("viewer", True),
    "manage_manufacturing": ("viewer", True),
    "use_ai_assistant": ("viewer", True),
    "run_backups": ("viewer", True),
    "view_subscriptions": ("viewer", True),
    "view_inventory_costs": ("viewer", True),
    "set_inventory_prices": ("viewer", True),
    "set_sales_doc_prices": ("viewer", True),
    "delete_documents": ("viewer", True),
    "adjust_inventory": ("viewer", True),
    "import_export_data": ("viewer", True),
    "view_payments": ("viewer", True),
    "view_financial_reports": ("viewer", True),
    "manage_accounting": ("viewer", True),
    "manage_module_settings": ("viewer", True),
    "manage_users": ("viewer", True),
    "manage_company_settings": ("admin", True),
    "manage_integrations": ("viewer", True),
    "manage_permissions": ("owner", False),
    "manage_company_lifecycle": ("owner", False),
    "manage_billing": ("owner", False),
}


def _expand(stored_role: str, floor_role: str) -> list[str]:
    """The role keys a threshold used to grant: every role at or above the
    threshold, clamped up to the floor, ordered low to high."""
    threshold = max(_ROLE_LEVELS[stored_role], _ROLE_LEVELS[floor_role])
    return sorted(
        (r for r, lvl in _ROLE_LEVELS.items() if lvl >= threshold),
        key=lambda r: _ROLE_LEVELS[r],
    )


def _collapse(granted_roles: list[str]) -> str | None:
    """Best-effort inverse of _expand: the threshold role at the lowest granted
    level. Exact for a contiguous set (the shape upgrade produces); lossy for a
    non-contiguous set, which no longer round-trips to the same set. None when no
    granted role is known, leaving the permission absent (resolves to default)."""
    levels = [_ROLE_LEVELS[r] for r in granted_roles if r in _ROLE_LEVELS]
    if not levels:
        return None
    low = min(levels)
    return next(r for r, lvl in _ROLE_LEVELS.items() if lvl == low)


def upgrade() -> None:
    conn = op.get_bind()

    def _mutate(data: dict, _row) -> bool:
        if "role_permissions" not in data:
            return False
        role_perms = data.get("role_permissions")
        grants = dict(data.get("role_grants") or {})
        if isinstance(role_perms, dict):
            for key, stored_role in role_perms.items():
                meta = _CATALOGUE.get(key)
                if meta is None:
                    continue
                floor_role, grantable = meta
                if not grantable:
                    continue
                if stored_role not in _ROLE_LEVELS:
                    continue
                grants[key] = _expand(stored_role, floor_role)
        if grants:
            data["role_grants"] = grants
        data.pop("role_permissions", None)
        return True

    update_company_settings(conn, _mutate)


def downgrade() -> None:
    conn = op.get_bind()

    def _mutate(data: dict, _row) -> bool:
        if "role_grants" not in data:
            return False
        grants = data.get("role_grants")
        role_perms = dict(data.get("role_permissions") or {})
        if isinstance(grants, dict):
            for key, roles in grants.items():
                threshold = _collapse(roles if isinstance(roles, list) else [])
                if threshold is not None:
                    role_perms[key] = threshold
        if role_perms:
            data["role_permissions"] = role_perms
        data.pop("role_grants", None)
        return True

    update_company_settings(conn, _mutate)
