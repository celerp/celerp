# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Independent company copy routes.

An owner makes a copy of the company their session is on and downloads it. Opening a
copy is two steps: read (upload, check, preview) and open (create the new company).
A signed-in owner opens a copy as an additional company; a fresh installation with no
user yet opens one through the bootstrap routes, which create the first owner, are
gated by the setup code where one is configured, and close once any user exists.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.routers.auth import limiter
from celerp.routers.migrations import ensure_not_bootstrapped, user_owner
from celerp.services import bootstrap
from celerp.services import company_copy as cc
from celerp.services.auth import MIN_PASSWORD_LENGTH, AuthContext, issue_token_pair, validate_password
from celerp.services.provisioning import create_install_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/company-copies", tags=["company-copies"])

UPLOAD_AGAIN = "This upload is no longer available. Choose the file again."
_BOOTSTRAP = "bootstrap"
_KEEP_SECONDS = 24 * 3600


def _root() -> Path:
    from celerp.config import settings
    return settings.data_dir / "company_copies"


def _purge(folder: Path) -> None:
    """Delete copy files and uploads older than a day."""
    if not folder.is_dir():
        return
    cutoff = time.time() - _KEEP_SECONDS
    for f in folder.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def _error(exc: cc.CopyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ── Make and download a copy ─────────────────────────────────────────────────

class CopyIn(BaseModel):
    prepared_by: str | None = None


@router.post("", status_code=201)
async def create_copy(payload: CopyIn, ctx: AuthContext = Depends(user_owner),
                      session: AsyncSession = Depends(get_session)):
    """Copy the company the session is on."""
    _purge(_root())
    copy_id = uuid.uuid4().hex
    dest = _root() / str(ctx.company_id) / f"{copy_id}{cc.SUFFIX}"
    try:
        manifest = await cc.export_company(session, ctx.company_id, dest, prepared_by=payload.prepared_by)
    except cc.CopyError as exc:
        return _error(exc)
    return {"copy_id": copy_id, "company_name": manifest["company"]["name"], "handoff_id": manifest["handoff_id"]}


@router.get("/{copy_id}/download")
async def download_copy(copy_id: str, ctx: AuthContext = Depends(user_owner)):
    """Serve a copy of the session's own company; any other id is not found."""
    path = _root() / str(ctx.company_id) / f"{copy_id}{cc.SUFFIX}"
    if not copy_id.isalnum() or not path.is_file():
        raise HTTPException(status_code=404, detail="Company copy not found. Make a new copy.")
    return FileResponse(path, media_type="application/octet-stream", filename=f"{ctx.company.slug}{cc.SUFFIX}")


# ── Open a copy ──────────────────────────────────────────────────────────────

def _staged(owner: str, token: str) -> Path:
    if not token.isalnum():
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN)
    return _root() / "uploads" / f"{owner}-{token}{cc.SUFFIX}"


async def _read(file: UploadFile, owner: str, session: AsyncSession) -> dict:
    """Stage an uploaded copy, check it, and return its preview with the upload token."""
    _purge(_root() / "uploads")
    token = secrets.token_hex(16)
    path = _staged(owner, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, file.file, out)
        copy = await asyncio.to_thread(cc.read_copy, path)
        await cc.check_schema(session, copy)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"upload_token": token, **copy.summary()}


def _claim(owner: str, token: str) -> Path:
    """Take a staged upload for one open; a second open of the same upload is refused."""
    path = _staged(owner, token)
    claimed = path.with_suffix(".opening")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail=UPLOAD_AGAIN) from None
    return claimed


class OpenIn(BaseModel):
    upload_token: str


@router.post("/read")
async def read_copy(file: UploadFile = File(...), ctx: AuthContext = Depends(user_owner),
                    session: AsyncSession = Depends(get_session)):
    try:
        return await _read(file, str(ctx.user.id), session)
    except cc.CopyError as exc:
        return _error(exc)


@router.post("/open", status_code=201)
async def open_copy(payload: OpenIn, ctx: AuthContext = Depends(user_owner),
                    session: AsyncSession = Depends(get_session)):
    """Open a staged copy as a new company owned by the caller; their session stays where it is."""
    path = _claim(str(ctx.user.id), payload.upload_token)
    try:
        copy = await asyncio.to_thread(cc.read_copy, path)
        company = await cc.open_copy(session, copy, owner=ctx.user)
    except cc.CopyError as exc:
        return _error(exc)
    finally:
        path.unlink(missing_ok=True)
    return {"company_id": str(company.id), "company_name": company.name}


@router.post("/bootstrap/read")
@limiter.limit("5/minute")
async def bootstrap_read(request: Request, file: UploadFile = File(...),
                         session: AsyncSession = Depends(get_session), x_setup_code: str | None = Header(None)):
    await ensure_not_bootstrapped(session)
    bootstrap.verify_setup_code(x_setup_code)
    try:
        return await _read(file, _BOOTSTRAP, session)
    except cc.CopyError as exc:
        return _error(exc)


class BootstrapOpenIn(OpenIn):
    name: str
    email: str
    password: str


@router.post("/bootstrap/open", status_code=201)
@limiter.limit("5/minute")
async def bootstrap_open(request: Request, payload: BootstrapOpenIn, session: AsyncSession = Depends(get_session),
                         x_setup_code: str | None = Header(None)):
    """Create the first owner and open the copy as their company in one commit, then sign them in.

    The bootstrap lock serializes racing opens; the loser re-checks and is refused."""
    await ensure_not_bootstrapped(session)
    required = bootstrap.verify_setup_code(x_setup_code)
    errors: dict[str, str] = {}
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
    await bootstrap.lock_bootstrap(session)
    try:
        await ensure_not_bootstrapped(session)
    except HTTPException:
        await session.rollback()
        raise
    path = _claim(_BOOTSTRAP, payload.upload_token)
    try:
        copy = await asyncio.to_thread(cc.read_copy, path)
        user = await create_install_owner(session, name=name, email=email, password=payload.password)
        company = await cc.open_copy(session, copy, owner=user)
    except cc.CopyError as exc:
        return _error(exc)
    finally:
        path.unlink(missing_ok=True)
    tokens = await issue_token_pair(session, user=user, company=company, role="owner")
    if required:
        try:
            await asyncio.to_thread(bootstrap.clear_setup_code)
        except Exception:
            logger.warning("Setup-code cleanup failed after opening a company copy", exc_info=True)
    return {**tokens, "company_id": str(company.id), "company_name": company.name}
