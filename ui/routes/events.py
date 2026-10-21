# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Muxed events proxy route.

Proxies /events/stream from the API server, combining notification + session-watch
into a single SSE connection per browser tab.
"""

from __future__ import annotations

import asyncio

import anyio
import httpx
from starlette.requests import Request

from ui.config import get_token as _token
from ui.streaming import ClosingStreamingResponse

_RETRY_DELAYS = [1, 2, 4, 8, 16]
# The longest the stream waits for the API to answer before trying again.
_OPEN_SECONDS = 10


def setup_routes(app):

    @app.get("/events/stream")
    async def proxy_events_stream(request: Request) -> ClosingStreamingResponse:
        token = _token(request)

        async def _stream():
            from ui.config import API_BASE
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
                        upstream = c.build_request("GET", "/events/stream", headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "text/event-stream",
                        })
                        # Leaving the page cancels this stream. A connect cancelled just as
                        # its socket opens drops that socket unclosed (anyio), so the stream
                        # finishes opening first, within a bounded time.
                        with anyio.CancelScope(shield=True), anyio.fail_after(_OPEN_SECONDS):
                            resp = await c.send(upstream, stream=True)
                        try:
                            async for chunk in resp.aiter_bytes():
                                if await request.is_disconnected():
                                    return
                                yield chunk
                        finally:
                            await resp.aclose()
                    return  # clean stream close - stop retrying
                except (asyncio.CancelledError, GeneratorExit):
                    return
                except Exception:
                    await asyncio.sleep(delay)

        return ClosingStreamingResponse(_stream(), media_type="text/event-stream")
