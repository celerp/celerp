# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a company needs from the installed modules, and getting it ready.

A migration or a company backup depends on modules: the bundled ones that receive its
records, and the modules whose data a backup carries. ``plan_requirements`` says, for
each, whether it is running at a version that will do, or what stands in the way:
turning it on, a restart, an update, or a module this installation does not have.
``prepare`` turns on what may be turned on: a bundled module always, another only when
the owner named it. Turning a module on takes a restart, which the caller schedules with
``schedule_restart`` after it has told the owner.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from celerp.config import read_config, replace_enabled_modules
from celerp.modules import loader

log = logging.getLogger(__name__)


class Status(str, enum.Enum):
    READY = "ready"
    ENABLE_REQUIRED = "enable_required"
    RESTART_REQUIRED = "restart_required"
    UPGRADE_RESTART_REQUIRED = "upgrade_restart_required"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"


_BLOCKING = frozenset({Status.MISSING, Status.INCOMPATIBLE})


class RequirementsBlocked(Exception):
    """A needed module is missing or cannot run here; nothing was turned on."""


class ConsentRequired(Exception):
    """A needed module from outside Celerp is off and the owner has not agreed to turn it on."""


@dataclass(frozen=True)
class Requirement:
    name: str
    label: str
    status: Status
    first_party: bool


@dataclass(frozen=True)
class RequirementPlan:
    requirements: tuple[Requirement, ...]

    @property
    def ready(self) -> bool:
        return all(r.status is Status.READY for r in self.requirements)

    @property
    def blocked(self) -> list[Requirement]:
        return [r for r in self.requirements if r.status in _BLOCKING]

    @property
    def preparable(self) -> list[Requirement]:
        return [r for r in self.requirements if r.status is Status.ENABLE_REQUIRED and r.first_party]

    @property
    def needs_consent(self) -> list[Requirement]:
        return [r for r in self.requirements if r.status is Status.ENABLE_REQUIRED and not r.first_party]

    def public(self) -> list[dict]:
        return [{"name": r.name, "label": r.label, "status": r.status.value, "first_party": r.first_party}
                for r in self.requirements]


def _at_least(version, minimum: str | None) -> bool:
    if minimum is None:
        return True
    try:
        return Version(str(version)) >= Version(minimum)
    except InvalidVersion:
        return False


def _status(name: str, minimum: str | None, enabled: set[str]) -> tuple[Status, bool]:
    if loader.is_core_folded(name):
        return Status.READY, True
    path = loader.resolve_runtime_module_path(name, loader.module_search_path())
    if path is None:
        # Removed from disk: even while its code still runs, it is gone after a restart.
        return Status.MISSING, name in loader.first_party_names()
    first_party = loader.is_first_party(path)
    if loader.is_running(name):
        if _at_least(loader.running_version(name), minimum):
            return Status.READY, first_party
        return (Status.UPGRADE_RESTART_REQUIRED if _at_least(loader.read_manifest(path).get("version"), minimum)
                else Status.INCOMPATIBLE), first_party
    if name in loader.load_errors() or not _at_least(loader.read_manifest(path).get("version"), minimum):
        return Status.INCOMPATIBLE, first_party
    return (Status.RESTART_REQUIRED if name in enabled else Status.ENABLE_REQUIRED), first_party


def plan_requirements(needed: dict[str, str | None]) -> RequirementPlan:
    """Each needed module (name to the lowest version that will do, or None) with its status.
    Reads the installation; changes nothing."""
    enabled = set(read_config().get("modules", {}).get("enabled", []))
    out = []
    for name in sorted(needed):
        status, first_party = _status(name, needed[name], enabled)
        out.append(Requirement(name, loader.module_label(name), status, first_party))
    return RequirementPlan(tuple(out))


def prepare(plan: RequirementPlan, *, consent: frozenset[str] = frozenset()) -> bool:
    """Turn on, in the configuration, every needed module that is off: bundled ones, and
    others the owner named in ``consent``. Refused with nothing changed when a module is
    missing or cannot run here, or one from outside Celerp lacks consent. Additive and
    idempotent. Returns whether a restart is needed before the plan is ready."""
    if plan.blocked:
        raise RequirementsBlocked(", ".join(r.label for r in plan.blocked))
    unconsented = [r for r in plan.needs_consent if r.name not in consent]
    if unconsented:
        raise ConsentRequired(", ".join(r.label for r in unconsented))
    names = [r.name for r in plan.requirements if r.status is Status.ENABLE_REQUIRED]
    if names:
        # The company that needs them is created after the restart, and its own module
        # set names them from then on; until then they join the installation's list.
        replace_enabled_modules([*read_config().get("modules", {}).get("enabled", []), *names])
    return not plan.ready


def schedule_restart() -> bool:
    """Restart Celerp half a second from now, after the current response has gone out, so a
    changed module set loads. Returns whether the restart was scheduled."""
    from celerp.routers.system import _restart_sentinel_path, _send_sigterm
    try:
        sentinel = _restart_sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        asyncio.get_running_loop().call_later(0.5, _send_sigterm)
    except Exception as exc:
        log.warning("Failed to schedule restart: %s", exc)
        return False
    log.info("Restart scheduled to load a changed module set")
    return True
