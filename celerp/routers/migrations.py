# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company migration routes.

Two entry modes share one flow (scan, decide, start, watch, verify, finish):

- bootstrap: a fresh install with no user yet. The routes are public, closed for good
  once any user exists, and gated by the one-time setup code where one is configured.
  Start creates the first owner and the staged company in one transaction.
- company: an authenticated company owner moves another company in.

Run routes are scoped to the caller's company: a run of another company is not found.
Any member can read a run and its verification; only the owner acts on it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.importers.adapters.registry import list_adapters
from celerp.models.company import User
from celerp.models.migration import MigrationRun
from celerp.routers.auth import limiter
from celerp.services import bootstrap
from celerp.services import migration_scan_store as store
from celerp.services import migrations
from celerp.services.auth import (
    MIN_PASSWORD_LENGTH,
    AuthContext,
    get_auth_context,
    issue_token_pair,
    validate_password,
)
from celerp.services.permissions import role_has_permission
from celerp.services.provisioning import create_install_owner, provision_migration_company

logger = logging.getLogger(__name__)



class _MigrationRoute(APIRoute):
    """Answers a refused migration request with its own detail.

    The app-wide 404 handler replaces every 404 detail with a generic one; a migration the caller
    cannot see must still say "Migration not found.", so migration errors become responses here.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except (migrations.MigrationError, store.ScanStoreError) as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

        return route


router = APIRouter(prefix="/migrations", tags=["migrations"], route_class=_MigrationRoute)

OWNER_ONLY = "Only the company owner can move a company into Celerp."
BOOTSTRAPPED = "System already bootstrapped. Contact your admin."
BOOTSTRAP: store.ScanOwner = ("bootstrap", None)
_NAME_MAX = 200


class ScanTokenIn(BaseModel):
    scan_token: str


class DecisionsIn(BaseModel):
    """Values are checked by ``migrations.validate_decisions`` so every problem is explained."""
    scan_token: str
    mode: Any = None
    cutover_date: Any = None
    mappings: Any = None
    prepared_by: Any = None


class StartFromScanIn(BaseModel):
    scan_token: str
    company_name: str


class BootstrapStartIn(BaseModel):
    scan_token: str
    company_name: str
    name: str
    email: str
    password: str


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _upload_parts(request: Request) -> AsyncIterator[store.UploadPart]:
    """Stream multipart fields to the scan store without buffering the body.

    A request that is not multipart yields nothing, which the store reports as no file."""
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        return
    events: deque[tuple[str, Any]] = deque()
    headers: dict[bytes, bytes] = {}
    field, value = bytearray(), bytearray()

    def on_header_end() -> None:
        headers[bytes(field).lower()] = bytes(value)
        field.clear()
        value.clear()

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if end > start:
            events.append(("data", bytes(data[start:end])))

    parser = MultipartParser(boundary, {
        "on_part_begin": headers.clear,
        "on_header_field": lambda data, start, end: field.extend(data[start:end]),
        "on_header_value": lambda data, start, end: value.extend(data[start:end]),
        "on_header_end": on_header_end,
        "on_headers_finished": lambda: events.append(("part", dict(headers))),
        "on_part_data": on_part_data,
        "on_part_end": lambda: events.append(("end", None)),
    })
    stream = request.stream()

    async def next_event() -> tuple[str, Any] | None:
        while not events:
            try:
                chunk = await anext(stream)
            except StopAsyncIteration:
                return None
            try:
                parser.write(chunk)
            except MultipartParseError as exc:
                raise store.ScanStoreError(422, "The upload could not be read. Try again.") from exc
        return events.popleft()

    async def chunks() -> AsyncIterator[bytes]:
        while (event := await next_event()) is not None and event[0] != "end":
            if event[0] == "data":
                yield event[1]

    while (event := await next_event()) is not None:
        if event[0] != "part":
            continue
        disposition = parse_options_header(event[1].get(b"content-disposition", b""))[1]
        filename = disposition.get(b"filename")
        part = store.UploadPart(disposition.get(b"name", b"").decode("utf-8", "replace"),
                                filename.decode("utf-8", "replace") if filename is not None else None,
                                chunks())
        yield part
        async for _ in part.chunks:  # whatever the store left unread
            pass


def _require_owner(ctx: AuthContext) -> None:
    if not role_has_permission(ctx.company.settings, ctx.role, "manage_company_lifecycle"):
        raise HTTPException(status_code=403, detail=OWNER_ONLY)


async def _user_owner(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    _require_owner(ctx)
    return ctx


async def _ensure_not_bootstrapped(session: AsyncSession) -> None:
    if await session.scalar(select(User.id).limit(1)) is not None:
        raise HTTPException(status_code=409, detail=BOOTSTRAPPED)


def _company_name(value: str, errors: dict) -> str:
    name = value.strip()
    if not name:
        errors["company_name"] = "Enter a company name."
    elif len(name) > _NAME_MAX:
        errors["company_name"] = f"The company name must be at most {_NAME_MAX} characters."
    return name


async def _prepare(scan: store.ScanSession):
    return await asyncio.to_thread(migrations.prepare_start, scan)


async def _save_decisions(owner: store.ScanOwner, payload: DecisionsIn) -> dict:
    scan = store.load_scan(payload.scan_token, owner=owner)
    decisions = await asyncio.to_thread(migrations.validate_decisions, scan, payload.model_dump(exclude={"scan_token"}))
    return {"scan": migrations.scan_view(store.save_decisions(payload.scan_token, owner=owner, decisions=decisions))}


async def _start(session: AsyncSession, *, user: User, company_name: str, scan: store.ScanSession,
                 decisions, expected_snonce: str | None) -> dict:
    """Stage the company, claim the scan and persist the run as running, in one commit.

    Returns the token pair for the new company plus the run id; the caller schedules the runner."""
    company = await provision_migration_company(session, owner=user, company_name=company_name)
    run = await migrations.create_run(session, company=company, user=user, scan=scan, decisions=decisions)
    try:
        await migrations.request_start(session, run)
        tokens = await issue_token_pair(session, user=user, company=company, role="owner",
                                        expected_snonce=expected_snonce)
    except BaseException:
        migrations.remove_source(run.id)
        raise
    return {**tokens, "run_id": str(run.id)}


async def _run(session: AsyncSession, run_id: uuid.UUID, ctx: AuthContext) -> MigrationRun:
    return await migrations.get_run_for_company(session, run_id, ctx.company_id)


async def _owned_run(session: AsyncSession, run_id: uuid.UUID, ctx: AuthContext) -> MigrationRun:
    run = await _run(session, run_id, ctx)  # another company's run is not found, before any role check
    _require_owner(ctx)
    return run


# ── Sources ──────────────────────────────────────────────────────────────────

@router.get("/sources")
async def sources() -> list[dict]:
    return [{
        "key": a.key, "display_name": a.display_name,
        "artifacts": [{"key": s.key, "label": s.label, "extensions": list(s.extensions), "max_bytes": s.max_bytes}
                      for s in a.artifact_specs],
    } for a in list_adapters()]


# ── Bootstrap (no user exists yet) ───────────────────────────────────────────

@router.post("/bootstrap/scan")
@limiter.limit("5/minute")
async def bootstrap_scan(request: Request, session: AsyncSession = Depends(get_session),
                         x_setup_code: str | None = Header(None)) -> dict:
    await _ensure_not_bootstrapped(session)
    bootstrap.verify_setup_code(x_setup_code)
    scan = await store.create_scan(_upload_parts(request), owner=BOOTSTRAP)
    return {"scan_token": scan.token, "scan": migrations.scan_view(scan)}


@router.post("/bootstrap/scan/read")
async def bootstrap_scan_read(payload: ScanTokenIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _ensure_not_bootstrapped(session)
    return {"scan": migrations.scan_view(store.load_scan(payload.scan_token, owner=BOOTSTRAP))}


@router.post("/bootstrap/decisions")
async def bootstrap_decisions(payload: DecisionsIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _ensure_not_bootstrapped(session)
    return await _save_decisions(BOOTSTRAP, payload)


@router.post("/bootstrap/start", status_code=201)
async def bootstrap_start(payload: BootstrapStartIn, session: AsyncSession = Depends(get_session),
                          x_setup_code: str | None = Header(None)) -> dict:
    """Create the first owner, the staged company and the running migration in one commit.

    The bootstrap lock serializes racing starts; the loser re-checks and is refused.
    The setup code is consumed only after the commit."""
    required = False
    try:
        await _ensure_not_bootstrapped(session)
        required = bootstrap.verify_setup_code(x_setup_code)
        await bootstrap.lock_bootstrap(session)
        await _ensure_not_bootstrapped(session)
        scan = store.load_scan(payload.scan_token, owner=BOOTSTRAP)
        decisions = await _prepare(scan)
        errors: dict[str, str] = {}
        company_name = _company_name(payload.company_name, errors)
        name, email = payload.name.strip(), payload.email.strip()
        if not name:
            errors["name"] = "Enter your name."
        if "@" not in email:
            errors["email"] = "Enter a valid email address."
        try:
            validate_password(payload.password)
        except ValueError:
            errors["password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        user = await create_install_owner(session, name=name, email=email, password=payload.password)
        result = await _start(session, user=user, company_name=company_name, scan=scan, decisions=decisions,
                              expected_snonce=None)
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        logger.exception("Bootstrap migration start failed")
        raise HTTPException(status_code=500, detail="Migration could not start.") from None
    if required:
        try:
            await asyncio.to_thread(bootstrap.clear_setup_code)
        except Exception:
            logger.warning("Setup-code cleanup failed after bootstrap migration start", exc_info=True)
    migrations.schedule_run(uuid.UUID(result["run_id"]))
    return result


# ── Company owner ────────────────────────────────────────────────────────────

@router.post("/scan")
async def scan(request: Request, ctx: AuthContext = Depends(_user_owner)) -> dict:
    result = await store.create_scan(_upload_parts(request), owner=("user", ctx.user.id))
    return {"scan_token": result.token, "scan": migrations.scan_view(result)}


@router.post("/scan/read")
async def scan_read(payload: ScanTokenIn, ctx: AuthContext = Depends(_user_owner)) -> dict:
    return {"scan": migrations.scan_view(store.load_scan(payload.scan_token, owner=("user", ctx.user.id)))}


@router.post("/scan/decisions")
async def scan_decisions(payload: DecisionsIn, ctx: AuthContext = Depends(_user_owner)) -> dict:
    return await _save_decisions(("user", ctx.user.id), payload)


@router.post("/start-from-scan", status_code=201)
async def start_from_scan(payload: StartFromScanIn, ctx: AuthContext = Depends(_user_owner),
                          session: AsyncSession = Depends(get_session)) -> dict:
    scan = store.load_scan(payload.scan_token, owner=("user", ctx.user.id))
    decisions = await _prepare(scan)
    errors: dict[str, str] = {}
    company_name = _company_name(payload.company_name, errors)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    try:
        result = await _start(session, user=ctx.user, company_name=company_name, scan=scan, decisions=decisions,
                              expected_snonce=ctx.snonce)
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        logger.exception("Migration start failed")
        raise HTTPException(status_code=500, detail="Migration could not start.") from None
    migrations.schedule_run(uuid.UUID(result["run_id"]))
    return result


# ── Runs ─────────────────────────────────────────────────────────────────────

@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                  session: AsyncSession = Depends(get_session)) -> dict:
    await migrations.mark_stale_runs_interrupted(session, ctx.company_id)
    return await migrations.run_view(session, await _run(session, run_id, ctx))


@router.get("/{run_id}/reconciliation")
async def get_reconciliation(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                             session: AsyncSession = Depends(get_session)) -> dict:
    run = await _run(session, run_id, ctx)
    if not run.reconciliation:
        raise HTTPException(status_code=409, detail="Verification has not run yet.")
    return run.reconciliation


@router.get("/{run_id}/reconciliation/pack")
async def get_reconciliation_pack(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                                  session: AsyncSession = Depends(get_session)) -> Response:
    run = await _run(session, run_id, ctx)
    if not run.reconciliation:
        raise HTTPException(status_code=409, detail="Verification has not run yet.")
    try:
        body = migrations.reconciliation_pack_csv(run)
    except Exception:
        logger.exception("Reconciliation pack for migration %s failed", run.id)
        raise HTTPException(status_code=500, detail="Could not build the reconciliation pack.") from None
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="reconciliation-{run.id}.csv"'})


@router.post("/{run_id}/start", status_code=202)
async def start_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                    session: AsyncSession = Depends(get_session)) -> dict:
    run = await _owned_run(session, run_id, ctx)
    try:
        await migrations.request_start(session, run)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    migrations.schedule_run(run.id)
    return await migrations.run_view(session, run)


@router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                     session: AsyncSession = Depends(get_session)) -> dict:
    run = await _owned_run(session, run_id, ctx)
    try:
        await migrations.request_cancel(session, run)
    except BaseException:
        await session.rollback()
        raise
    return await migrations.run_view(session, run)


@router.post("/{run_id}/finalize")
async def finalize_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                       session: AsyncSession = Depends(get_session)) -> dict:
    run = await _owned_run(session, run_id, ctx)
    try:
        await migrations.finalize(session, run)
    except BaseException:
        await session.rollback()
        raise
    return await migrations.run_view(session, run)


@router.post("/{run_id}/discard")
async def discard_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                      session: AsyncSession = Depends(get_session)) -> dict:
    run = await _owned_run(session, run_id, ctx)
    try:
        redirect = await migrations.discard(session, run)
    except BaseException:
        await session.rollback()
        raise
    return {"redirect": redirect}
