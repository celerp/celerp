# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Notification proxy routes.

The UI server (port 8080) proxies notification endpoints to the API server
(port 5001) so the shell.py notification JS can call /notifications/* on the
same origin without CORS issues.
"""

from __future__ import annotations

import asyncio
import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ui.config import API_BASE, get_token as _token


def setup_routes(app):

    @app.get("/notifications")
    async def proxy_notifications(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response('{"items":[],"unread_count":0}', media_type="application/json", status_code=200)
        params = dict(request.query_params)
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.get(
                    "/notifications",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                return Response(content=r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response('{"items":[],"unread_count":0}', media_type="application/json", status_code=200)

    @app.post("/notifications/read-all")
    async def proxy_notifications_read_all(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.post(
                    "/notifications/read-all",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return Response(content=r.content, status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(status_code=503)

    @app.post("/notifications/{notification_id}/read")
    async def proxy_notification_read(request: Request, notification_id: str) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
            try:
                r = await c.post(
                    f"/notifications/{notification_id}/read",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return Response(content=r.content, status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(status_code=503)

    @app.get("/notifications/stream")
    async def proxy_notifications_stream(request: Request) -> Response:
        token = _token(request)
        if not token:
            # Return an empty SSE stream so EventSource doesn't loop on 401
            async def _empty():
                yield "data: {}\n\n"
            return StreamingResponse(_empty(), media_type="text/event-stream")

        async def _stream():
            try:
                async with httpx.AsyncClient(base_url=API_BASE, timeout=None) as c:
                    async with c.stream(
                        "GET",
                        "/notifications/stream",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "text/event-stream",
                        },
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            if await request.is_disconnected():
                                return
                            yield chunk
            except asyncio.CancelledError:
                # Server shutdown or client disconnect — exit cleanly
                return
            except (httpx.ConnectError, httpx.TimeoutException):
                yield "data: {}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")
