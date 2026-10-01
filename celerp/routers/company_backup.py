# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company backup routes.

An owner downloads a backup of the company their session is on. Restoring is two
steps: read (upload, check, preview what restoring does) and restore (do what the
preview showed). A signed-in owner restores a backup as an additional company, or opens
the company it was already restored as, and is switched to it; when that company was
deactivated, its owner reactivates it instead of restoring a copy. A fresh
installation with no user yet restores one through the bootstrap routes, which create
the first owner, are gated by the setup code where one is configured, and close once
any user exists. A login left with no company, after its last company was reset,
restores one through the start-company routes with its email and password.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from celerp.db import get_session
from celerp.models.company import User
from celerp.models.migration import MigrationRun, MigrationStatus
from celerp.modules import requirements
from celerp.modules.importer import MAX_ARCHIVE_BYTES, ModuleImportError, install_from_zip
from celerp.routers.auth import authenticate, hold_direct_slot, limiter
from celerp.routers.migrations import ensure_not_bootstrapped, owner_account, user_owner
from celerp.services import bootstrap
from celerp.services import company_backup as cb
from celerp.services import company_backup_files as files
from celerp.services.auth import HAS_COMPANY, AuthContext, first_usable_company_link, issue_token_pair

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company-backups", tags=["company-backups"])

UPLOAD_AGAIN = "This upload is no longer available. Choose the file again."
RUN_NOT_FOUND = "Migration not found."
RUN_NOT_READY = "This migration has not finished, so its company cannot be backed up yet."
MODULES_UNAVAILABLE = ("This company backup needs modules that cannot run here: {labels}. "
                       "Import them, or choose another backup. Nothing was restored.")
CONSENT_NEEDED = ("These modules come from outside Celerp. Confirm that you want to turn them on: {labels}. "
                  "Nothing was restored.")
MODULES_NOT_SAVED = "Celerp could not turn on the modules this backup needs. Nothing was restored."
MODULE_TOO_LARGE = "This module file is too large (limit 50 MB)."
_BOOTSTRAP = "bootstrap"
_START_COMPANY = "start_company"


def _error(exc: cb.BackupError, stage: Path | None = None) -> JSONResponse:
    """The refusal. Unless the owner can put it right and go on, the upload is deleted."""
    if stage is not None and exc.code is None:
        files.discard_stage(stage)
    content: dict = {"detail": exc.detail}
    if exc.code is not None:
        content["code"] = exc.code
    if isinstance(exc, cb.StalePreview):
        content["plan"] = exc.plan.public()
    if isinstance(exc, cb.ModulesRequired):
        content["modules"] = exc.modules.public()
    return JSONResponse(status_code=exc.status_code, content=content)


# ── Download ─────────────────────────────────────────────────────────────────

async def _provenance(session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> dict:
    """Where a backup made at the end of a migration came from."""
    run = await session.get(MigrationRun, run_id)
    if run is None or run.company_id != ctx.company_id or run.created_by_user_id != ctx.user.id:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    if run.status != MigrationStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=RUN_NOT_READY)
    return {"prepared_by": run.prepared_by, "source_system": run.source_system}


@router.get("/download")
async def download_backup(run_id: uuid.UUID | None = None, ctx: AuthContext = Depends(user_owner),
                          session: AsyncSession = Depends(get_session)):
    """Back up the company the session is on and serve the file, deleted once sent."""
    provenance = await _provenance(session, ctx, run_id) if run_id is not None else None
    dest = files.export_path(ctx.company_id)
    try:
        await cb.export_company_snapshot(ctx.company_id, dest, provenance=provenance)
    except cb.BackupError as exc:
        return _error(exc)
    return FileResponse(dest, media_type="application/octet-stream", filename=f"{ctx.company.slug}{cb.EXTENSION}",
                        background=BackgroundTask(dest.unlink, missing_ok=True))


# ── Restore ──────────────────────────────────────────────────────────────────

def _stage(owner: str, token: str) -> Path:
    path = files.stage_path(owner, token)
    if path is None:
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
    return path


def _uploaded(owner: str, token: str) -> Path:
    path = _stage(owner, token)
    if not path.is_file():
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
    return path


async def _preview(session: AsyncSession, stage: Path, token: str, mode: str, user_id=None,
                   current_company_id=None) -> dict:
    """The staged backup checked, with what restoring it does: for a login the
    plan for restoring it in ``mode``, refused when it was already restored as a company
    they may not open. A backup whose modules are not ready is previewed with what each
    needs; its records are checked once they are."""
    backup = await asyncio.to_thread(cb.read_backup, stage)
    try:
        await cb.check_backup(session, backup)
    except cb.ModulesRequired:
        pass
    if user_id is None:
        plan = cb.plan_bootstrap_restore(backup)
    else:
        plan = await cb.plan_existing_restore(session, backup, mode, user_id, current_company_id)
        if plan.action == cb.REFUSE:
            raise cb.BackupError(409, cb.NOT_A_MEMBER)
    return {"upload_token": token, "file_name": files.stage_facts(stage).get("file_name", ""),
            **backup.summary(), **plan.public()}


