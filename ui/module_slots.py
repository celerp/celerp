# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which module slot contributions a page shows to the viewing role.

Every page that renders module contributions (inventory item actions, Pricing-tab
row actions, document detail actions and badges, catalog channels, bulk actions)
filters them here, so a module the company has switched off contributes nothing,
a role without a contribution's permission never sees it, and an entry that
requires a connector shows only while the company is connected to it. Hiding is
presentation only: the module's own route must still check the permission.
"""
from __future__ import annotations

from celerp.services.permissions import is_permission_key, role_has_permission


def slot_permission_allows(contribution: dict, settings: dict, role: str) -> bool:
    """The permission gate on one slot entry, failing closed.

    The loader's rule: an entry without "permission" is not gated; one with it is
    shown only when the value is a registry key the role holds. A malformed value
    ("", 0, False, an unknown key) hides the entry rather than showing it to all.
    """
    if "permission" not in contribution:
        return True
    permission = contribution["permission"]
    return is_permission_key(permission) and role_has_permission(settings, role, permission)


def module_contribution_visible(
    contribution: dict, settings: dict, role: str,
    connected_connectors: set[str] | None = None,
) -> bool:
    """Apply company-module, permission, and optional connector gates uniformly."""
    from celerp.modules.registry import uses_module
    if not uses_module(settings, contribution.get("_module")):
        return False
    if not slot_permission_allows(contribution, settings, role):
        return False
    required = contribution.get("requires_connector")
    if required and not (isinstance(required, str) and required in (connected_connectors or set())):
        return False
    return True


def required_connectors(*slots: str) -> set[str]:
    """The connector ids the entries of these slots name in "requires_connector"."""
    from celerp.modules.slots import get as get_slot
    return {c["requires_connector"] for slot in slots for c in get_slot(slot) if c.get("requires_connector")}


async def connected_connector_ids(company_id: str, connectors: set[str]) -> set[str]:
    """Which of these connectors the company is connected to, read once per render.

    Nothing is read when no connector is asked for. A failed read returns none, so
    connector-gated entries are hidden rather than shown.
    """
    if not connectors:
        return set()
    try:
        from celerp.connectors.ownership import connected_connector_platforms
        from celerp.db import get_session_ctx
        async with get_session_ctx() as session:
            return await connected_connector_platforms(session, company_id, tuple(sorted(connectors)))
    except Exception:
        return set()


def visible_slot_contributions(
    slot: str, settings: dict, role: str, connected_connectors: set[str] | None,
) -> list[dict]:
    """The contributions to a slot this role sees, in registration order."""
    from celerp.modules.slots import get as get_slot
    return [c for c in get_slot(slot)
            if module_contribution_visible(c, settings, role, connected_connectors)]
