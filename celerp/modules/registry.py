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
# the mirror of that union a restart reads, written by commit_with_load_set alone.
#
# What a module that a company has turned off means for that company:
#   * its routes, pages, menu entries, slot contributions, search results and
#     per-company hooks are refused or left out for that company;
#   * its projection handlers keep applying to that company's existing and new
#     events while the module is loaded, so a turned-off module never leaves a
#     record half-built or rebuilt into a different shape. Turning it back on
#     shows the records exactly as they were.

# Advisory-lock key of the one module-state boundary (hold_module_state).
_MODULE_STATE_LOCK_KEY = 0x43454C4552500002


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


async def hold_module_state(session, *, bounded: bool = False) -> None:
    """Hold the module-state boundary until *session*'s transaction ends.

    Every change to which modules a company uses takes it before it checks the
    modules it names and holds it through its commit; deleting a module or purging
    its data takes it before its in-use check and holds it until the folder is gone
    or the tables are dropped. So a check never goes stale before the change it
    allows: a module is never removed while a company comes to use it. A holder can
    run as long as a company restore, so the wait has no time limit, unless
    *bounded*: then it gives up at the request lock timeout (a lock-timeout database
    error), for a caller that must answer within a known time (turning a module off).

    Celerp runs on Postgres, embedded or external. Another dialect (SQLite in unit
    tests) has no advisory locks and serves one request at a time there, so this is
    a no-op on it."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return
    if bounded:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MODULE_STATE_LOCK_KEY})
        return
    await session.execute(text("SET LOCAL lock_timeout = 0"))
    await session.execute(text("SET LOCAL statement_timeout = 0"))
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MODULE_STATE_LOCK_KEY})
    await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    await session.execute(text("SET LOCAL statement_timeout TO DEFAULT"))


async def share_module_state(session) -> None:
    """Hold the module-state boundary in shared mode until *session*'s transaction ends.

    Work that uses a company's modules is admitted under it (boundary.module_use):
    holders never wait for each other, and a change to which modules a company uses
    (hold_module_state) waits for every holder and is waited for by them, up to the
    request lock timeout (a lock-timeout database error), so the admission reads
    settings no change is part way through. A no-op off Postgres, as hold_module_state is."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": _MODULE_STATE_LOCK_KEY})


async def _named_lock(session, key: str, mode: str) -> None:
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text(f"SELECT {mode}(hashtextextended(:k, 0))"), {"k": key})


async def _company_module_lock(session, company_id, mode: str) -> None:
    await _named_lock(session, f"module-use:{company_id}", mode)


async def hold_module_gate(session, company_id, module_name: str) -> None:
    """Keep new work in *module_name* for *company_id* from being admitted until
    *session*'s transaction ends (share_module_gate). Taken by turning that module off
    while it waits for the work already admitted, so other modules and other companies
    are never held up by it. First in the lock order, before the module-state boundary."""
    await _named_lock(session, f"module-gate:{company_id}:{module_name}", "pg_advisory_xact_lock")


async def share_module_gate(session, company_id, module_name: str) -> None:
    """Hold, until *session*'s transaction ends, *module_name* for *company_id* as it is:
    admission of work in it (boundary.module_use) takes this before the module-state
    boundary, so it never reads the settings while that module is being turned off for
    that company. Holders never wait for each other."""
    await _named_lock(session, f"module-gate:{company_id}:{module_name}", "pg_advisory_xact_lock_shared")


async def hold_company_modules(session, company_id) -> None:
    """Keep the module work of *company_id* from writing until *session*'s transaction
    ends, waiting for the writes already made (share_company_modules). Taken by turning
    a module off, after the module-state boundary and before the company lock, waiting
    briefly (boundary.take_company_locks)."""
    await _company_module_lock(session, company_id, "pg_advisory_xact_lock")


async def share_company_modules(session, company_id) -> None:
    """Hold, until *session*'s transaction ends, the company's module settings as they
    are: a write of module work takes it before reading them, so no change to them is
    part way through while it writes, and none commits before it does. Holders never
    wait for each other. Before the company lock, as hold_company_modules is."""
    await _company_module_lock(session, company_id, "pg_advisory_xact_lock_shared")


def is_installed(module_name: str) -> bool:
    """Whether *module_name* is a module this installation runs or has on disk."""
    from celerp.modules.loader import is_running, module_search_path, read_manifest, resolve_module_path
    if is_running(module_name):
        return True
    path = resolve_module_path(module_name, module_search_path())
    return path is not None and read_manifest(path).get("name") == module_name


def _configured_load_set() -> list[str]:
    from celerp.config import installation_modules
    return installation_modules()


def dependency_closure(names) -> list[str]:
    """*names* and every module they depend on, in install order."""
    from celerp.config import _install_closure
    return _install_closure(sorted(names))


def uses_own_list(company_settings: dict[str, Any] | None) -> bool:
    """Whether the company has chosen its own set (otherwise it uses the installation's list)."""
    return bool(company_settings) and _SETTINGS_KEY in company_settings


def company_modules(company_settings: dict[str, Any] | None) -> set[str]:
    """The company's set, starting from the installation's list on a first choice."""
    if uses_own_list(company_settings):
        return get_enabled(company_settings)
    return set(_configured_load_set())


def enable_for_company(company_settings: dict[str, Any] | None, module_name: str) -> tuple[dict[str, Any], list[str]]:
    """Settings with *module_name* and its dependencies on, and the dependencies
    this turned on as well (so the caller can say so)."""
    current = company_modules(company_settings)
    closure = set(dependency_closure([module_name])) | {module_name}
    added = sorted(closure - current - {module_name})
    return set_enabled(dict(company_settings or {}), current | closure), added


def dependents(company_settings: dict[str, Any] | None) -> dict[str, list[str]]:
    """For each module, the modules the company uses that need it, so it cannot be turned
    off before them. A module built into Celerp is always on and refuses its own work
    while a module it needs is off, so it never holds one on."""
    from celerp.modules.loader import is_core_folded
    out: dict[str, list[str]] = {}
    for user in sorted(company_modules(company_settings)):
        if is_core_folded(user):
            continue
        for needed in dependency_closure([user]):
            if needed != user:
                out.setdefault(needed, []).append(user)
    return out


def disable_for_company(company_settings: dict[str, Any] | None, module_name: str) -> dict[str, Any]:
    """Settings with *module_name* off. Refused while another module the company
    uses depends on it (``dependents``)."""
    current = company_modules(company_settings) - {module_name}
    needed_by = dependents(company_settings).get(module_name, [])
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


async def commit_with_load_set(session) -> None:
    """Commit the caller's change to a company's set, then rewrite the load set's mirror.

    The caller holds the module-state boundary (hold_module_state) from before its
    checks, so the change commits inside it. The mirror is written only after the
    change committed, so a commit that fails never leaves config.toml disagreeing
    with the database. It is recomputed in its own transaction under the boundary,
    from committed state alone: whichever writer recomputes last sees every
    committed change. A module no company uses any more is already refused to every
    company and leaves the process at the next restart, so turning one off never
    needs a restart."""
    import asyncio
    from celerp.config import replace_enabled_modules

    await session.commit()
    await hold_module_state(session)
    names = await load_set(session)
    await asyncio.to_thread(replace_enabled_modules, names)
    await session.commit()
