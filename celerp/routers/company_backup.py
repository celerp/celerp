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
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.company import Company, User
from celerp.models.migration import MigrationRun, MigrationStatus
from celerp.routers.auth import authenticate, hold_direct_slot, limiter
from celerp.routers.migrations import ensure_not_bootstrapped, owner_account, user_owner
from celerp.services import bootstrap
from celerp.services import company_backup as cb
from celerp.services.auth import HAS_COMPANY, AuthContext, first_usable_company_link, issue_token_pair
from celerp.services.company_files import company_backups_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company-backups", tags=["company-backups"])

UPLOAD_AGAIN = "This upload is no longer available. Choose the file again."
RUN_NOT_FOUND = "Migration not found."
RUN_NOT_READY = "This migration has not finished, so its company cannot be backed up yet."
_BOOTSTRAP = "bootstrap"
_START_COMPANY = "start_company"
_KEEP_SECONDS = 24 * 3600
_CHUNK = 1024 * 1024


def _root() -> Path:
    return company_backups_dir()


def _purge(folder: Path) -> None:
    """Delete backup files and uploads older than a day."""
    if not folder.is_dir():
        return
    cutoff = time.time() - _KEEP_SECONDS
    for f in folder.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except FileNotFoundError:
            continue  # removed by another request meanwhile


def _error(exc: cb.BackupError) -> JSONResponse:
    if isinstance(exc, cb.StalePreview):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail, "code": "stale_preview", "plan": exc.plan.public()})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


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
    """Back up the company the session is on and serve the file."""
    provenance = await _provenance(session, ctx, run_id) if run_id is not None else None
    filename = f"{ctx.company.slug}{cb.EXTENSION}"
    # The backup reads through a transaction of its own: end this one first, so the
    # download holds one connection while the backup is made, not two.
    await session.rollback()
    _purge(_root())
    dest = _root() / str(ctx.company_id) / f"{uuid.uuid4().hex}{cb.EXTENSION}"
    try:
        await cb.export_company_snapshot(ctx.company_id, dest, provenance=provenance)
    except cb.BackupError as exc:
        return _error(exc)
    return FileResponse(dest, media_type="application/octet-stream", filename=filename)


# ── Restore ──────────────────────────────────────────────────────────────────

def _staged(owner: str, token: str) -> Path:
    if not token.isalnum():
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
    return _root() / "uploads" / f"{owner}-{token}{cb.EXTENSION}"


def _uploaded(owner: str, token: str) -> Path:
    path = _staged(owner, token)
    if not path.is_file():
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
    return path


def _save(source: BinaryIO, dest: Path) -> None:
    """Write an upload to ``dest``, refusing it once it passes the upload limit."""
    size = 0
    with open(dest, "wb") as out:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > cb.MAX_UPLOAD_BYTES:
                raise cb.BackupError(413, cb.TOO_LARGE_UPLOAD)
            out.write(chunk)


async def _read(file: UploadFile, owner: str, session: AsyncSession, mode: str = _BOOTSTRAP,
                user_id=None, current_company_id=None) -> dict:
    """Stage an uploaded backup, check it, and return its preview with the upload token and
    what restoring it does: for a login the plan for restoring it in ``mode``, refused when
    it was already restored as a company they may not open."""
    _purge(_root() / "uploads")
    token = secrets.token_hex(16)
    path = _staged(owner, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(_save, file.file, path)
        backup = await asyncio.to_thread(cb.read_backup, path)
        await cb.check_backup(session, backup)
        if user_id is None:
            plan = {"action": cb.CREATE, "team_members": 0}
        else:
            planned = await cb.plan_existing_restore(session, backup, mode, user_id, current_company_id)
            if planned.action == cb.REFUSE:
                raise cb.BackupError(409, cb.NOT_A_MEMBER)
            plan = planned.public()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"upload_token": token, **backup.summary(), **plan}


async def _tokens(session: AsyncSession, user_id, company_id) -> dict:
    """A session for the user on the company, with their active role there."""
    user = await session.get(User, uuid.UUID(str(user_id)))
    return await issue_token_pair(session, user=user, company_id=uuid.UUID(str(company_id)))


async def _signed_in(session: AsyncSession, result: cb.RestoreResult) -> dict:
    """The restore response, with a session for the restored company."""
    tokens = await _tokens(session, result.user_id, result.company_id)
    return {"company_id": result.company_id, "company_name": result.company_name, "created": result.created,
            "backup_created_at": result.backup_created_at, "team_members": result.team_members, **tokens}


RestoreMode = Literal["settings", "new_company"]


