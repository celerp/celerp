# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import json
import logging
import time
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Imported at module level so tests can patch celerp.middleware.is_draining
# and celerp.middleware.get_session_ctx
from celerp.db import get_session_ctx
from celerp.services.runtime_state import is_draining

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware - avoids BaseHTTPMiddleware body_stream CancelledError on shutdown."""

    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'",
        ),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        existing_keys: set[bytes] = set()

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                existing_keys.update(k.lower() for k, _ in message.get("headers", []))
                extra = [(k, v) for k, v in self._HEADERS if k not in existing_keys]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


class MaxBodySizeMiddleware:
    """Pure ASGI middleware - rejects requests whose Content-Length exceeds the limit.

    Only checks the Content-Length header - does not buffer the body, which
    avoids conflicts with streaming responses (CSV exports, SSE, etc.).
    Clients that omit Content-Length on large uploads are not covered here;
    that is acceptable for the current use case (JSON API).
    """

    def __init__(self, app: ASGIApp, max_body_size_bytes: int) -> None:
        self.app = app
        self.max_body_size_bytes = int(max_body_size_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Bulk file/cert import legitimately uploads a large ZIP (many small files);
        # exempt it from the body cap. It's bounded instead by the WS tunnel frame
        # size and the per-file 50 MB limit in store_upload().
        if scope.get("path", "").endswith(("/items/files/bulk", "/items/attachments/bulk")):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_body_size_bytes:
                    response = JSONResponse(status_code=413, content={"detail": "Request too large"})
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class SlidingTokenRefreshMiddleware:
    """Pure ASGI sliding-window JWT refresh for Bearer token (API) clients.

    On every successful (2xx) authenticated response, if the Bearer token is
    past half its lifetime, issue a fresh access token and include it in the
    ``X-Refreshed-Token`` response header. The client should replace its stored
    token with this value to maintain a sliding session.

    No-op when:
    - No Authorization: Bearer header is present
    - Token decode fails (invalid/expired - the route handler already rejected it)
    - Response status >= 300 (redirects, errors)
    - Token has not yet consumed half its TTL
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None

        status_holder: list[int] = []

        async def send_with_refresh(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder.append(message["status"])
                if message["status"] < 300 and token:
                    result = _maybe_refresh_bearer(token)
                    if result:
                        refreshed, jti, new_expiry = result
                        message = dict(message)
                        message["headers"] = list(message.get("headers", [])) + [
                            (b"x-refreshed-token", refreshed.encode("latin-1"))
                        ]
                        # Update JTI expiry in DB so active_user_ids() stays accurate
                        try:
                            from celerp.models.auth import SessionRegistry
                            async with get_session_ctx() as s:
                                row = await s.get(SessionRegistry, jti)
                                if row is not None:
                                    row.expiry = new_expiry
                                    await s.commit()
                        except Exception:
                            pass  # fail open - expiry update is best-effort
            await send(message)

        await self.app(scope, receive, send_with_refresh)


def _maybe_refresh_bearer(token: str) -> tuple[str, str, datetime] | None:
    """Return (new_token, jti, new_expiry) if past half-life, else None.

    Reuses the original JTI so the session slot is not duplicated in the registry.
    The caller is responsible for updating the JTI row's expiry in the DB.
    """
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
        capped_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
        total_ttl = capped_minutes * 60
        issued_at = exp - total_ttl
        elapsed = time.time() - issued_at
        if elapsed <= total_ttl / 2:
            return None
        sub = claims.get("sub")
        company_id = claims.get("company_id")
        role = claims.get("role", "")
        jti = claims.get("jti")
        snonce = claims.get("snonce", "")
        if not sub or not company_id or not jti:
            return None
        from celerp.services.auth import create_access_token
        # Reuse the existing snonce - the token was already validated by get_current_user,
        # so the nonce is correct.  No DB call needed here (sync function).
        new_token, _token_jti = create_access_token(sub, company_id, role, jti=jti, snonce=snonce)
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        new_expiry = _dt.now(_tz.utc) + _td(seconds=total_ttl)
        return new_token, jti, new_expiry
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


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DRAIN_BYPASS_PREFIXES = ("/__celerp/", "/health", "/auth/session-watch")


class DrainMiddleware:
    """Return 503 on write requests while the cluster is draining.

    Reads the drain flag from ``SystemRuntimeState`` on every write request.
    Fails open (passes the request through) if the DB is unreachable so that
    a DB hiccup doesn't hard-block all mutations.

    Safe paths (bypass): /__celerp/*, /health, /auth/session-watch.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        if method not in _WRITE_METHODS or any(path.startswith(p) for p in _DRAIN_BYPASS_PREFIXES):
            await self.app(scope, receive, send)
            return

        try:
            async with get_session_ctx() as s:
                draining = await is_draining(s)
        except Exception:
            draining = False  # fail open

        if draining:
            response = JSONResponse(
                status_code=503,
                content={"detail": "Server is temporarily unavailable for maintenance"},
                headers={"Retry-After": "10"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
