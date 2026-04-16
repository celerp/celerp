# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'",
        )
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        return response


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the limit.

    Only checks the Content-Length header - does not buffer the body, which
    avoids conflicts with streaming responses (CSV exports, SSE, etc.).
    Clients that omit Content-Length on large uploads are not covered here;
    that is acceptable for the current use case (JSON API).
    """

    def __init__(self, app, max_body_size_bytes: int):
        super().__init__(app)
        self.max_body_size_bytes = int(max_body_size_bytes)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_size_bytes:
                    return JSONResponse(status_code=413, content={"detail": "Request too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        return await call_next(request)


class SlidingTokenRefreshMiddleware(BaseHTTPMiddleware):
    """Sliding-window JWT refresh for Bearer token (API) clients.

    On every successful (2xx) authenticated response, if the Bearer token is
    past half its lifetime, issue a fresh access token and include it in the
    ``X-Refreshed-Token`` response header. The client should replace its stored
    token with this value to maintain a sliding session.

    No-op when:
    - No Authorization: Bearer header is present
    - Token decode fails (invalid/expired — the route handler already rejected it)
    - Response status >= 300 (redirects, errors)
    - Token has not yet consumed half its TTL
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Only inject on successful responses
        if response.status_code >= 300:
            return response

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return response

        token = auth[len("Bearer "):]
        refreshed = _maybe_refresh_bearer(token)
        if refreshed:
            response.headers["X-Refreshed-Token"] = refreshed

        return response


def _maybe_refresh_bearer(token: str) -> str | None:
    """Return a fresh access token if the given token is past half-life, else None."""
    import base64 as _b64
    import json as _json

    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        claims = _json.loads(_b64.urlsafe_b64decode(payload_b64 + "=" * (padding % 4)))
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        from celerp.config import settings
        total_ttl = int(settings.access_token_expire_minutes) * 60
        issued_at = exp - total_ttl
        elapsed = time.time() - issued_at
        if elapsed <= total_ttl / 2:
            return None
        sub = claims.get("sub")
        company_id = claims.get("company_id")
        role = claims.get("role", "")
        if not sub or not company_id:
            return None
        from celerp.services.auth import create_access_token
        return create_access_token(sub, company_id, role)
    except Exception:
        return None


def log_unhandled_exception(request: Request, exc: Exception) -> None:
    logger.exception(
        json.dumps(
            {
                "event": "unhandled_exception",
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "client": request.client.host if request.client else None,
            }
        ),
        exc_info=exc,
    )
