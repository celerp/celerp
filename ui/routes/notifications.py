# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Notification proxy routes.

The UI server (port 8080) proxies notification endpoints to the API server
(port 5001) so the shell.py notification JS can call /notifications/* on the
same origin without CORS issues.
"""

from __future__ import annotations

import httpx
from starlette.requests import Request
from starlette.responses import Response

import ui.api_client as api
from ui.config import get_token as _token


def setup_routes(app):

    @app.get("/notifications")
    async def proxy_notifications(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response('{"items":[],"unread_count":0}', media_type="application/json", status_code=200)
        params = dict(request.query_params)
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.get("/notifications", params=params)
                return Response(content=r.content, media_type="application/json", status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response('{"items":[],"unread_count":0}', media_type="application/json", status_code=200)

    @app.post("/notifications/read-all")
    async def proxy_notifications_read_all(request: Request) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.post("/notifications/read-all")
                return Response(content=r.content, status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(status_code=503)

    @app.post("/notifications/{notification_id}/read")
    async def proxy_notification_read(request: Request, notification_id: str) -> Response:
        token = _token(request)
        if not token:
            return Response(status_code=401)
        async with api._local_client(token, timeout=10.0, follow_redirects=False) as c:
            try:
                r = await c.post(f"/notifications/{notification_id}/read")
                return Response(content=r.content, status_code=r.status_code)
            except (httpx.ConnectError, httpx.TimeoutException):
                return Response(status_code=503)
