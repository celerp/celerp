# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company migration routes.

Two entry modes share one flow (scan, decide, start, watch, verify, finish):

- bootstrap: a fresh install with no user yet. The routes are public, closed for good
  once any user exists, and gated by the one-time setup code where one is configured.
  Start creates the first owner and the staged company in one transaction.
- company: an authenticated company owner moves another company in. Their session stays
  on the company they are working in; they open the new company only after finishing.

Run routes belong to the run's creator while they own its company, whichever company
their current token is scoped to (``migrations.get_owned_migration_run``); to anyone
else a run is not found.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.routing import APIRoute
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.importers.adapters.registry import list_adapters
from celerp.models.company import Company, User
from celerp.models.migration import MigrationRun
from celerp.modules import requirements
from celerp.routers.auth import companyless_login, hold_direct_slot, limiter
from celerp.services import bootstrap
from celerp.services import migration_scan_store as store
from celerp.services import migrations
from celerp.services.auth import (
    HAS_COMPANY,
    MIN_PASSWORD_LENGTH,
    AuthContext,
    get_auth_context,
    hold_companyless_login,
    issue_token_pair,
    validate_password,
)
from celerp.services.permissions import role_has_permission
from celerp.services.provisioning import create_install_owner, provision_migration_company
from ui.i18n import t

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
MODULES_NOT_SAVED = ("Celerp could not turn on the features this migration needs, so nothing was created. "
                     "Check that Celerp can save its settings, then try again.")
BOOTSTRAP: store.ScanOwner = ("bootstrap", None)


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


class StartCompanyStartIn(BaseModel):
    email: str
    password: str
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


