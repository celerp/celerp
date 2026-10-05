# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""System administration endpoints: restart, factory reset and updates."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy import select, text
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


def _company_rows(schema: dict) -> dict[str, str]:
    """Every table holding rows of the company bound as ``:c``, with the condition that
    picks them: its company column, or else a foreign key to rows already picked (a
    conversation's messages, a run's entity maps). A key that clears on delete picks
    nothing: Postgres clears it. ``schema`` is the database catalog, so a switched-off
    module's tables are included."""
    from celerp.db_catalog import company_tables, ident

    owned = company_tables(schema, held=True)
    where: dict[str, str] = {"companies": "id = CAST(:c AS uuid)"}

    def rows(name: str) -> str:
        if name not in where:
            table = schema[name]
            if "company_id" in table.columns:  # a few connector tables keep it as text
                where[name] = f"company_id = CAST(CAST(:c AS text) AS {ident(table.columns['company_id'].udt)})"
            else:
                where[name] = " OR ".join(
                    f"({', '.join(map(ident, fk.cols))}) IN (SELECT {', '.join(map(ident, fk.tcols))} "
                    f"FROM {ident(fk.target)} WHERE {rows(fk.target)})"
                    for fk in table.fks if fk.target in owned and fk.target != name and not fk.clears)
        return where[name]

    return {name: rows(name) for name in owned}


def _held_elsewhere(schema: dict) -> str:
    """A query naming a table whose rows the reset of the company bound as ``:c`` would
    delete, change or trip over though they are not only that company's: a row outside
    the company naming one of its rows by any key, or a row hanging off the company that
    also hangs off another one. Nothing when there is none."""
    from celerp.db_catalog import ident

    rows = _company_rows(schema)
    checks = []
    for name in sorted(rows):
        for fk in schema[name].fks:
            if fk.target not in rows:
                continue
            refs = f"({', '.join(map(ident, fk.cols))}) IN (SELECT {', '.join(map(ident, fk.tcols))} " \
                   f"FROM {ident(fk.target)} WHERE ({rows[fk.target]})"
            checks.append((name, f"({rows[name]}) IS NOT TRUE AND {refs})"))
            if "company_id" not in schema[name].columns and not fk.clears:
                checks.append((name, f"({rows[name]}) AND {refs} IS NOT TRUE)"))
    return " UNION ALL ".join(
        f"(SELECT '{name.replace(chr(39), chr(39) * 2)}' WHERE EXISTS "
        f"(SELECT 1 FROM {ident(name)} WHERE {where}))" for name, where in checks) + " LIMIT 1"


def _company_deletes(schema: dict) -> list[str]:
    """The deletes that remove the company bound as ``:c``, each table before any it
    references. Tables that refer to each other in a loop have no such order, so the
    reset is refused naming them."""
    from celerp import db_catalog
    from celerp.accounting_roles import refusal

    rows = _company_rows(schema)
    order, unordered = db_catalog.fk_order(list(rows), schema)
    if looped := ", ".join(sorted(unordered - set(order))):
        raise HTTPException(status_code=409, detail=refusal(
            "system.factory_reset.reference_cycle",
            f"The tables {looped} refer to each other in a loop, so this company cannot be "
            "reset. Nothing was deleted.", tables=looped))
    return [f"DELETE FROM {db_catalog.ident(name)} WHERE {rows[name]}" for name in reversed(order)]


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
    from celerp import db_catalog
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
    schema = await db_catalog.read(session)
    held = await session.scalar(text(_held_elsewhere(schema)), {"c": str(company_id)})
    if held:
        raise HTTPException(status_code=409, detail=refusal(
            "system.factory_reset.held_elsewhere",
            f"Records in {held} that are not only this company's refer to its data, so it "
            "cannot be reset. Nothing was deleted.", table=held))
    for delete in _company_deletes(schema):
        await session.execute(text(delete), {"c": str(company_id)})
    await session.execute(text(db_catalog.delete_users_left_without_a_company(schema)), {"members": members})
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