async def _read(file: UploadFile, owner: str, session: AsyncSession, mode: str = _BOOTSTRAP,
                user_id=None, current_company_id=None) -> dict:
    """Stage an uploaded backup privately and return its preview with the upload token."""
    await asyncio.to_thread(files.purge_expired_uploads)
    token = secrets.token_hex(16)
    stage = _stage(owner, token)
    try:
        try:
            await asyncio.to_thread(files.stage_upload, file.file, stage, limit=cb.MAX_UPLOAD_BYTES, owner=owner,
                                    mode=mode, file_name=file.filename)
        except files.UploadTooLarge:
            raise cb.BackupError(413, cb.TOO_LARGE_UPLOAD) from None
        return await _preview(session, stage, token, mode, user_id, current_company_id)
    except BaseException:
        files.discard_stage(stage)
        raise


async def _staged(session: AsyncSession, owner: str, token: str, ctx: AuthContext | None, mode: str):
    """The preview of an upload already staged, checked again."""
    stage = _uploaded(owner, token)
    user_id, company_id = (ctx.user.id, ctx.company_id) if ctx is not None else (None, None)
    try:
        return await _preview(session, stage, token, mode, user_id, company_id)
    except cb.BackupError as exc:
        return _error(exc, stage)


async def _prepare(owner: str, token: str, consent: list[str]) -> JSONResponse:
    """Turn on the modules a staged backup needs, then restart Celerp to load them. Bundled
    modules are turned on; one from outside Celerp only when the owner named it."""
    stage = _uploaded(owner, token)
    try:
        backup = await asyncio.to_thread(cb.read_backup, stage)
    except cb.BackupError as exc:
        return _error(exc, stage)
    modules = cb.requirement_plan(backup)
    try:
        restart = await asyncio.to_thread(requirements.prepare, modules, consent=frozenset(consent))
    except requirements.RequirementsBlocked as exc:
        return JSONResponse(status_code=409, content={"detail": MODULES_UNAVAILABLE.format(labels=exc),
                                                      "code": "modules_unavailable"})
    except requirements.ConsentRequired as exc:
        return JSONResponse(status_code=409, content={"detail": CONSENT_NEEDED.format(labels=exc),
                                                      "code": "consent_required"})
    except OSError:
        logger.exception("Could not turn on the modules a company backup needs")
        raise HTTPException(status_code=503, detail=MODULES_NOT_SAVED) from None
    if not restart:
        return JSONResponse(status_code=200, content={"restart": False, "restarting": False})
    return JSONResponse(status_code=202, content={"restart": True, "restarting": requirements.schedule_restart()})


async def _import_module(file: UploadFile, session: AsyncSession, owner: str, token: str,
                         ctx: AuthContext | None, mode: str):
    """Install a module the staged backup needs through the module importer, as the Modules
    page does; it lands turned off. Returns the staged backup's preview, checked again."""
    _uploaded(owner, token)
    data = await file.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail=MODULE_TOO_LARGE)
    try:
        await asyncio.to_thread(install_from_zip, data)
    except ModuleImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _staged(session, owner, token, ctx, mode)


async def _tokens(session: AsyncSession, user_id, company_id) -> dict:
    """A session for the user on the company, with their active role there."""
    user = await session.get(User, uuid.UUID(str(user_id)))
    return await issue_token_pair(session, user=user, company_id=uuid.UUID(str(company_id)))


async def _signed_in(session: AsyncSession, result: cb.RestoreResult) -> JSONResponse:
    """The restore response, with a session for the company it opened."""
    tokens = await _tokens(session, result.user_id, result.company_id)
    return JSONResponse(status_code=201 if result.outcome == cb.CREATED else 200, content={
        "company_id": result.company_id, "company_name": result.company_name, "outcome": result.outcome,
        "backup_created_at": result.backup_created_at, "team_members": result.team_members,
        "role_permissions": result.role_permissions, **tokens})


RestoreMode = Literal["settings", "new_company"]


class UploadIn(BaseModel):
    upload_token: str


class PrepareIn(UploadIn):
    consent: list[str] = []


class RestoreIn(UploadIn):
    mode: RestoreMode
    plan_fingerprint: str
    company_name: str | None = None


