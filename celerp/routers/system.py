# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""System administration endpoints: restart and updates."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.company import User
from celerp.services.auth import (
    get_current_user, is_install_owner, require_install_owner,
)

router = APIRouter()


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


@router.post("/restart", dependencies=[Depends(require_install_owner)])
async def restart_server(
    background_tasks: BackgroundTasks,
) -> dict:
    """Gracefully restart the server process (SIGTERM → process manager respawns).

    Restarting stops the whole installation, so only the installation owner may.
    Returns immediately; the restart happens ~200ms later in a background task.
    """
    background_tasks.add_task(_send_sigterm)
    return {"ok": True, "restarting": True}


# ── Updates ───────────────────────────────────────────────────────────────────
# Installation-wide, so gated on the install owner; reading the status needs
# only a login.


@router.get("/installation-owner")
async def installation_owner(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Whether this login is the installation owner, so pages offer
    installation-wide controls only to them."""
    return {"installation_owner": await is_install_owner(session, user.id)}


@router.get("/update")
async def update_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The update card's state; see celerp.services.update.status."""
    from celerp.services import update
    return update.status(owner=await is_install_owner(session, user.id))


@router.post("/update", status_code=202, dependencies=[Depends(require_install_owner)])
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


@router.post("/update/check", dependencies=[Depends(require_install_owner)])
async def check_for_update() -> dict:
    from celerp.services import update
    await asyncio.to_thread(update.refresh_check)
    return update.status(owner=True)


class UpdateSettings(BaseModel):
    auto: StrictBool


@router.patch("/update/settings", dependencies=[Depends(require_install_owner)])
async def update_settings(body: UpdateSettings) -> dict:
    """Turn automatic overnight updates on or off."""
    from celerp.services import update
    update.set_auto(body.auto)
    return {"auto": body.auto}
