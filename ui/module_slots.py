# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which module slot contributions a page shows to the viewing role.

Every page that renders module contributions (inventory item actions, Pricing-tab
row actions, document detail actions and badges, catalog channels, bulk actions)
filters them here, so a module the company has switched off contributes nothing
and a role without a contribution's permission never sees it. Hiding is
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
    from celerp.modules.loader import CORE_FOLDED
    from celerp.modules.registry import get_enabled
    module = contribution.get("_module")
    if module and module not in CORE_FOLDED and "enabled_modules" in settings:
        if module not in get_enabled(settings):
            return False
    if not slot_permission_allows(contribution, settings, role):
        return False
    if "requires_connector" in contribution:
        required = contribution["requires_connector"]
        if not (isinstance(required, str) and required in (connected_connectors or set())):
            return False
    return True


def visible_slot_contributions(slot: str, settings: dict, role: str) -> list[dict]:
    """The contributions to a slot this role sees, in registration order."""
    from celerp.modules.slots import get as get_slot
    return [c for c in get_slot(slot) if module_contribution_visible(c, settings, role)]
