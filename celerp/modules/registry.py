# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module registry — enabled/disabled state.

Enabled modules are persisted in company.settings["enabled_modules"] as a list
of module names. This module provides read/write helpers that operate on that
settings key.

A change to the installation's load set takes effect at the next restart
(modules are loaded once at process startup); a company's own choice of the
modules already loaded takes effect immediately.
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


# -- one model: each company's own set, and the installation's load set --------
#
# A company's set is what it chose (settings["enabled_modules"]). A company that
# has never chosen (key absent) uses whatever the installation loads, and its
# first choice starts from that list. The installation loads the union of every
# company's set, closed over dependencies; config.toml [modules].enabled is only
# the mirror of that union a restart reads, written by sync_load_set alone.
#
# What a module that a company has turned off means for that company:
#   * its routes, pages, menu entries, slot contributions, search results and
#     per-company hooks are refused or left out for that company;
#   * its projection handlers keep applying to that company's existing and new
#     events while the module is loaded, so a turned-off module never leaves a
#     record half-built or rebuilt into a different shape. Turning it back on
#     shows the records exactly as they were.

# Advisory-lock key serializing load-set recomputation across processes.
_LOAD_SET_LOCK_KEY = 0x43454C4552500002


class ModuleStillNeeded(ValueError):
    """Turning off a module another module of the same company depends on."""

    def __init__(self, module_name: str, needed_by: list[str]):
        self.module_name = module_name
        self.needed_by = needed_by
        super().__init__(f"{module_name} is needed by {', '.join(needed_by)}")


def uses_module(company_settings: dict[str, Any] | None, module_name: str | None) -> bool:
    """Whether a company uses *module_name*: the one per-company enablement rule.

    Core (no module name) and core-folded components are always on. A company
    that has never chosen uses every loaded module; otherwise only the ones in
    its set."""
    from celerp.modules.loader import CORE_FOLDED
    if not module_name or module_name in CORE_FOLDED:
        return True
    if not company_settings or _SETTINGS_KEY not in company_settings:
        return True
    return module_name in get_enabled(company_settings)


def _configured_load_set() -> list[str]:
    from celerp.config import read_config
    return list(read_config().get("modules", {}).get("enabled") or [])


def dependency_closure(names) -> list[str]:
    """*names* and every module they depend on, in install order."""
    from celerp.config import _install_closure
    return _install_closure(sorted(names))


def company_modules(company_settings: dict[str, Any] | None) -> set[str]:
    """The company's set, starting from the installation's list on a first choice."""
    if company_settings and _SETTINGS_KEY in company_settings:
        return get_enabled(company_settings)
    return set(_configured_load_set())


def enable_for_company(company_settings: dict[str, Any] | None, module_name: str) -> tuple[dict[str, Any], list[str]]:
    """Settings with *module_name* and its dependencies on, and the dependencies
    this turned on as well (so the caller can say so)."""
    current = company_modules(company_settings)
    closure = set(dependency_closure([module_name])) | {module_name}
    added = sorted(closure - current - {module_name})
    return set_enabled(dict(company_settings or {}), current | closure), added


def disable_for_company(company_settings: dict[str, Any] | None, module_name: str) -> dict[str, Any]:
    """Settings with *module_name* off. Refused while another module the company
    uses depends on it."""
    current = company_modules(company_settings) - {module_name}
    needed_by = sorted(n for n in current if module_name in dependency_closure([n]))
    if needed_by:
        raise ModuleStillNeeded(module_name, needed_by)
    return set_enabled(dict(company_settings or {}), current)


async def load_set(session) -> list[str]:
    """Every module some company uses, closed over dependencies."""
    from sqlalchemy import select
    from celerp.models.company import Company

    union: set[str] = set()
    for company_settings in (await session.scalars(select(Company.settings))).all():
        union |= company_modules(company_settings)
    return dependency_closure(union)


def restart_needed(names) -> bool:
    """Whether a restart is needed to load any of *names*."""
    from celerp.modules.loader import is_running, restart_would_load
    return any(not is_running(n) and restart_would_load(n) for n in names)


async def sync_load_set(session) -> None:
    """Recompute the load set from every company and write its mirror.

    Called inside the writer's transaction after it changed a company's set and
    before it commits: the lock serializes recomputation, and the union read here
    sees this writer's change and every committed one. A module no company uses
    any more is already refused to every company and leaves the process at the
    next restart, so turning one off never needs a restart."""
    import asyncio
    from sqlalchemy import text
    from celerp.config import replace_enabled_modules

    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOAD_SET_LOCK_KEY})
    names = await load_set(session)
    await asyncio.to_thread(replace_enabled_modules, names)
