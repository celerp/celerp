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

from celerp.services.permissions import role_has_permission


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
    permission = contribution.get("permission")
    if permission and not role_has_permission(settings, role, permission):
        return False
    required = contribution.get("requires_connector")
    if required and required not in (connected_connectors or set()):
        return False
    return True


def visible_slot_contributions(slot: str, settings: dict, role: str) -> list[dict]:
    """The contributions to a slot this role sees, in registration order."""
    from celerp.modules.slots import get as get_slot
    return [c for c in get_slot(slot) if module_contribution_visible(c, settings, role)]
