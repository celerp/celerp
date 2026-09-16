# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Muxed SSE router - combines notifications + session-watch into one stream."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from celerp.services.auth import oauth2_scheme, validate_access_token
from celerp.notifications.sse import subscribe, unsubscribe

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_TICK = object()  # sentinel: asyncio.TimeoutError path

# Seconds the stream waits for a queued event before running the periodic
# session-watch poll (nonce eviction, drain, keepalive). A module constant so the
# poll cadence has one source of truth and tests can drive it deterministically.
_STREAM_TICK_SECONDS = 10.0


@router.get("/events/stream")
async def events_stream(token: str = Depends(oauth2_scheme)):
    """Muxed SSE: delivers notification + session-watch events on one connection.

    Auth: the token is FULLY validated once, up front, through the same
    ``validate_access_token`` the request path uses - old version, refresh
    token, missing nonce, stale nonce, inactive membership and inactive user
    are all rejected with 401 before any subscription is opened. That validation
    runs in a short-lived DB session that is closed before streaming begins, so
    the stream never holds a DB connection for its lifetime; the live stream
    keeps polling the nonce cache-first for revocation and drain.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.services.session_tracker import (
        get_nonce as _get_nonce,
        pop_evicted_by_ip as _pop_ip,
        get_nonce_from_cache as _get_nonce_from_cache,
    )
    from celerp.services.runtime_state import is_draining as _is_draining

    async with AsyncSessionLocal() as s:
        ctx = await validate_access_token(s, token)
    user_id = ctx.user.id
    company_id = ctx.company_id
    user_id_str = str(user_id)
    token_nonce = ctx.snonce

    async def _stream():
        q = subscribe(company_id, user_id)
        keepalive_tick = 0
        loop = asyncio.get_event_loop()
        # Absolute deadline for the next session-watch poll. The poll runs on this
        # deadline, not "10 seconds since the last notification", so a steady stream
        # of notifications cannot starve eviction/drain detection.
        next_tick = loop.time() + _STREAM_TICK_SECONDS
        try:
            while True:
                timeout = next_tick - loop.time()
                if timeout > 0:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=timeout)
                    except asyncio.TimeoutError:
                        event = _TICK
                    except asyncio.CancelledError:
                        return
                else:
                    # Deadline already reached; run the poll without waiting on the
                    # queue (which never blocks while notifications are flooding in).
                    event = _TICK

                if event is None:
                    # Eviction/shutdown sentinel from sse.subscribe - close cleanly.
                    return

                if event is not _TICK:
                    # Notification payload
                    yield f"event: notification\ndata: {json.dumps(event)}\n\n"
                    if loop.time() < next_tick:
                        continue
                    # Deadline reached while draining the queue - fall through to the
                    # poll before waiting on the next notification.

                # Poll path: re-arm the deadline, then run the session watch. Every
                # tick checks both eviction (nonce rotated elsewhere) and drain, so a
                # deploy can move a stream whose nonce still matches - a matching
                # nonce must never suppress the drain signal. Re-arming here covers
                # the eviction/keepalive continues and the bottom fall-through,
                # avoiding a busy poll loop.
                next_tick = loop.time() + _STREAM_TICK_SECONDS

                # Nonce poll, cache-first with a DB read on a miss: a rotated nonce
                # means the session was revoked elsewhere and the stream is evicted.
                # The token always carries a nonce here (validate_access_token
                # rejects a missing-nonce token before the stream opens).
                cached_nonce = _get_nonce_from_cache(user_id_str)
                if cached_nonce is not None:
                    current_nonce = cached_nonce
                else:
                    async with AsyncSessionLocal() as s:
                        current_nonce = await _get_nonce(s, user_id_str)
                if current_nonce != token_nonce:
                    async with AsyncSessionLocal() as s:
                        evicted_ip = await _pop_ip(s, user_id_str) or ""
                    yield f"event: evicted\ndata: {json.dumps({'by': evicted_ip})}\n\n"
                    return

                async with AsyncSessionLocal() as s:
                    if await _is_draining(s):
                        yield "event: drain\ndata: {}\n\n"
                        return

                keepalive_tick += 1
                if keepalive_tick % 3 == 0:
                    yield ": keepalive\n\n"

        except asyncio.CancelledError:
            return
        finally:
            unsubscribe(company_id, user_id, q)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
