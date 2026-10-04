# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Upgrade guard, run from the API lifespan.

Projections are rebuilt from the ledger with this build's handlers
(`ProjectionEngine.rebuild`) when the stored ones may have been computed
differently: the database was last started by a develop build, or by any build
whose projection semantics (`PROJECTION_SEMANTICS`) differ from this one's. This
runs in the lifespan, not the CLI, because rebuild needs every projection handler
(kernel `sys.*` + each loaded module) registered, which only happens here, and
it runs before the modules' start hooks, which read the projections.

Gated by the `projection_version` and `projection_semantics` markers so it runs
once per change. Before
rebuilding it pre-checks that this build can replay every `event_type` in the
ledger (`ProjectionEngine.replayable`); one it cannot means a downgrade or a
missing module, so we skip the rebuild rather than silently fold those events
into wrong state. The result says whether the projections are current, and the
caller runs nothing that reads them when they are not.

The data-backfill half of the reconcile runs earlier, at `celerp migrate` time
(`cli._reconcile_after_migrate`), where no handlers are needed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Bump whenever a kernel or bundled-module projection handler computes different
# state from the same ledger events, so every installation that upgrades past the
# change rebuilds its projections once. Independent of the app version: a release
# that changes no handler leaves it alone and its upgrade skips the rebuild.
PROJECTION_SEMANTICS = 1
PROJECTION_SEMANTICS_KEY = "projection_semantics"


async def unknown_event_types(session: "AsyncSession") -> set[str]:
    """Ledger event types this build cannot replay."""
    from sqlalchemy import text

    from celerp.projections.engine import ProjectionEngine

    rows = await session.execute(text("SELECT DISTINCT event_type FROM ledger"))
    return {r[0] for r in rows if not ProjectionEngine.replayable(r[0])}


def _is_dev_version(v: str) -> bool:
    """A develop build's version string. Matches the UI's dev-build detection
    (ui/components/shell.py): a PEP 440 `.devN` segment, or the `+dev` local
    fallback (`0.0.0+dev`) used when setuptools_scm can't read the version."""
    return ".dev" in v or "+dev" in v


def _should_rebuild(marker: str | None, semantics: str | None) -> bool:
    """Whether to rebuild projections, given the version and the projection
    semantics that last booted this DB.

    Rebuild when the database was last written by a DEVELOP build (its
    projections may use experimental logic the release computes differently),
    when the origin is unknown (no marker), or when its projections were computed
    under other semantics than this build's (none recorded means older than the
    semantics marker). A release→release upgrade with unchanged semantics skips
    the (potentially expensive) full rebuild entirely.
    """
    if marker is None or semantics != str(PROJECTION_SEMANTICS):
        return True
    return _is_dev_version(marker)


async def run_upgrade_guard(session: "AsyncSession") -> dict:
    """Rebuild projections when they may be stale (`_should_rebuild`). Caller owns the txn.

    Returns a summary dict; never raises for an expected condition (unknown
    events → skip). `changed` is False when both markers already match; `current`
    is False only when the projections may be stale and were not rebuilt.
    """
    from celerp import __version__
    from celerp.migrations._data_reconcile import (
        PROJECTION_VERSION_KEY,
        get_meta,
        set_meta,
    )
    from celerp.projections.engine import ProjectionEngine

    conn = await session.connection()
    marker = await conn.run_sync(lambda c: get_meta(c, PROJECTION_VERSION_KEY))
    semantics = await conn.run_sync(lambda c: get_meta(c, PROJECTION_SEMANTICS_KEY))
    if marker == __version__ and semantics == str(PROJECTION_SEMANTICS):
        return {"changed": False, "rebuilt": False, "current": True}

    rebuilt = False
    if _should_rebuild(marker, semantics):
        unknown = await unknown_event_types(session)
        if unknown:
            # Downgrade or missing module: replaying these events would produce
            # wrong state. Skip the rebuild and leave the marker as-is so a later
            # boot (with the module present, or the right release) retries.
            log.error(
                "Upgrade guard: %d unknown ledger event type(s) — skipping projection "
                "rebuild to avoid corruption. Missing handler/module for: %s",
                len(unknown), sorted(unknown),
            )
            return {"changed": True, "rebuilt": False, "current": False, "unknown_event_types": sorted(unknown)}
        await ProjectionEngine.rebuild(session)
        rebuilt = True
        log.info("Upgrade guard: rebuilt projections (from %s, semantics %s) for version %s, semantics %s",
                 marker, semantics, __version__, PROJECTION_SEMANTICS)

    # Record this boot's version and semantics so a later upgrade that changes
    # neither handlers nor build kind can skip.
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_VERSION_KEY, __version__))
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_SEMANTICS_KEY, str(PROJECTION_SEMANTICS)))
    return {"changed": True, "rebuilt": rebuilt, "current": True}
