# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Develop→release upgrade guard, run from the API lifespan.

After a version change, the release build may compute projections differently
from the develop build that wrote them, so projections are rebuilt from the
ledger using the release's handlers (`ProjectionEngine.rebuild`). This runs in
the lifespan — not the CLI — because rebuild needs every projection handler
(kernel `sys.*` + each loaded module) registered, which only happens here.

Gated by the `projection_version` marker so it runs once per version. Before
rebuilding it pre-checks that every `event_type` in the ledger is a known event
(with modules loaded); an unknown type means a downgrade or a missing module, so
we skip the rebuild rather than silently fold those events into wrong state.

The data-backfill half of the reconcile runs earlier, at `celerp migrate` time
(`cli._reconcile_after_migrate`), where no handlers are needed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def unknown_event_types(session: "AsyncSession") -> set[str]:
    """Ledger event types that are not in the (modules-loaded) event catalog."""
    from sqlalchemy import text

    from celerp.events.schemas import EVENT_SCHEMA_MAP

    rows = await session.execute(text("SELECT DISTINCT event_type FROM ledger"))
    return {r[0] for r in rows} - set(EVENT_SCHEMA_MAP.keys())


def _is_dev_version(v: str) -> bool:
    """A develop build's version string. Matches the UI's dev-build detection
    (ui/components/shell.py): a PEP 440 `.devN` segment, or the `+dev` local
    fallback (`0.0.0+dev`) used when setuptools_scm can't read the version."""
    return ".dev" in v or "+dev" in v


def _should_rebuild(marker: str | None) -> bool:
    """Whether to rebuild projections, given the version that last booted this DB.

    Rebuild when the database was last written by a DEVELOP build (its
    projections may use experimental logic the release computes differently), or
    when the origin is unknown (no marker — the first boot under this guard, so
    rebuild once defensively). A database last booted by a RELEASE build is
    assumed projection-correct, so a release→release upgrade skips the
    (potentially expensive) full rebuild entirely.
    """
    if marker is None:
        return True
    return _is_dev_version(marker)


async def run_upgrade_guard(session: "AsyncSession") -> dict:
    """Rebuild projections on a develop→release transition. Caller owns the txn.

    Returns a summary dict; never raises for an expected condition (unknown
    events → skip). `changed` is False when the marker already matches.
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
    if marker == __version__:
        return {"changed": False, "rebuilt": False}

    rebuilt = False
    if _should_rebuild(marker):
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
            return {"changed": True, "rebuilt": False, "unknown_event_types": sorted(unknown)}
        await ProjectionEngine.rebuild(session)
        rebuilt = True
        log.info("Upgrade guard: rebuilt projections (from %s) for version %s", marker, __version__)

    # Record this boot's version so a later release→release upgrade can skip.
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_VERSION_KEY, __version__))
    return {"changed": True, "rebuilt": rebuilt}
