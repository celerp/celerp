# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Debug / diagnostics endpoints.

These routes are intentionally ONLY registered when CELERP_DEBUG=1 is set.
Do NOT register them in production by default - they expose internal state.

Endpoints:
  GET /debug/pool          Live DB pool stats + pg_stat_activity
  GET /debug/sse           Active SSE subscriber counts
  GET /debug/requests      In-flight request counter (via middleware)
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import engine, get_session
from celerp.services.auth import require_admin

router = APIRouter(prefix="/debug", tags=["debug"])


# ---------------------------------------------------------------------------
# In-flight request tracking (populated by DebugMiddleware when CELERP_DEBUG=1)
# ---------------------------------------------------------------------------

_in_flight: dict[str, dict] = {}  # request_id -> {method, path, started_at}
_in_flight_lock = asyncio.Lock()

# Checkout event log: list of (ts, event, path) tuples, capped at 200
_pool_events: list[tuple[float, str, str]] = []
_MAX_POOL_EVENTS = 200


def _record_pool_event(event: str, path: str = "") -> None:
    """Called by pool event listeners (checkout / checkin / overflow)."""
    _pool_events.append((time.monotonic(), event, path))
    if len(_pool_events) > _MAX_POOL_EVENTS:
        del _pool_events[:-_MAX_POOL_EVENTS]


def install_pool_listeners() -> None:
    """Attach SQLAlchemy pool event listeners so every checkout/checkin is logged."""
    from sqlalchemy import event as sa_event

    sync_pool = engine.sync_engine.pool

    @sa_event.listens_for(sync_pool, "checkout")
    def on_checkout(dbapi_conn, conn_record, conn_proxy):
        _record_pool_event("checkout")

    @sa_event.listens_for(sync_pool, "checkin")
    def on_checkin(dbapi_conn, conn_record):
        _record_pool_event("checkin")

    @sa_event.listens_for(sync_pool, "overflow")
    def on_overflow(dbapi_conn, conn_record):
        _record_pool_event("overflow")

    @sa_event.listens_for(sync_pool, "invalidate")
    def on_invalidate(dbapi_conn, conn_record, exception):
        _record_pool_event("invalidate")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/pool", dependencies=[Depends(require_admin)])
async def pool_stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Live DB pool stats and active Postgres connections.

    Shows:
    - pool_size, checked_out, checked_in, overflow
    - pg_stat_activity for this database (query, state, duration)
    - recent pool events (checkout/checkin/overflow log)
    """
    from sqlalchemy import text

    sync_pool = engine.sync_engine.pool
    pool_info: dict[str, Any] = {
        "pool_size": sync_pool.size(),
        "checked_in": sync_pool.checkedin(),
        "checked_out": sync_pool.checkedout(),
        "overflow": sync_pool.overflow(),
        "invalid": sync_pool.invalidated() if hasattr(sync_pool, "invalidated") else "n/a",
    }

    # pg_stat_activity - shows what Postgres actually sees
    pg_activity: list[dict] = []
    try:
        rows = await session.execute(
            text("""
                SELECT pid, state, wait_event_type, wait_event,
                       EXTRACT(epoch FROM (now() - query_start))::int AS duration_s,
                       LEFT(query, 120) AS query_snippet
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                ORDER BY query_start
            """)
        )
        pg_activity = [
            {
                "pid": r.pid,
                "state": r.state,
                "wait_event": f"{r.wait_event_type}/{r.wait_event}" if r.wait_event else None,
                "duration_s": r.duration_s,
                "query": r.query_snippet,
            }
            for r in rows
        ]
    except Exception as exc:
        pg_activity = [{"error": str(exc)}]

    # Summarise pool event log
    now = time.monotonic()
    event_summary: dict[str, int] = defaultdict(int)
    recent_events = []
    for ts, evt, path in _pool_events[-50:]:
        event_summary[evt] += 1
        recent_events.append({"age_s": round(now - ts, 2), "event": evt, "path": path})

    return {
        "pool": pool_info,
        "pg_stat_activity": pg_activity,
        "pg_connection_count": len(pg_activity),
        "pool_event_summary": dict(event_summary),
        "recent_pool_events": recent_events,
    }


@router.get("/sse", dependencies=[Depends(require_admin)])
async def sse_stats() -> dict[str, Any]:
    """Active SSE subscriber state.

    Shows how many notification stream subscribers are registered per user key.
    If this grows over time without shrinking, there's a subscriber leak.
    """
    from celerp.notifications.sse import _subscribers

    per_key = {k: len(v) for k, v in _subscribers.items() if v}
    return {
        "total_subscriber_keys": len(per_key),
        "total_queues": sum(per_key.values()),
        "per_key": per_key,
    }


@router.get("/caches", dependencies=[Depends(require_admin)])
async def cache_stats() -> dict[str, Any]:
    """In-process cache state (nonce cache, drain cache).

    If _nonce_cache grows unboundedly, we have a user-id accumulation bug.
    """
    import celerp.services.session_tracker as _st
    import celerp.services.runtime_state as _rs

    now = time.monotonic()
    nonce_entries = [
        {"user_id": uid, "age_s": round(now - ts, 2)}
        for uid, (_val, ts) in _st._nonce_cache.items()
    ]

    drain_entry = None
    cached = _rs._drain_cache_get()
    if cached is not None:
        drain_entry = {
            "value": cached,
            "cache_age_s": round(now - _rs._drain_cache_at, 2) if _rs._drain_cache is not None else None,
        }

    return {
        "nonce_cache_count": len(_st._nonce_cache),
        "nonce_entries": nonce_entries,
        "drain_cache": drain_entry,
    }
