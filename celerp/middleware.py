# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Imported at module level so tests can patch celerp.middleware.is_draining
# and celerp.middleware.get_session_ctx
from celerp.db import get_session_ctx
from celerp.held_back import HeldBack, held_back
from celerp.services.runtime_state import is_draining
from ui.i18n import t

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
    """Pure ASGI middleware - rejects request bodies larger than the limit.

    A declared Content-Length over the limit is refused before the app runs. A
    body sent without one (chunked) is counted as it streams: once it passes the
    limit the app sees the client as gone and the response is a 413. Nothing is
    buffered here, so streaming responses (CSV exports, SSE) are unaffected.
    """

    def __init__(self, app: ASGIApp, max_body_size_bytes: int) -> None:
        self.app = app
        self.max_body_size_bytes = int(max_body_size_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Bulk file/cert import and backup restore legitimately upload large archives;
        # exempt them from the body cap. Bulk imports are bounded by the WS tunnel frame
        # size and the per-file 50 MB limit in store_upload(); a .celerp-backup restore is
        # a trusted whole-instance archive (DB + files) that is inherently large. The two
        # migration scan uploads stream to disk under their own per-file and total caps, and a
        # company backup, which carries the company's attachment files, is staged under the
        # same total cap.
        if scope.get("path", "").endswith(
            ("/items/files/bulk", "/items/attachments/bulk", "/backup/import", "/backup/import-bootstrap",
             "/migrations/bootstrap/scan", "/migrations/scan", "/company-backups/read",
             "/company-backups/bootstrap/read")
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_body_size_bytes:
                    response = JSONResponse(status_code=413, content={"detail": t("error.upload_too_large")})
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                await response(scope, receive, send)
                return

        limit = self.max_body_size_bytes
        received = 0
        too_large = False
        started = False

        async def counted_receive() -> dict:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict) -> None:
            nonlocal started
            if too_large and not started:
                return  # the 413 below replaces whatever the app answered
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not started:
            await JSONResponse(status_code=413, content={"detail": t("error.upload_too_large")})(scope, receive, send)


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
                    refreshed = await _refresh_bearer_validated(token)
                    if refreshed:
                        message = dict(message)
                        message["headers"] = list(message.get("headers", [])) + [
                            (b"x-refreshed-token", refreshed.encode("latin-1"))
                        ]
            await send(message)

        await self.app(scope, receive, send_with_refresh)


def _past_half_life(exp: object) -> bool:
    """True when a token with expiry *exp* (unix seconds) is past half its TTL."""
    if not isinstance(exp, (int, float)):
        return False
    from celerp.config import settings
    capped_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    total_ttl = capped_minutes * 60
    issued_at = exp - total_ttl
    return (time.time() - issued_at) > total_ttl / 2


async def _refresh_bearer_validated(token: str) -> str | None:
    """The request-path sliding refresh: return a freshly signed access token, or
    None when the bearer must not be re-minted.

    This runs at the ASGI layer BEFORE any route dependency, so it establishes DB
    authority itself rather than trusting that a route validated the token:

    - Fully validate the bearer via ``validate_access_token`` (signature, v2
      contract, access type, active user + membership, company-active rule, and
      exact per-user nonce). A forged, refresh-typed, revoked, or expired token
      raises and is never re-minted.
    - Only when it validates and is past half-life, re-mint through the single
      issuance point, reusing the original JTI so the session slot is not
      duplicated. The role comes from current DB membership (``ctx.role``), never
      the token claim, so a demoted user's refreshed token reflects the demotion.

    Fails closed: any DB or validation error yields no refreshed token.
    """
    from celerp.credentials import issue_token_pair
    from celerp.services.auth import validate_access_token
    from fastapi import HTTPException

    try:
        async with get_session_ctx() as s:
            try:
                ctx = await validate_access_token(s, token)
            except HTTPException:
                return None
            if not _past_half_life(ctx.claims.get("exp")):
                return None
            # Sliding refresh is a continuation of the authenticated session:
            # pass the snonce it validated on so a concurrent revocation cannot
            # be jumped over (the reused JTI would otherwise re-register onto the
            # newer generation).
            pair = await issue_token_pair(
                s,
                user=ctx.user,
                company_id=ctx.company.id,
                jti=ctx.claims["jti"],
                expected_snonce=ctx.snonce,
            )
            return pair["access_token"]
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
_DRAIN_BYPASS_PREFIXES = ("/__celerp/", "/health")
# What still changes while the stored records are not current: signing in and out,
# reading notices, enabling or installing the module that holds them back, updating
# Celerp, and the repairs that bring them current. Nothing here changes a business record:
# the doctor is allowed for its report, and refuses its repairs itself (run_doctor).
_HELD_BACK_ALLOWED_PREFIXES = ("/auth/", "/notifications", "/companies/me/modules/", "/system/restart",
                               "/system/update", "/ledger/rebuild", "/admin/doctor")
_HELD_BACK_REFUSED_SUFFIXES = ("/purge-data",)


def _refused_while_held_back(scope: Scope, path: str) -> HeldBack | None:
    """Why a change to records is refused: the last start could not bring them current."""
    cause = held_back(scope.get("app"))
    if cause is None or (path.startswith(_HELD_BACK_ALLOWED_PREFIXES)
                         and not path.endswith(_HELD_BACK_REFUSED_SUFFIXES)):
        return None
    return cause


class DrainMiddleware:
    """Return 503 on write requests while the cluster is draining, or while the last
    start could not bring the stored records current (``app.state.data_current``).

    Reads the drain flag from ``SystemRuntimeState`` on every write request.
    Fails open (passes the request through) if the DB is unreachable so that
    a DB hiccup doesn't hard-block all mutations.

    Safe paths (bypass): /__celerp/*, /health. While the records are not current,
    only the sign-in, notice, module, update and repair paths above still accept writes.
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

        if (cause := _refused_while_held_back(scope, path)) is not None:
            await JSONResponse(status_code=503, content={"detail": cause.refusal()})(scope, receive, send)
            return

        try:
            async with get_session_ctx() as s:
                draining = await is_draining(s)
        except Exception:
            draining = False  # fail open

        if draining:
            response = JSONResponse(
                status_code=503,
                content={"detail": t("error.maintenance")},
                headers={"Retry-After": "10"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class OlderStockMiddleware:
    """Run a write request once more after placing older stock it was refused for.

    A request refused only because a lot it needs records no inventory account yet
    (lot_origin.PLACEABLE_REFUSALS) has ended, rolled back, holding nothing. Its
    company's stock is then placed the one way (lot_origin.place_unplaced_stock) in a
    transaction of its own, and once none waits the request runs again, once, and its
    answer is the one returned. Placing that cannot happen (a posting account or a
    period lock holds it back, each with its own notice) returns the refusal as it was.
    A request that committed any write before it was refused (_COMMITTED) is never run
    again, so nothing it wrote is written twice; the stock is still placed and the
    refusal returned. The company is the one the request was authorised for
    (auth.get_auth_context)."""

    _REFUSED = (409, 422)

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        body, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return  # the client went away before sending the request
            body.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(body)
        token = _COMMITTED.set(committed := [])
        try:
            held = await self._run(scope, body, receive, send)
        finally:
            _COMMITTED.reset(token)
        if held is None:
            return
        company_id = (scope.get("state") or {}).get("company_id")
        if company_id is not None and _placeable(held) and await _placed(company_id) and not committed:
            await self._run(scope, body, receive, send, hold=False)
            return
        for message in held:
            await send(message)

    async def _run(self, scope: Scope, body: bytes, receive: Receive, send: Send, *,
                   hold: bool = True) -> list[dict] | None:
        """Run the request on ``body``. A refusal is kept and returned instead of sent
        when ``hold``; anything else is sent as it comes and None returned."""
        sent = False
        held: list[dict] = []

        async def replay() -> dict:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def keep(message: dict) -> None:
            if held or (hold and message["type"] == "http.response.start" and message["status"] in self._REFUSED):
                held.append(message)
            else:
                await send(message)

        await self.app(scope, replay, keep)
        return held or None


# Set while OlderStockMiddleware runs a request the first time: a commit that wrote
# anything appends to it, so the request is known to have kept a write. A savepoint
# released inside the request is not a commit; only the outermost transaction counts.
_COMMITTED: ContextVar[list | None] = ContextVar("older_stock_committed", default=None)


@event.listens_for(Session, "after_flush")
def _flushed(session: Session, _context) -> None:
    if _COMMITTED.get() is not None and (session.new or session.dirty or session.deleted):
        session.info["older_stock_wrote"] = True


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    if session.in_nested_transaction():
        return
    if session.info.pop("older_stock_wrote", False) and (committed := _COMMITTED.get()) is not None:
        committed.append(True)


@event.listens_for(Session, "after_transaction_end")
def _ended(session: Session, transaction) -> None:
    if transaction.parent is None:  # committed (counted above), rolled back or closed
        session.info.pop("older_stock_wrote", None)


def _placeable(held: list[dict]) -> bool:
    """Whether a held refusal is one placing older stock can lift."""
    from celerp.services.lot_origin import PLACEABLE_REFUSALS

    try:
        detail = json.loads(b"".join(m.get("body", b"") for m in held[1:])).get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("message_key") in PLACEABLE_REFUSALS


async def _placed(company_id) -> bool:
    from celerp.services.lot_origin import place_unplaced_stock

    async with get_session_ctx() as session:
        await session.rollback()  # nothing of the refused request is kept
        placed = await place_unplaced_stock(session, company_id)
        await session.commit()
    return placed


async def notify_refused(company_id, roles: list[str]) -> None:
    """A posting was refused because ``roles`` have no account that can take it. The
    refused request has rolled back, so those who manage accounting are notified in a
    transaction of its own (posting_readiness.notify_unmapped)."""
    from celerp.services.posting_readiness import notify_unmapped

    async with get_session_ctx() as session:
        await session.rollback()
        await notify_unmapped(session, company_id, roles)
        await session.commit()


# The anonymous liveness probes, matched exactly: the only routes an unfinished
# System Recovery still serves. Readiness is refused until the recovery converges,
# so nothing routes traffic to an installation that answers only 503s. Never a
# prefix, so no authenticated or state-reading route under /health or /__celerp/
# gets through.
_RECOVERY_PROBES = frozenset({"/health", "/__celerp/health"})


class ModuleStartupMiddleware:
    """Serve no module route until the UI process has reported which modules it
    started (celerp.modules.outcome), so a module that failed there is stopped
    here before any of its routes answer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from celerp.db import lifecycle_engine
        from celerp.modules import outcome
        from celerp.modules.loader import route_module

        if (scope["type"] != "http" or outcome.ui_report_applied()
                or route_module(scope) is None):
            await self.app(scope, receive, send)
            return
        try:
            ready = await outcome.confirm_ui_report(scope["app"], lifecycle_engine)
        except Exception:
            logger.exception("Reading the module outcome record failed")
            ready = False
        if ready:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(status_code=503, content={"detail": outcome.STARTING})
        await response(scope, receive, send)


class RecoveryMaintenanceMiddleware:
    """Serve nothing but the liveness probes while a System Recovery is unfinished.

    Until the recovery finishes or is undone, the database, files and modules may
    not agree, and no session from before the replacement may be honoured.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from celerp.services.backup_import import recovery_incomplete

        if (scope["type"] != "http" or scope.get("path", "") in _RECOVERY_PROBES
                or not recovery_incomplete()):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=503,
            content={"detail": t("error.recovery_incomplete")},
        )
        await response(scope, receive, send)
