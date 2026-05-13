# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Muxed SSE router - combines notifications + session-watch into one stream."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from celerp.services.auth import get_token_claims, oauth2_scheme
from celerp.notifications.sse import subscribe, unsubscribe

log = logging.getLogger(__name__)

router = APIRouter()

_TICK = object()  # sentinel: asyncio.TimeoutError path


@router.get("/events/stream")
async def events_stream(token: str = Depends(oauth2_scheme)):
    """Muxed SSE: delivers notification + session-watch events on one connection.

    Auth: manual token decode (no Depends(get_session)) so FastAPI does NOT
    hold a DB connection open for the stream lifetime.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.services.session_tracker import (
        get_nonce as _get_nonce,
        pop_evicted_by_ip as _pop_ip,
        get_nonce_from_cache as _get_nonce_from_cache,
    )
    from celerp.services.runtime_state import is_draining as _is_draining

    claims = get_token_claims(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Session expired")

    token_nonce = claims.get("snonce", "")
    user_id_str = claims.get("sub", "")
    company_id_str = claims.get("company_id", "")

    try:
        user_id = uuid.UUID(user_id_str)
        company_id = uuid.UUID(company_id_str)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="Invalid token claims")

    async def _stream():
        q = subscribe(company_id, user_id)
        keepalive_tick = 0
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    event = _TICK
                except asyncio.CancelledError:
                    return

                if event is None:
                    # Eviction/shutdown sentinel from sse.subscribe - close cleanly.
                    return

                if event is not _TICK:
                    # Notification payload
                    yield f"event: notification\ndata: {json.dumps(event)}\n\n"
                    continue

                # Timeout path: poll nonce (same cache-first logic as auth.py session_watch)
                cached_nonce = _get_nonce_from_cache(user_id_str)
                if cached_nonce is not None:
                    if cached_nonce != token_nonce:
                        async with AsyncSessionLocal() as s:
                            ip = await _pop_ip(s, user_id_str) or ""
                        yield f"event: evicted\ndata: {json.dumps({'by': ip})}\n\n"
                        return
                    keepalive_tick += 1
                    if keepalive_tick % 3 == 0:
                        yield ": keepalive\n\n"
                    continue

                # Cache miss: hit Postgres
                async with AsyncSessionLocal() as s:
                    current_nonce = await _get_nonce(s, user_id_str)
                    if current_nonce != token_nonce:
                        ip = await _pop_ip(s, user_id_str) or ""
                        yield f"event: evicted\ndata: {json.dumps({'by': ip})}\n\n"
                        return
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
