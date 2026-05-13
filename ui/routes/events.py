# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Muxed events proxy route.

Proxies /events/stream from the API server, combining notification + session-watch
into a single SSE connection per browser tab.
"""

from __future__ import annotations

import asyncio
import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from ui.config import API_BASE, get_token as _token

_RETRY_DELAYS = [1, 2, 4, 8, 16]


def setup_routes(app):

    @app.get("/events/stream")
    async def proxy_events_stream(request: Request) -> StreamingResponse:
        token = _token(request)

        async def _stream():
            # Commit the 200 immediately so any downstream exception won't become a 500.
            yield ": keepalive\n\n"

            if not token:
                return

            for delay in _RETRY_DELAYS:
                if await request.is_disconnected():
                    return
                try:
                    async with httpx.AsyncClient(
                        base_url=API_BASE,
                        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
                        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                    ) as c:
                        async with c.stream(
                            "GET",
                            "/events/stream",
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "text/event-stream",
                            },
                        ) as resp:
                            async for chunk in resp.aiter_bytes():
                                if await request.is_disconnected():
                                    return
                                yield chunk
                    return  # clean stream close - stop retrying
                except (asyncio.CancelledError, GeneratorExit):
                    return
                except Exception:
                    await asyncio.sleep(delay)

        return StreamingResponse(_stream(), media_type="text/event-stream")