async def user_owner(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not role_has_permission(ctx.company.settings, ctx.role, "manage_company_lifecycle"):
        raise HTTPException(status_code=403, detail=OWNER_ONLY)
    return ctx


async def ensure_not_bootstrapped(session: AsyncSession) -> None:
    if await session.scalar(select(User.id).limit(1)) is not None:
        raise HTTPException(status_code=409, detail=BOOTSTRAPPED)


def _company_name(value: str, errors: dict) -> str:
    name = value.strip()
    if (error := migrations.company_name_error(name)) is not None:
        errors["company_name"] = error
    return name


_EMAIL_MAX = 320  # users.email is String(320)


def owner_account(name: str, email: str, password: str, errors: dict) -> tuple[str, str]:
    """Check the first owner's name, email and password, recording each problem in
    ``errors``; returns the trimmed name and email."""
    name, email = name.strip(), email.strip()
    if not name:
        errors["name"] = "Enter your name."
    elif "\x00" in name:
        errors["name"] = "Your name contains a character that cannot be saved."
    if "@" not in email or "\x00" in email or len(email) > _EMAIL_MAX:
        errors["email"] = "Enter a valid email address."
    try:
        validate_password(password)
    except ValueError:
        errors["password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return name, email


async def _prepare(scan: store.ScanSession):
    return await asyncio.to_thread(migrations.prepare_start, scan)


async def _save_decisions(owner: store.ScanOwner, payload: DecisionsIn) -> dict:
    scan = store.load_scan(payload.scan_token, owner=owner)
    decisions = await asyncio.to_thread(migrations.validate_decisions, scan, payload.model_dump(exclude={"scan_token"}))
    return {"scan": migrations.scan_view(store.save_decisions(payload.scan_token, owner=owner, decisions=decisions))}


async def _turn_on_modules(plan: migrations.StartPlan) -> bool:
    """Turn on, in the installation's configuration, the bundled modules the migration
    needs, before anything is created. Returns whether a restart must load them."""
    try:
        return await asyncio.to_thread(requirements.prepare, plan.requirements)
    except OSError:
        logger.exception("Could not turn on the modules a migration needs")
        raise HTTPException(status_code=503, detail=MODULES_NOT_SAVED) from None


async def _stage(session: AsyncSession, *, user: User, company_name: str, scan: store.ScanSession,
                 plan: migrations.StartPlan, awaiting: bool) -> MigrationRun:
    """Start, phase one: the staged company and a preparing run holding the scan's claim.
    The caller holds the claim lock and commits."""
    company = await provision_migration_company(session, owner=user, company_name=company_name,
                                                settings=migrations.staged_settings(plan.modules))
    return await migrations.create_run(session, company=company, user=user, scan=scan, decisions=plan.decisions,
                                       modules=plan.modules, awaiting=awaiting)


@asynccontextmanager
async def _start_errors(session: AsyncSession) -> AsyncIterator[None]:
    """Roll back a failed start step; an unexpected failure is logged and shown plainly."""
    try:
        yield
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        logger.exception("Migration start failed")
        raise HTTPException(status_code=500, detail="Migration could not start.") from None


async def _claim(session: AsyncSession, run_id: uuid.UUID, scan_token: str, *, awaiting: bool) -> None:
    """Start, phase two, after phase one committed: move the source into the run and start
    it, or, while it waits for its modules, restart Celerp to load them; startup then
    starts the same run. Only the call that started the run schedules the runner."""
    async with _start_errors(session):
        started = await migrations.claim_source(session, run_id, token=scan_token, start=not awaiting)
    if started:
        migrations.schedule_run(run_id)
    elif awaiting:
        requirements.schedule_restart()


async def _owned_run(session: AsyncSession, run_id: uuid.UUID, ctx: AuthContext) -> MigrationRun:
    return await migrations.get_owned_migration_run(session, run_id, ctx.user.id)


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
    await ensure_not_bootstrapped(session)
    bootstrap.verify_setup_code(x_setup_code)
    scan = await store.create_scan(_upload_parts(request), owner=BOOTSTRAP)
    return {"scan_token": scan.token, "scan": migrations.scan_view(scan)}


@router.post("/bootstrap/scan/read")
async def bootstrap_scan_read(payload: ScanTokenIn, session: AsyncSession = Depends(get_session)) -> dict:
    await ensure_not_bootstrapped(session)
    return {"scan": migrations.scan_view(store.load_scan(payload.scan_token, owner=BOOTSTRAP))}


@router.post("/bootstrap/decisions")
async def bootstrap_decisions(payload: DecisionsIn, session: AsyncSession = Depends(get_session)) -> dict:
    await ensure_not_bootstrapped(session)
    return await _save_decisions(BOOTSTRAP, payload)


@router.post("/bootstrap/start", status_code=201)
async def bootstrap_start(payload: BootstrapStartIn, session: AsyncSession = Depends(get_session),
                          x_setup_code: str | None = Header(None)) -> dict:
    """Create the first owner, the staged company and a preparing run in one commit, then
    start it (``_claim``).

    The bootstrap lock serializes racing starts; the loser re-checks and is refused. If the
    response is lost after the commit, the owner signs in and is taken back to the run.
    The setup code is consumed only after the commit."""
    required = False
    async with _start_errors(session):
        await ensure_not_bootstrapped(session)
        required = bootstrap.verify_setup_code(x_setup_code)
        await bootstrap.lock_bootstrap(session)
        await ensure_not_bootstrapped(session)
        scan = store.load_scan(payload.scan_token, owner=BOOTSTRAP)
        plan = await _prepare(scan)
        errors: dict[str, str] = {}
        company_name = _company_name(payload.company_name, errors)
        name, email = owner_account(payload.name, payload.email, payload.password, errors)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        awaiting = await _turn_on_modules(plan)
        user = await create_install_owner(session, name=name, email=email, password=payload.password)
        run = await _stage(session, user=user, company_name=company_name, scan=scan, plan=plan, awaiting=awaiting)
        run_id = run.id
        # The first owner has no other company, so they are signed in to the staged one;
        # its token reaches the migration routes only. Issuing the tokens commits.
        tokens = await issue_token_pair(session, user=user, company_id=run.company_id)
    if required:
        try:
            await asyncio.to_thread(bootstrap.clear_setup_code)
        except Exception:
            logger.warning("Setup-code cleanup failed after bootstrap migration start", exc_info=True)
    await _claim(session, run_id, payload.scan_token, awaiting=awaiting)
    return {**tokens, "run_id": str(run_id), "preparing": awaiting}


# ── Company owner ────────────────────────────────────────────────────────────

@router.post("/scan")
async def scan(request: Request, ctx: AuthContext = Depends(user_owner)) -> dict:
    result = await store.create_scan(_upload_parts(request), owner=("user", ctx.user.id))
    return {"scan_token": result.token, "scan": migrations.scan_view(result)}


@router.post("/scan/read")
async def scan_read(payload: ScanTokenIn, ctx: AuthContext = Depends(user_owner),
                    session: AsyncSession = Depends(get_session)) -> dict:
    """The scan's view, or ``{"run_id"}`` of the run the caller already started from it,
    so a wizard whose start response was lost returns to that run."""
    run_id = await migrations.started_run_id(session, store.scan_claim(payload.scan_token), ctx.user.id)
    if run_id is not None:
        return {"run_id": str(run_id)}
    return {"scan": migrations.scan_view(store.load_scan(payload.scan_token, owner=("user", ctx.user.id)))}


@router.post("/scan/decisions")
async def scan_decisions(payload: DecisionsIn, ctx: AuthContext = Depends(user_owner)) -> dict:
    return await _save_decisions(("user", ctx.user.id), payload)


@router.post("/start-from-scan", status_code=201)
async def start_from_scan(payload: StartFromScanIn, response: Response, ctx: AuthContext = Depends(user_owner),
                          session: AsyncSession = Depends(get_session)) -> dict:
    """Start a migration from a scan, 201. A repeated start from the same scan answers 200
    with the run it already created, finishing that start if it died part way."""
    async with _start_errors(session):
        run = await migrations.lock_scan_claim(session, store.scan_claim(payload.scan_token))
        if run is None:
            scan = store.load_scan(payload.scan_token, owner=("user", ctx.user.id))
            plan = await _prepare(scan)
            errors: dict[str, str] = {}
            company_name = _company_name(payload.company_name, errors)
            if errors:
                raise HTTPException(status_code=422, detail=errors)
            awaiting = await _turn_on_modules(plan)
            run = await _stage(session, user=ctx.user, company_name=company_name, scan=scan, plan=plan,
                               awaiting=awaiting)
        elif run.created_by_user_id == ctx.user.id:
            response.status_code = 200
            awaiting = bool(run.source_summary.get("awaiting_modules"))
        else:
            raise migrations.MigrationError(409, migrations.SCAN_ALREADY_STARTED)
        run_id = run.id
        await session.commit()
    await _claim(session, run_id, payload.scan_token, awaiting=awaiting)
    return {"run_id": str(run_id), "preparing": awaiting}


# ── A login with no company left ─────────────────────────────────────────────
# After its last company was reset, a login moves its books in from another system. The
# upload and the start each check its email and password; the steps between them hold
# only the scan token, as in bootstrap. The migration's staged company becomes its company.

START_COMPANY = "start_company"
_basic = HTTPBasic(auto_error=False)


def _start_company_owner(scan_token: str) -> store.ScanOwner:
    owner = store.scan_owner(scan_token)
    if owner[0] != START_COMPANY:
        raise store.ScanStoreError(410, store.EXPIRED)
    return owner


@router.post("/start-company/scan")
@limiter.limit("5/minute")
async def start_company_scan(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic),
                             session: AsyncSession = Depends(get_session)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail=t("auth.invalid_credentials"))
    user_id = (await companyless_login(session, credentials.username, credentials.password)).id
    await session.rollback()  # nothing is held while the upload streams in
    scan = await store.create_scan(_upload_parts(request), owner=(START_COMPANY, user_id))
    return {"scan_token": scan.token, "scan": migrations.scan_view(scan)}


@router.post("/start-company/scan/read")
async def start_company_scan_read(payload: ScanTokenIn) -> dict:
    owner = _start_company_owner(payload.scan_token)
    return {"scan": migrations.scan_view(store.load_scan(payload.scan_token, owner=owner))}


@router.post("/start-company/decisions")
async def start_company_decisions(payload: DecisionsIn) -> dict:
    return await _save_decisions(_start_company_owner(payload.scan_token), payload)


@router.post("/start-company/start", status_code=201)
@limiter.limit("5/minute")
async def start_company_start(request: Request, payload: StartCompanyStartIn,
                              session: AsyncSession = Depends(get_session)) -> dict:
    """Start a migration as the company of a login that has none left, then sign it in to
    that company. The login is held until the commit, so of two starts one creates the
    company and the other is told the login already has one; a start whose answer was
    lost is the same, and signing in lands on the company being moved in."""
    async with _start_errors(session):
        user = await companyless_login(session, payload.email, payload.password)
        await hold_direct_slot(session)
        if not await hold_companyless_login(session, user.id):
            raise HTTPException(status_code=409, detail=t(HAS_COMPANY))
        if await migrations.lock_scan_claim(session, store.scan_claim(payload.scan_token)) is not None:
            raise migrations.MigrationError(409, migrations.SCAN_ALREADY_STARTED)
        scan = store.load_scan(payload.scan_token, owner=(START_COMPANY, user.id))
        plan = await _prepare(scan)
        errors: dict[str, str] = {}
        company_name = _company_name(payload.company_name, errors)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        awaiting = await _turn_on_modules(plan)
        run = await _stage(session, user=user, company_name=company_name, scan=scan, plan=plan, awaiting=awaiting)
        run_id = run.id
        tokens = await issue_token_pair(session, user=user, company_id=run.company_id)
    await _claim(session, run_id, payload.scan_token, awaiting=awaiting)
    return {**tokens, "run_id": str(run_id), "preparing": awaiting}


# ── Runs ─────────────────────────────────────────────────────────────────────

async def _run_view(session: AsyncSession, run_id: uuid.UUID, ctx: AuthContext) -> dict:
    run = await _owned_run(session, run_id, ctx)
    await migrations.mark_stale_runs_interrupted(session, run.company_id)
    await session.refresh(run)
    return await migrations.run_view(session, run)


@router.get("/staged")
async def get_staged_run(ctx: AuthContext = Depends(get_auth_context),
                         session: AsyncSession = Depends(get_session)) -> dict:
    """The latest run of the staged company this session is scoped to: where the session belongs."""
    run_id = None
    if ctx.company.is_migration_staged:
        run_id = await session.scalar(select(MigrationRun.id).where(MigrationRun.company_id == ctx.company_id)
                                      .order_by(MigrationRun.created_at.desc()).limit(1))
    if run_id is None:
        raise migrations.MigrationError(404, migrations.NOT_FOUND)
    return await _run_view(session, run_id, ctx)


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                  session: AsyncSession = Depends(get_session)) -> dict:
    return await _run_view(session, run_id, ctx)


@router.get("/{run_id}/reconciliation")
async def get_reconciliation(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                             session: AsyncSession = Depends(get_session)) -> dict:
    run = await _owned_run(session, run_id, ctx)
    if not run.reconciliation:
        raise HTTPException(status_code=409, detail="Verification has not run yet.")
    return run.reconciliation


@router.get("/{run_id}/reconciliation/pack")
async def get_reconciliation_pack(run_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                                  session: AsyncSession = Depends(get_session)) -> Response:
    run = await _owned_run(session, run_id, ctx)
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
