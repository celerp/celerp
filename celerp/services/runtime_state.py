# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Cluster-wide runtime state helpers.

``SystemRuntimeState`` is a single DB row (id=1) storing a JSON dict of
runtime flags shared across all workers.  The canonical flags are:

    draining: bool   - True while the cluster is in drain mode (no new writes)
    drain_since: str - ISO timestamp when drain started (informational)

All helpers are async and accept an ``AsyncSession``.  Callers must be in an
async context (FastAPI handlers, lifespan tasks, etc.).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

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


def _drain_cache_bust() -> None:
    global _drain_cache
    _drain_cache = None


async def _get_or_create(session: AsyncSession) -> SystemRuntimeState:
    row = await session.get(SystemRuntimeState, _SINGLETON_ID)
    if row is None:
        row = SystemRuntimeState(id=_SINGLETON_ID, value=dict(_DEFAULT_VALUE))
        session.add(row)
        await session.flush()
    return row


async def get_runtime_state(session: AsyncSession) -> dict[str, Any]:
    """Return a copy of the current runtime state dict.

    Result is cached for up to ``_DRAIN_TTL_S`` seconds.  The cache is
    explicitly busted by ``set_draining`` so transitions are immediate.
    """
    cached = _drain_cache_get()
    if cached is not None:
        return cached
    row = await _get_or_create(session)
    value = dict(row.value) if row.value else dict(_DEFAULT_VALUE)
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
    row = await _get_or_create(session)
    now_iso = datetime.now(timezone.utc).isoformat()
    current = dict(row.value) if row.value else {}
    if draining:
        row.value = {**current, "draining": True, "drain_since": now_iso}
    else:
        row.value = {**current, "draining": False, "drain_since": None}
    await session.commit()
    _drain_cache_bust()  # bust so next is_draining call reads fresh state
