# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Cluster-wide runtime state helpers.

``SystemRuntimeState`` is a single DB row (id=1) storing a JSON dict of
runtime flags shared across all workers.  The canonical flags are:

    draining: bool   - True while the cluster is in drain mode (no new writes)
    drain_since: str - ISO timestamp when drain started (informational)

All helpers are async and accept an ``AsyncSession``.  Callers must be in an
async context (FastAPI handlers, lifespan tasks, etc.).

Every writer reads the row through ``lock_runtime_state`` and replaces the dict
on the row it returns: the row is locked until the writer commits and holds the
current value, so two writers never write back each other's stale copy.  Reads
(``get_runtime_state``) never lock.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.auth import SystemRuntimeState


_DEFAULT_VALUE: dict[str, Any] = {"draining": False}

_SINGLETON_ID = 1


# ---------------------------------------------------------------------------
# In-process drain cache
# ---------------------------------------------------------------------------
# The drain flag almost never changes (only during deploys).  Caching it for
# 1 second eliminates a DB round-trip on every write request in DrainMiddleware
# without any meaningful security or correctness cost.  The cache is explicitly
# busted by set_draining() so transitions are immediate within the same process.
# ---------------------------------------------------------------------------

_DRAIN_TTL_S = 1  # seconds - short enough that cross-process propagation lag is negligible

_drain_cache: dict[str, Any] | None = None  # None = not cached
_drain_cache_at: float = 0.0


def _drain_cache_set(value: dict[str, Any]) -> None:
    global _drain_cache, _drain_cache_at
    _drain_cache = dict(value)
    _drain_cache_at = time.monotonic()


def _drain_cache_get() -> dict[str, Any] | None:
    if _drain_cache is None:
        return None
    if time.monotonic() - _drain_cache_at > _DRAIN_TTL_S:
        _drain_cache_bust()
        return None
    return _drain_cache


def drain_cache_status() -> dict[str, Any] | None:
    """The cached drain state and its age in seconds; None when nothing is cached."""
    cached = _drain_cache_get()
    if cached is None:
        return None
    return {"value": cached, "cache_age_s": round(time.monotonic() - _drain_cache_at, 2)}


def _drain_cache_bust() -> None:
    global _drain_cache
    _drain_cache = None


async def lock_runtime_state(session: AsyncSession, *, create: bool = True) -> SystemRuntimeState | None:
    """The runtime-state row, locked until the caller commits; created when missing, or
    None without *create* (a writer that only edits what is already recorded).

    The row is read FOR UPDATE with populate_existing, so a copy this session read
    earlier is refreshed to the value committed before the lock was granted.
    """
    if create:
        await session.execute(pg_insert(SystemRuntimeState).values(
            id=_SINGLETON_ID, value=dict(_DEFAULT_VALUE)).on_conflict_do_nothing())
    return (await session.execute(
        select(SystemRuntimeState).where(SystemRuntimeState.id == _SINGLETON_ID).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()


async def get_runtime_state(session: AsyncSession) -> dict[str, Any]:
    """Return a copy of the current runtime state dict.

    Result is cached for up to ``_DRAIN_TTL_S`` seconds.  The cache is
    explicitly busted by ``set_draining`` so transitions are immediate.
    """
    cached = _drain_cache_get()
    if cached is not None:
        return cached
    # A read never creates the row: the drain check runs it in a session it never
    # commits, where an insert would be discarded and, until then, hold the row's key.
    row = await session.get(SystemRuntimeState, _SINGLETON_ID)
    value = dict(row.value) if row is not None and row.value else dict(_DEFAULT_VALUE)
    _drain_cache_set(value)
    return value


async def is_draining(session: AsyncSession) -> bool:
    """Return True if the cluster is currently in drain mode."""
    state = await get_runtime_state(session)
    return bool(state.get("draining", False))


async def set_draining(session: AsyncSession, draining: bool) -> None:
    """Set or clear the draining flag.

    Always replaces the full dict to avoid JSON mutation tracking issues.
    """
    row = await lock_runtime_state(session)
    now_iso = datetime.now(timezone.utc).isoformat()
    current = dict(row.value) if row.value else {}
    if draining:
        row.value = {**current, "draining": True, "drain_since": now_iso}
    else:
        row.value = {**current, "draining": False, "drain_since": None}
    await session.commit()
    _drain_cache_bust()  # bust so next is_draining call reads fresh state


# ---------------------------------------------------------------------------
# Star-CTA dismissal (install-level: one ask for the whole install)
# ---------------------------------------------------------------------------

async def star_prompt_dismissed(session: AsyncSession) -> bool:
    """Return True if the GitHub-star ask has been dismissed for this install."""
    state = await get_runtime_state(session)
    return bool(state.get("star_prompt_dismissed", False))


async def dismiss_star_prompt(session: AsyncSession) -> None:
    """Mark the GitHub-star ask dismissed so the dashboard card stops."""
    row = await lock_runtime_state(session)
    current = dict(row.value) if row.value else {}
    row.value = {**current, "star_prompt_dismissed": True}
    await session.commit()
    _drain_cache_bust()  # this row's cache is shared with the drain flag
