# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""System administration endpoints: restart, factory reset and updates."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
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


# ── Factory reset ─────────────────────────────────────────────────────────────

_TRUNCATE_TABLES = [
    "ledger", "projections", "notifications", "import_batches", "sync_runs",
    "doc_share_tokens", "ai_conversations", "ai_messages", "ai_batch_jobs",
    "outbound_queue", "connector_configs", "connector_sources",
    "accounts", "bank_accounts", "bank_statement_lines",
    "label_templates", "reconciliation_rules", "reconciliation_sessions",
    "marketplace_configs", "session_registry", "user_auth_state",
]


@router.post("/factory-reset")
async def factory_reset(
    _: None = require_permission("manage_company_lifecycle"),
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Wipe all company data and return the system to a fresh-install state."""
    from sqlalchemy import text
    from celerp.connectors.ownership import lock_connector_maintenance

    async with session.begin():
        await lock_connector_maintenance(session)
        for table in _TRUNCATE_TABLES:
            await session.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
        await session.execute(
            text("DELETE FROM user_companies WHERE company_id = :cid"),
            {"cid": str(company_id)},
        )
        await session.execute(
            text("DELETE FROM locations WHERE company_id = :cid"),
            {"cid": str(company_id)},
        )
        await session.execute(text("DELETE FROM users"))
        await session.execute(
            text("DELETE FROM companies WHERE id = :cid"),
            {"cid": str(company_id)},
        )

    # Bust in-process nonce cache — all users deleted, stale tokens must not auto-create rows
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
