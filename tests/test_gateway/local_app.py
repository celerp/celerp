# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A stand-in for the httpx.AsyncClient the gateway opens per proxied request.

The gateway sends each request with build_request + send(stream=True); a test's
subclass answers from request(method, url, headers, content) with any object that
has status_code, content and headers, and gets back a real streamable response.
"""

from __future__ import annotations

import httpx


class LocalClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def build_request(self, method, url, headers=None, content=None):
        return httpx.Request(method, url, headers=headers, content=content)

    async def send(self, request, stream=False):
        r = await self.request(request.method, str(request.url),
                               headers=dict(request.headers), content=request.content or None)
        return httpx.Response(r.status_code, headers=r.headers, content=r.content, request=request)

    async def request(self, method, url, headers=None, content=None):
        raise NotImplementedError
