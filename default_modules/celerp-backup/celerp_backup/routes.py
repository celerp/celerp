# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Backup router — /backup/*

Endpoints:
  POST /backup/trigger            Run a backup (database or files)
  GET  /backup/list               List backups from relay (proxied)
  POST /backup/restore/{id}       Restore a cloud backup
  GET  /backup/export             Export full local backup (.celerp-backup)
  GET  /backup/export/{id}        Export a cloud backup as .celerp-backup
  POST /backup/import             Import a .celerp-backup file (authenticated)
  POST /backup/import-bootstrap   Import a .celerp-backup file (no auth, pre-bootstrap only)
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from celerp.db import get_session
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.gateway.state import get_session_token
from celerp.services.auth import get_current_user, viewer_read_only
from celerp.services.backup import BackupResult
from ui.i18n import t

router = APIRouter(dependencies=[Depends(get_current_user), Depends(viewer_read_only)])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 ** 2:.1f} MB"


def _fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return iso[:16]


def _flash(msg: str, kind: str = "success") -> Response:
    """Return an HTMX-swappable flash div."""
    from fasthtml.common import Div, to_xml
    html = to_xml(Div(msg, cls=f"flash flash--{kind}", id="backup-flash"))
    return Response(content=html, media_type="text/html")


def _restore_flash(result, base_msg: str) -> Response:
    """Post-restore flash that always lets the user continue the journey.

    A restart finishes applying a restore. When the import already scheduled one
    (the module set changed), say so and reload the page once the server is back;
    otherwise offer a Restart now button - the user is never told to restart
    without a way to do it from where they stand.
    """
    from fasthtml.common import Button, Div, Script, to_xml
    from ui.components.shell import RESTART_POLL_JS

    msg = base_msg
    if result.warnings:
        names = ", ".join(result.warnings)
        msg += f" Note: {len(result.warnings)} module(s) enabled on the source are not installed on this server ({names})."
    if result.schema_warning:
        msg += f" Warning: {result.schema_warning}"
    kind = "warning" if (result.warnings or result.schema_warning) else "success"
    if result.restart_scheduled:
        body = Div(
            Div(f"{msg} {t('settings.restarting_automatically')}", cls=f"flash flash--{kind}"),
            Script(RESTART_POLL_JS),
            id="backup-flash",
        )
    else:
        body = Div(
            Div(msg, cls=f"flash flash--{kind}"),
            Button(t("btn.restart_now"), cls="btn btn--primary mt-sm",
                   hx_post="/backup/restart-app",
                   hx_target="#backup-flash", hx_swap="outerHTML"),
            id="backup-flash",
        )
    return Response(content=to_xml(body), media_type="text/html")


def _backup_table(items: list[dict]):
    """Render a data-table of backup items."""
    from fasthtml.common import Button, Div, Table, Tbody, Td, Th, Thead, Tr

    rows = [
        Tr(
            Td(_fmt_date(item["created_at"])),
            Td(_fmt_size(item["size_bytes"]), cls="cell--number"),
            Td(item.get("label") or "-"),
            Td(
                Div(
                    Button(
                        t("btn.export"),
                        onclick=f"window.location.href='/backup/export/{item['id']}'",
                        cls="btn btn--xs btn--secondary",
                    ),
                    Button(
                        t("btn.restore"),
                        hx_post=f"/backup/restore/{item['id']}",
                        hx_confirm="This will replace your current database. Type RESTORE to confirm.",
                        hx_target="#backup-flash",
                        hx_swap="outerHTML",
                        cls="btn btn--xs btn--outline btn--danger",
                    ),
                    cls="cell-actions",
                ),
            ),
            cls="data-row",
        )
        for item in items
    ]
    return Table(
        Thead(Tr(Th(t("th.date")), Th(t("th.size")), Th(t("th.label")), Th(t("th.actions")))),
        Tbody(*rows),
        cls="data-table data-table--compact",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/trigger")
async def trigger_backup():
    """Run a cloud snapshot (database + files). Returns flash + HX-Trigger to refresh list."""
    from celerp.services import backup_repo, backup_scheduler
    result: BackupResult = await backup_repo.run_snapshot(label="manual")
    backup_scheduler.record_db_result(result.ok, result.error, result.size_bytes or 0)

    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error or "Unknown error")

    resp = _flash(f"Backup complete ({_fmt_size(result.size_bytes)} uploaded)")
    resp.headers["HX-Trigger"] = "backupDone"
    return resp


