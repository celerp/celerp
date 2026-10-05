# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
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


def is_enabled(company_settings: dict[str, Any] | None, module_name: str) -> bool:
    """Whether the company runs ``module_name``. A company whose settings predate
    per-module enablement (no enabled_modules key) runs every loaded module."""
    if _SETTINGS_KEY not in (company_settings or {}):
        return True
    return module_name in get_enabled(company_settings)


def set_enabled(company_settings: dict[str, Any], enabled: set[str]) -> dict[str, Any]:
    """Return an updated settings dict with the given enabled module set."""
    updated = dict(company_settings)
    updated[_SETTINGS_KEY] = sorted(enabled)
    return updated


def _current(company_settings: dict[str, Any]) -> set[str]:
    """The modules the company runs now: every module this process loaded when its
    settings predate per-module enablement (see is_enabled)."""
    if _SETTINGS_KEY not in company_settings:
        from celerp.modules.loader import loaded_modules

        return {m["name"] for m in loaded_modules()}
    return get_enabled(company_settings)


def enable(company_settings: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Return updated settings with module_name added to the modules the company runs."""
    enabled = _current(company_settings)
    enabled.add(module_name)
    return set_enabled(company_settings, enabled)


def disable(company_settings: dict[str, Any], module_name: str) -> dict[str, Any]:
    """Return updated settings with module_name removed from the modules the company runs."""
    enabled = _current(company_settings)
    enabled.discard(module_name)
    return set_enabled(company_settings, enabled)