@router.post("/read")
async def read_backup(file: UploadFile = File(...), mode: RestoreMode = Form("settings"),
                      ctx: AuthContext = Depends(user_owner), session: AsyncSession = Depends(get_session)):
    try:
        return await _read(file, str(ctx.user.id), session, mode, ctx.user.id, ctx.company_id)
    except cb.BackupError as exc:
        return _error(exc)


@router.get("/staged")
async def staged_backup(upload_token: str, mode: RestoreMode = "settings", ctx: AuthContext = Depends(user_owner),
                        session: AsyncSession = Depends(get_session)):
    return await _staged(session, str(ctx.user.id), upload_token, ctx, mode)


@router.post("/prepare")
async def prepare_modules(payload: PrepareIn, ctx: AuthContext = Depends(user_owner)):
    return await _prepare(str(ctx.user.id), payload.upload_token, payload.consent)


@router.post("/import-module")
async def import_module(file: UploadFile = File(...), upload_token: str = Form(...),
                        mode: RestoreMode = Form("settings"), ctx: AuthContext = Depends(user_owner),
                        session: AsyncSession = Depends(get_session)):
    return await _import_module(file, session, str(ctx.user.id), upload_token, ctx, mode)


@router.post("/discard", status_code=204)
async def discard_upload(payload: UploadIn, ctx: AuthContext = Depends(user_owner)) -> Response:
    """Delete the caller's staged upload; anything else is left alone."""
    stage = files.stage_path(str(ctx.user.id), payload.upload_token)
    if stage is not None:
        files.discard_stage(stage)
    return Response(status_code=204)


@router.post("/restore")
async def restore_backup(payload: RestoreIn, ctx: AuthContext = Depends(user_owner),
                         session: AsyncSession = Depends(get_session)):
    """Restore a staged backup as the preview showed it and switch the caller to the company:
    a new one, or the one it was already restored as. Repeating a restore that finished,
    when its response was lost, opens the company it made."""
    stage = _stage(str(ctx.user.id), payload.upload_token)
    if not stage.is_file():
        result = await cb.reopen_restored(files.restored_company(stage), user_id=ctx.user.id)
        if result is None:
            raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
        return await _signed_in(session, result)
    try:
        result = await cb.restore_company(stage, mode=payload.mode, user_id=ctx.user.id,
                                          current_company_id=ctx.company_id, plan_fingerprint=payload.plan_fingerprint,
                                          company_name=payload.company_name)
    except cb.BackupError as exc:
        return _error(exc, stage)
    files.finish_stage(stage, result.company_id)
    return await _signed_in(session, result)


@router.post("/reactivate")
async def reactivate_restored(payload: RestoreIn, ctx: AuthContext = Depends(user_owner),
                              session: AsyncSession = Depends(get_session)):
    """Reactivate the deactivated company a staged backup was already restored as, instead
    of restoring a copy, and switch the caller to it. Connectors its deactivation
    disconnected are named, not reconnected."""
    stage = _stage(str(ctx.user.id), payload.upload_token)
    if not stage.is_file():
        reopened = await cb.reopen_restored(files.restored_company(stage), user_id=ctx.user.id)
        if reopened is None:
            raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
        company_id, name, outcome, reconnect = reopened.company_id, reopened.company_name, reopened.outcome, []
    else:
        try:
            done = await cb.reactivate_restored(stage, mode=payload.mode, user_id=ctx.user.id,
                                                current_company_id=ctx.company_id,
                                                plan_fingerprint=payload.plan_fingerprint)
        except cb.BackupError as exc:
            return _error(exc, stage)
        files.finish_stage(stage, done.company_id)
        company_id, name, reconnect = done.company_id, done.company_name, done.connectors_to_reconnect
        outcome = cb.REACTIVATED if done.reactivated else cb.OPENED_EXISTING
    tokens = await _tokens(session, ctx.user.id, company_id)
    return {"company_id": str(company_id), "company_name": name, "outcome": outcome,
            "connectors_to_reconnect": reconnect, **tokens}


async def _first_run(session: AsyncSession, x_setup_code: str | None) -> None:
    await ensure_not_bootstrapped(session)
    bootstrap.verify_setup_code(x_setup_code)


@router.post("/bootstrap/read")
@limiter.limit("5/minute")
async def bootstrap_read(request: Request, file: UploadFile = File(...),
                         session: AsyncSession = Depends(get_session), x_setup_code: str | None = Header(None)):
    await _first_run(session, x_setup_code)
    try:
        return await _read(file, _BOOTSTRAP, session)
    except cb.BackupError as exc:
        return _error(exc)


