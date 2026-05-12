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

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.auth import SystemRuntimeState


_DEFAULT_VALUE: dict[str, Any] = {"draining": False}

_SINGLETON_ID = 1


async def _get_or_create(session: AsyncSession) -> SystemRuntimeState:
    row = await session.get(SystemRuntimeState, _SINGLETON_ID)
    if row is None:
        row = SystemRuntimeState(id=_SINGLETON_ID, value=dict(_DEFAULT_VALUE))
        session.add(row)
        await session.flush()
    return row


async def get_runtime_state(session: AsyncSession) -> dict[str, Any]:
    """Return a copy of the current runtime state dict."""
    row = await _get_or_create(session)
    return dict(row.value) if row.value else dict(_DEFAULT_VALUE)


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
