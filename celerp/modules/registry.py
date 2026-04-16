# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Module registry — enabled/disabled state.

Enabled modules are persisted in company.settings["enabled_modules"] as a list
of module names. This module provides read/write helpers that operate on that
settings key.

Note: changes to enabled state require a restart (modules are loaded once at
process startup). The settings UI shows a restart-required banner after any toggle.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Key used in company.settings JSON blob
_SETTINGS_KEY = "enabled_modules"


def get_enabled(company_settings: dict[str, Any] | None) -> set[str]:
    """Return the set of enabled module names from company settings.

    Returns empty set if the key is absent — no implicit defaults.
    """
    if not company_settings:
        return set()
    raw = company_settings.get(_SETTINGS_KEY)
    if raw is None:
        return set()
    if isinstance(raw, list):
        return set(raw)
    log.warning("enabled_modules in company settings is not a list (%r) — using empty set", raw)
    return set()


def set_enabled(company_settings: dict[str, Any], enabled: set[str]) -> dict[str, Any]:
    """Return an updated settings dict with the given enabled module set."""
    updated = dict(company_settings)
    updated[_SETTINGS_KEY] = sorted(enabled)
    return updated


def enable(company_settings: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Return updated settings with module_name added to enabled set."""
    enabled = get_enabled(company_settings)
    enabled.add(module_name)
    return set_enabled(company_settings, enabled)


def disable(company_settings: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Return updated settings with module_name removed from enabled set."""
    enabled = get_enabled(company_settings)
    enabled.discard(module_name)
    return set_enabled(company_settings, enabled)
