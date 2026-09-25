# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Update proxy routes.

The update card in the notifications panel calls /system/update* on the UI
origin; these forward to the API with the signed-in user's token, the same way
the notification routes do. The API decides who may do what.
"""

from __future__ import annotations

import httpx
from starlette.requests import Request
from starlette.responses import Response

import ui.api_client as api
from ui.config import get_token as _token

# Checking and requesting an update ask the package index, which can take up
# to two minutes; the rest answer at once.
_CHECK_TIMEOUT = 150.0
_TIMEOUT = 10.0


async def _forward(request: Request, method: str, path: str, timeout: float = _TIMEOUT) -> Response:
    token = _token(request)
    if not token:
        return Response(status_code=401)
    body = await request.body()
    async with api._local_client(token, timeout=timeout, follow_redirects=False) as c:
        try:
            r = await c.request(method, path, content=body or None,
                                headers={"content-type": "application/json"} if body else None)
        except (httpx.ConnectError, httpx.TimeoutException):
            # The API is down while an update installs; the card keeps waiting.
            return Response(status_code=503)
    return Response(content=r.content, media_type="application/json", status_code=r.status_code)


def setup_routes(app):

    @app.get("/system/update")
    async def proxy_update_status(request: Request) -> Response:
        return await _forward(request, "GET", "/system/update")

    @app.post("/system/update")
    async def proxy_update_install(request: Request) -> Response:
        return await _forward(request, "POST", "/system/update", _CHECK_TIMEOUT)

    @app.post("/system/update/check")
    async def proxy_update_check(request: Request) -> Response:
        return await _forward(request, "POST", "/system/update/check", _CHECK_TIMEOUT)

    @app.patch("/system/update/settings")
    async def proxy_update_settings(request: Request) -> Response:
        return await _forward(request, "PATCH", "/system/update/settings")