@router.get("/bootstrap/staged")
async def bootstrap_staged(upload_token: str, session: AsyncSession = Depends(get_session)):
    """The upload token was issued against the setup code and names one private upload,
    so showing or deleting that upload needs nothing more."""
    await ensure_not_bootstrapped(session)
    return await _staged(session, _BOOTSTRAP, upload_token, None, _BOOTSTRAP)


@router.post("/bootstrap/prepare")
async def bootstrap_prepare(payload: PrepareIn, session: AsyncSession = Depends(get_session),
                            x_setup_code: str | None = Header(None)):
    await _first_run(session, x_setup_code)
    return await _prepare(_BOOTSTRAP, payload.upload_token, payload.consent)


@router.post("/bootstrap/import-module")
@limiter.limit("5/minute")
async def bootstrap_import_module(request: Request, file: UploadFile = File(...), upload_token: str = Form(...),
                                  session: AsyncSession = Depends(get_session),
                                  x_setup_code: str | None = Header(None)):
    await _first_run(session, x_setup_code)
    return await _import_module(file, session, _BOOTSTRAP, upload_token, None, _BOOTSTRAP)


@router.post("/bootstrap/discard", status_code=204)
async def bootstrap_discard(payload: UploadIn, session: AsyncSession = Depends(get_session)) -> Response:
    await ensure_not_bootstrapped(session)
    stage = files.stage_path(_BOOTSTRAP, payload.upload_token)
    if stage is not None:
        files.discard_stage(stage)
    return Response(status_code=204)


class BootstrapRestoreIn(BaseModel):
    upload_token: str
    name: str
    email: str
    password: str
    company_name: str | None = None


@router.post("/bootstrap/restore")
@limiter.limit("5/minute")
async def bootstrap_restore(request: Request, payload: BootstrapRestoreIn, session: AsyncSession = Depends(get_session),
                            x_setup_code: str | None = Header(None)):
    """Create the first owner and restore the backup as their company in one commit, then
    sign them in. Repeating it with the same account opens the same company."""
    required = bootstrap.verify_setup_code(x_setup_code)
    errors: dict[str, str] = {}
    name, email = owner_account(payload.name, payload.email, payload.password, errors)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    account = {"name": name, "email": email, "password": payload.password}
    stage = _stage(_BOOTSTRAP, payload.upload_token)
    if not stage.is_file():
        result = await cb.reopen_restored(files.restored_company(stage), owner_account=account)
        if result is None:
            raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
        return await _signed_in(session, result)
    try:
        result = await cb.restore_company(stage, mode="bootstrap", owner_account=account,
                                          company_name=payload.company_name)
    except cb.BackupError as exc:
        return _error(exc, stage)
    files.finish_stage(stage, result.company_id)
    response = await _signed_in(session, result)
    if required:
        try:
            await asyncio.to_thread(bootstrap.clear_setup_code)
        except Exception:
            logger.warning("Setup-code cleanup failed after restoring a company backup", exc_info=True)
    return response


async def _companyless(session: AsyncSession, email: str, password: str) -> User:
    """The login these credentials belong to, when it has no company left."""
    user = await authenticate(session, email, password)
    if await first_usable_company_link(session, user.id) is not None:
        raise HTTPException(status_code=409, detail=HAS_COMPANY)
    return user


@router.post("/start-company/read")
@limiter.limit("5/minute")
async def start_company_read(request: Request, file: UploadFile = File(...), email: str = Form(...),
                             password: str = Form(...), session: AsyncSession = Depends(get_session)):
    user = await _companyless(session, email, password)
    try:
        return await _read(file, str(user.id), session, _START_COMPANY, user.id)
    except cb.BackupError as exc:
        return _error(exc)


class StartCompanyRestoreIn(BaseModel):
    email: str
    password: str
    upload_token: str
    plan_fingerprint: str
    company_name: str | None = None


@router.post("/start-company/restore")
@limiter.limit("5/minute")
async def start_company_restore(request: Request, payload: StartCompanyRestoreIn,
                                session: AsyncSession = Depends(get_session)):
    """Restore a staged backup as the company of a login that has none left, as the preview
    showed it, and sign it in to that company."""
    user = await _companyless(session, payload.email, payload.password)
    stage = _uploaded(str(user.id), payload.upload_token)
    await hold_direct_slot(session)
    try:
        result = await cb.restore_company(stage, mode=_START_COMPANY, user_id=user.id,
                                          plan_fingerprint=payload.plan_fingerprint,
                                          company_name=payload.company_name)
    except cb.BackupError as exc:
        return _error(exc, stage)
    files.finish_stage(stage, result.company_id)
    return await _signed_in(session, result)