class RestoreIn(BaseModel):
    upload_token: str
    mode: RestoreMode
    plan_fingerprint: str


@router.post("/read")
async def read_backup(file: UploadFile = File(...), mode: RestoreMode = Form("settings"),
                      ctx: AuthContext = Depends(user_owner), session: AsyncSession = Depends(get_session)):
    try:
        return await _read(file, str(ctx.user.id), session, mode, ctx.user.id, ctx.company_id)
    except cb.BackupError as exc:
        return _error(exc)


@router.post("/restore")
async def restore_backup(payload: RestoreIn, ctx: AuthContext = Depends(user_owner),
                         session: AsyncSession = Depends(get_session)):
    """Restore a staged backup as the preview showed it and switch the caller to the company:
    a new one, or the one it was already restored as."""
    path = _uploaded(str(ctx.user.id), payload.upload_token)
    try:
        result = await cb.restore_company(path, mode=payload.mode, user_id=ctx.user.id,
                                          current_company_id=ctx.company_id, plan_fingerprint=payload.plan_fingerprint)
    except cb.BackupError as exc:
        return _error(exc)
    return JSONResponse(status_code=201 if result.created else 200, content=await _signed_in(session, result))


@router.post("/reactivate")
async def reactivate_restored(payload: RestoreIn, ctx: AuthContext = Depends(user_owner),
                              session: AsyncSession = Depends(get_session)):
    """Reactivate the deactivated company a staged backup was already restored as, instead
    of restoring a copy, and switch the caller to it. Connectors its deactivation
    disconnected are named, not reconnected."""
    path = _uploaded(str(ctx.user.id), payload.upload_token)
    try:
        done = await cb.reactivate_restored(path, mode=payload.mode, user_id=ctx.user.id,
                                            current_company_id=ctx.company_id, plan_fingerprint=payload.plan_fingerprint)
    except cb.BackupError as exc:
        return _error(exc)
    tokens = await _tokens(session, ctx.user.id, done.company_id)
    return {"company_id": str(done.company_id), "company_name": done.company_name, "reactivated": done.reactivated,
            "connectors_to_reconnect": done.connectors_to_reconnect, **tokens}


@router.post("/bootstrap/read")
@limiter.limit("5/minute")
async def bootstrap_read(request: Request, file: UploadFile = File(...),
                         session: AsyncSession = Depends(get_session), x_setup_code: str | None = Header(None)):
    await ensure_not_bootstrapped(session)
    bootstrap.verify_setup_code(x_setup_code)
    try:
        return await _read(file, _BOOTSTRAP, session)
    except cb.BackupError as exc:
        return _error(exc)


class BootstrapRestoreIn(BaseModel):
    upload_token: str
    name: str
    email: str
    password: str


@router.post("/bootstrap/restore")
@limiter.limit("5/minute")
async def bootstrap_restore(request: Request, payload: BootstrapRestoreIn, session: AsyncSession = Depends(get_session),
                            x_setup_code: str | None = Header(None)):
    """Create the first owner and restore the backup as their company in one commit, then
    sign them in. Repeating it with the same account returns the same company."""
    required = bootstrap.verify_setup_code(x_setup_code)
    errors: dict[str, str] = {}
    name, email = owner_account(payload.name, payload.email, payload.password, errors)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    path = _uploaded(_BOOTSTRAP, payload.upload_token)
    try:
        result = await cb.restore_company(path, mode="bootstrap",
                                          owner_account={"name": name, "email": email, "password": payload.password})
    except cb.BackupError as exc:
        return _error(exc)
    body = await _signed_in(session, result)
    if required:
        try:
            await asyncio.to_thread(bootstrap.clear_setup_code)
        except Exception:
            logger.warning("Setup-code cleanup failed after restoring a company backup", exc_info=True)
    return JSONResponse(status_code=201 if result.created else 200, content=body)


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


@router.post("/start-company/restore")
@limiter.limit("5/minute")
async def start_company_restore(request: Request, payload: StartCompanyRestoreIn,
                                session: AsyncSession = Depends(get_session)):
    """Restore a staged backup as the company of a login that has none left, as the preview
    showed it, and sign it in to that company."""
    user = await _companyless(session, payload.email, payload.password)
    path = _uploaded(str(user.id), payload.upload_token)
    await hold_direct_slot(session)
    try:
        result = await cb.restore_company(path, mode=_START_COMPANY, user_id=user.id,
                                          plan_fingerprint=payload.plan_fingerprint)
    except cb.BackupError as exc:
        return _error(exc)
    return JSONResponse(status_code=201 if result.created else 200, content=await _signed_in(session, result))
