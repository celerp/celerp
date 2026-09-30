# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company backup routes.

An owner downloads a backup of the company their session is on. Restoring is two
steps: read (upload, check, preview) and restore (create the new company). A signed-in
owner restores a backup as an additional company and is switched to it; a fresh
installation with no user yet restores one through the bootstrap routes, which create
the first owner, are gated by the setup code where one is configured, and close once
any user exists.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.migration import MigrationRun, MigrationStatus
from celerp.routers.auth import limiter
from celerp.routers.migrations import ensure_not_bootstrapped, owner_account, user_owner
from celerp.services import bootstrap
from celerp.services import company_backup as cb
from celerp.services.auth import AuthContext, issue_token_pair

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company-backups", tags=["company-backups"])

UPLOAD_AGAIN = "This upload is no longer available. Choose the file again."
RUN_NOT_FOUND = "Migration not found."
RUN_NOT_READY = "This migration has not finished, so its company cannot be backed up yet."
_BOOTSTRAP = "bootstrap"
_KEEP_SECONDS = 24 * 3600
_CHUNK = 1024 * 1024


def _root() -> Path:
    from celerp.config import settings
    return settings.data_dir / "company_backups"


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
    _purge(_root())
    dest = _root() / str(ctx.company_id) / f"{uuid.uuid4().hex}{cb.EXTENSION}"
    try:
        await cb.export_company(session, ctx.company_id, dest, provenance=provenance)
    except cb.BackupError as exc:
        return _error(exc)
    return FileResponse(dest, media_type="application/octet-stream", filename=f"{ctx.company.slug}{cb.EXTENSION}")


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


async def _read(file: UploadFile, owner: str, session: AsyncSession, ctx: AuthContext | None = None) -> dict:
    """Stage an uploaded backup, check it, and return its preview with the upload token and,
    for a signed-in owner, how many of the current company's team a Settings restore gives access."""
    _purge(_root() / "uploads")
    token = secrets.token_hex(16)
    path = _staged(owner, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(_save, file.file, path)
        backup = await asyncio.to_thread(cb.read_backup, path)
        await cb.check_backup(session, backup)
        team = 0 if ctx is None else await cb.team_members(session, backup, current_company_id=ctx.company_id,
                                                            user_id=ctx.user.id)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"upload_token": token, **backup.summary(), "team_members": team}


async def _signed_in(session: AsyncSession, result: cb.RestoreResult) -> dict:
    """The restore response, with a session for the restored company."""
    user = await session.get(User, uuid.UUID(result.user_id))
    company = await session.get(Company, uuid.UUID(result.company_id))
    role = await session.scalar(select(UserCompany.role).where(
        UserCompany.user_id == user.id, UserCompany.company_id == company.id, UserCompany.is_active.is_(True)))
    tokens = await issue_token_pair(session, user=user, company=company, role=role)
    return {"company_id": result.company_id, "company_name": result.company_name, "created": result.created,
            "backup_created_at": result.backup_created_at, "team_members": result.team_members, **tokens}


class RestoreIn(BaseModel):
    upload_token: str
    mode: Literal["settings", "new_company"]


@router.post("/read")
async def read_backup(file: UploadFile = File(...), ctx: AuthContext = Depends(user_owner),
                      session: AsyncSession = Depends(get_session)):
    try:
        return await _read(file, str(ctx.user.id), session, ctx)
    except cb.BackupError as exc:
        return _error(exc)


@router.post("/restore")
async def restore_backup(payload: RestoreIn, ctx: AuthContext = Depends(user_owner),
                         session: AsyncSession = Depends(get_session)):
    """Restore a staged backup as a new company for the caller and switch them to it."""
    path = _uploaded(str(ctx.user.id), payload.upload_token)
    try:
        result = await cb.restore_company(path, mode=payload.mode, user_id=ctx.user.id,
                                          current_company_id=ctx.company_id)
    except cb.BackupError as exc:
        return _error(exc)
    return JSONResponse(status_code=201 if result.created else 200, content=await _signed_in(session, result))


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