@router.get("/list")
async def list_backups(request: Request):
    """List cloud snapshots.

    Returns a rendered HTML table on HTMX requests, else JSON. Returns the
    empty-state when the relay is not connected.
    """
    from celerp.gateway.state import get_session_token
    from celerp.services import backup_repo

    if not get_session_token():
        if request.headers.get("HX-Request"):
            from fasthtml.common import Div, to_xml
            return Response(
                content=to_xml(Div(t("settings.cloud_not_connected"), cls="empty-state-msg")),
                media_type="text/html",
            )
        return {"items": []}

    try:
        data = await backup_repo.list_snapshots()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not request.headers.get("HX-Request"):
        return data

    items = data.get("items", [])
    if not items:
        from fasthtml.common import Div, to_xml
        return Response(
            content=to_xml(Div(t("settings.no_backups_yet"), cls="empty-state-msg")),
            media_type="text/html",
        )
    from fasthtml.common import to_xml
    return Response(content=to_xml(_backup_table(items)), media_type="text/html")


@router.post("/restore/{backup_id}")
async def restore_backup(backup_id: str):
    """Restore a cloud snapshot (database + files) via the canonical importer."""
    from celerp.services import backup_repo
    result = await backup_repo.restore_snapshot(backup_id)
    if not result.ok:
        return _flash(f"Restore failed: {result.error or 'Unknown error'}", "error")
    return _restore_flash(result, t("settings.database_restored_restart_the_application_to_apply"))


@router.get("/export")
async def export_local() -> FileResponse:
    """Export full local backup as .celerp-backup download."""
    from celerp.services.backup_export import export_full
    try:
        path = await export_full()
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # export_full writes a temp archive (delete=False); remove it once the response is
    # sent so multi-GB exports don't pile up in the temp dir.
    return FileResponse(path=str(path), filename=path.name, media_type="application/gzip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.get("/export/{backup_id}")
async def export_cloud(backup_id: str) -> FileResponse:
    """Download a cloud snapshot, rebuilt as a .celerp-backup archive."""
    from celerp.services.backup_repo import reassemble_snapshot
    try:
        path = await reassemble_snapshot(backup_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return FileResponse(path=str(path), filename=path.name, media_type="application/gzip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/import")
async def import_backup(request: Request, file: UploadFile = File(...), session: AsyncSession = Depends(get_session)):
    """Import a .celerp-backup file."""
    from celerp.services.backup_import import run_import, validate_archive

    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.write(await file.read())
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        meta = validate_archive(tmp_path)
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        return _flash(str(exc), "error")

    # Close session NOW so its connection returns to the pool before engine.dispose().
    # Keeping it open holds a lock and causes pg_restore to hang.
    await session.close()

    result = await run_import(tmp_path)
    tmp_path.unlink(missing_ok=True)

    if not result.ok:
        return _flash(f"Import failed: {result.error or 'Unknown error'}", "error")
    return _restore_flash(
        result,
        f"Imported backup from {meta.company_name or 'unknown'}. "
        f"Restart the application to apply changes.",
    )


# ── Bootstrap import (public — no auth, only works before first user exists) ──

public_router = APIRouter()


@public_router.post("/import-bootstrap")
async def import_backup_bootstrap(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Import a .celerp-backup file when no users exist (setup wizard restore)."""
    from celerp.services.backup_import import run_import, validate_archive
    from sqlalchemy import select
    from celerp.models.company import User

    existing = (await session.execute(select(User).limit(1))).first()
    if existing is not None:
        raise HTTPException(
            status_code=403,
            detail="System already bootstrapped. Log in and use Settings > Backup to restore.",
        )

    # Close the session NOW so its connection is returned to the pool.
    # run_import will call engine.dispose() to close all pool connections before
    # pg_restore runs — keeping this session open would hold a lock and cause pg_restore to hang.
    await session.close()

    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    tmp.write(await file.read())
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        meta = validate_archive(tmp_path)
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc))

    result = await run_import(tmp_path)
    tmp_path.unlink(missing_ok=True)

    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error or "Import failed")

    return {
        "ok": True,
        "company_name": meta.company_name,
        "warnings": result.warnings,
        "schema_warning": result.schema_warning,
        "restart_scheduled": result.restart_scheduled,
    }
