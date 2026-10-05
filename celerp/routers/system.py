# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""System administration endpoints: restart, factory reset and updates."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy import Delete, String, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.held_back import held_back
from celerp.models.company import User
from celerp.services.auth import (
    get_current_company_id, get_current_user, is_install_owner, require_install_owner,
)
from celerp.services.permissions import require_permission

_ATTACHMENT_ROOT = Path("static/attachments")

router = APIRouter(dependencies=[require_permission("manage_company_settings")])


# ── Graceful restart ──────────────────────────────────────────────────────────


def _restart_sentinel_path() -> Path:
    from celerp.config import config_path
    return config_path().parent / ".restart_requested"


def _send_sigterm() -> None:
    """Write restart sentinel then SIGTERM self.

    The sentinel tells the `celerp start` process manager to respawn rather
    than exit when it detects the subprocess death.
    """
    import os
    import signal
    import time
    _restart_sentinel_path().touch()
    time.sleep(0.2)  # let the response flush
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def restart_server(
    background_tasks: BackgroundTasks,
) -> dict:
    """Gracefully restart the server process (SIGTERM → process manager respawns).

    Used by the setup wizard after applying a preset so new modules are loaded.
    Returns immediately; the restart happens ~200ms later in a background task.
    """
    background_tasks.add_task(_send_sigterm)
    return {"ok": True, "restarting": True}


# ── Last start ───────────────────────────────────────────────────────────────


@router.get("/start-report")
async def start_report(request: Request) -> dict:
    """What Doctor shows first: why the last start held the records back (each failed
    step with its error), or nothing when the records are current."""
    cause = held_back(request.app)
    return {"held_back": cause.report() if cause is not None else None}


# ── Factory reset ─────────────────────────────────────────────────────────────

class FactoryReset(BaseModel):
    confirm_name: str = ""


def _company_tables() -> list:
    """Every table holding a company's rows, children before parents. Rows without a
    company column (conversation messages, migration entity maps, a user's sessions)
    go with their parent row by ON DELETE CASCADE."""
    from celerp.models.base import Base

    return [t for t in reversed(Base.metadata.sorted_tables)
            if "company_id" in t.c and t.name != "companies"]


def _users_left_without_a_company(members: list) -> Delete:
    """Of ``members``, the users no company has any more and no remaining row points to."""
    from celerp.models.base import Base

    users = User.__table__
    refs = [fk.parent for t in Base.metadata.sorted_tables for fk in t.foreign_keys
            if fk.column.table is users and fk.ondelete != "CASCADE"]
    return users.delete().where(users.c.id.in_(members), *[~exists().where(col == users.c.id) for col in refs])


@router.post("/factory-reset")
async def factory_reset(
    body: FactoryReset | None = None,
    _: None = require_permission("manage_company_lifecycle"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete the signed-in company and every record it holds, once its owner has typed
    the company's exact name. Other companies, and every user one of them still has,
    are untouched. One transaction: a failure part way leaves everything as it was."""
    from celerp.accounting_roles import refusal
    from celerp.connectors.ownership import lock_connector_maintenance
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    if company is None or (body.confirm_name if body else "") != company.name:
        raise HTTPException(status_code=422, detail=refusal(
            "system.factory_reset.name_mismatch",
            "Type the company name exactly as shown to reset this company."))
    # Signing in has already read on this session, so the wipe runs in the request's own
    # transaction and is committed in one step.
    await lock_connector_maintenance(session)
    members = list((await session.execute(
        select(UserCompany.user_id).where(UserCompany.company_id == company_id))).scalars())
    for table in _company_tables():
        column = table.c.company_id  # a few connector tables keep it as text
        await session.execute(table.delete().where(
            column == (str(company_id) if isinstance(column.type, String) else company_id)))
    await session.execute(Company.__table__.delete().where(Company.__table__.c.id == company_id))
    await session.execute(_users_left_without_a_company(members))
    await session.commit()

    # Bust the in-process nonce cache: a deleted user's stale token must not auto-create rows
    from celerp.services.session_tracker import _nonce_cache_bust_all
    _nonce_cache_bust_all()

    att_dir = _ATTACHMENT_ROOT / str(company_id)
    if att_dir.exists():
        import shutil
        shutil.rmtree(att_dir, ignore_errors=True)

    return {"ok": True}


# ── Updates ───────────────────────────────────────────────────────────────────
# Installation-wide, so gated on the install owner rather than the company
# permission the router above carries; reading the status needs only a login.

update_router = APIRouter()


@update_router.get("/update")
async def update_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The update card's state; see celerp.services.update.status."""
    from celerp.services import update
    return update.status(owner=await is_install_owner(session, user.id))


@update_router.post("/update", status_code=202, dependencies=[Depends(require_install_owner)])
async def install_update(background_tasks: BackgroundTasks) -> dict:
    """Install the newest version pip offers. The version is chosen here, never
    by the caller; Celerp restarts and is back in about a minute."""
    from celerp.services import update
    try:
        target = await asyncio.to_thread(update.request_available)
    except update.UpdateRefused as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    background_tasks.add_task(_send_sigterm)
    return {"ok": True, "installing": target}


@update_router.post("/update/check", dependencies=[Depends(require_install_owner)])
async def check_for_update() -> dict:
    from celerp.services import update
    await asyncio.to_thread(update.refresh_check)
    return update.status(owner=True)


class UpdateSettings(BaseModel):
    auto: StrictBool


@update_router.patch("/update/settings", dependencies=[Depends(require_install_owner)])
async def update_settings(body: UpdateSettings) -> dict:
    """Turn automatic overnight updates on or off."""
    from celerp.services import update
    update.set_auto(body.auto)
    return {"auto": body.auto}
