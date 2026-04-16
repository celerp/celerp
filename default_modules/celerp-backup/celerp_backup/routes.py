# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Backup router — /backup/*

Endpoints:
  POST /backup/trigger          Run a backup (database or files)
  GET  /backup/list             List backups from relay (proxied)
  POST /backup/restore/{id}     Restore a cloud backup (admin only)
  GET  /backup/export           Export full local backup (.celerp-backup)
  GET  /backup/export/{id}      Export a cloud backup as .celerp-backup
  POST /backup/import           Import a .celerp-backup file
  GET  /backup/status           Backup system status
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from celerp.config import settings
from celerp.services.auth import get_current_user
from celerp.services.backup import BackupResult
from ui.i18n import t, get_lang

router = APIRouter(
    dependencies=[Depends(get_current_user)],
)


class BackupStatusResponse(BaseModel):
    encryption_configured: bool
    gateway_connected: bool
    scheduler_running: bool
    backup_enabled: bool
    next_db_backup: str | None = None
    next_file_backup: str | None = None
    last_db_ok: bool | None = None
    last_db_error: str | None = None
    last_db_time: str | None = None
    last_file_ok: bool | None = None
    last_file_error: str | None = None
    last_file_time: str | None = None


@router.get("/status", response_model=BackupStatusResponse)
async def backup_status() -> BackupStatusResponse:
    from celerp.gateway.client import get_client
    from celerp.services import backup_scheduler
    db = backup_scheduler.last_db_result()
    fl = backup_scheduler.last_file_result()
    next_db = backup_scheduler.next_db_run_utc()
    next_fl = backup_scheduler.next_file_run_utc()
    return BackupStatusResponse(
        encryption_configured=bool(settings.backup_encryption_key),
        gateway_connected=get_client() is not None,
        scheduler_running=backup_scheduler._db_task is not None
            and not backup_scheduler._db_task.done(),
        backup_enabled=settings.backup_enabled,
        next_db_backup=next_db.isoformat() if next_db else None,
        next_file_backup=next_fl.isoformat() if next_fl else None,
        last_db_ok=db.ok,
        last_db_error=db.error,
        last_db_time=db.last_run.isoformat() if db.last_run else None,
        last_file_ok=fl.ok,
        last_file_error=fl.error,
        last_file_time=fl.last_run.isoformat() if fl.last_run else None,
    )


@router.post("/trigger")
async def trigger_backup(request: Request, type: str = "database"):
    """Run a backup: database or files. Returns flash + triggers table refresh."""
    from fasthtml.common import Div

    if type == "database":
        from celerp.services.backup import run_backup
        result: BackupResult = await run_backup(label="manual")
    elif type == "files":
        from celerp.services.backup_files import run_file_backup
        result = await run_file_backup(label="manual")
    else:
        raise HTTPException(status_code=400, detail="type must be 'database' or 'files'")

    if not result.ok:
        return Div(
            f"Backup failed: {result.error or 'Unknown error'}",
            cls="flash flash--error",
            id="backup-flash",
        )

    size_mb = result.size_bytes / (1024 ** 2)
    return Div(
        f"Backup complete ({size_mb:.1f} MB)",
        cls="flash flash--success",
        id="backup-flash",
        # Trigger table refresh via HTMX event
        hx_trigger="backupDone",
    )


@router.get("/list")
async def list_backups(request: Request, backup_type: str | None = None):
    """Proxy relay GET /backup/ with optional type filter.

    Returns HTML table if HX-Request header is present (HTMX), else JSON.
    """
    from celerp.services.backup import _relay_base_url, _session_headers
    url = f"{_relay_base_url()}/backup/"
    params = {}
    if backup_type:
        params["backup_type"] = backup_type
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=_session_headers(), params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:200])
        data = r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # If HTMX request, return rendered HTML table
    if request.headers.get("HX-Request"):
        from fasthtml.common import Table, Thead, Tbody, Tr, Th, Td, Button, Div, Span
        items = data.get("items", [])
        if not items:
            return Div(t("settings.no_backups_yet"), cls="empty-state-msg")

        def _fmt_size(b: int) -> str:
            if b < 1024:
                return f"{b} B"
            if b < 1024 ** 2:
                return f"{b / 1024:.1f} KB"
            return f"{b / 1024**2:.1f} MB"

        def _fmt_date(iso: str) -> str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return dt.strftime("%b %d, %H:%M")
            except Exception:
                return iso[:16]

        rows = [
            Tr(
                Td(_fmt_date(item["created_at"])),
                Td(_fmt_size(item["size_bytes"]), cls="cell--number"),
                Td(item.get("label") or "-"),
                Td(
                    Div(
                        Button(t("btn.export"),
                            onclick=f"window.location.href='/backup/export/{item['id']}'",
                            cls="btn btn--xs btn--secondary",
                        ),
                        Button(t("btn.restore"),
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
            Thead(Tr(
                Th(t("th.date")),
                Th(t("th.size")),
                Th(t("th.label")),
                Th(t("th.actions")),
            )),
            Tbody(*rows),
            cls="data-table data-table--compact",
        )

    return data


@router.post("/restore/{backup_id}")
async def restore_backup(backup_id: str, request: Request):
    """Restore a cloud backup. Creates safety backup first."""
    from fasthtml.common import Div
    from celerp.services.backup import run_restore
    result = await run_restore(backup_id)
    if not result.ok:
        return Div(
            f"Restore failed: {result.error or 'Unknown error'}",
            cls="flash flash--error",
            id="backup-flash",
        )
    return Div(t("settings.database_restored_restart_the_application_to_apply"),
        cls="flash flash--success",
        id="backup-flash",
    )


@router.get("/export")
async def export_local() -> FileResponse:
    """Export full local backup as .celerp-backup download."""
    from celerp.services.backup_export import export_full
    path = await export_full()
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/gzip",
    )


@router.get("/export/{backup_id}")
async def export_cloud(backup_id: str) -> FileResponse:
    """Export a cloud backup as .celerp-backup download."""
    from celerp.services.backup_export import export_from_cloud
    path = await export_from_cloud(backup_id)
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/gzip",
    )


@router.post("/import")
async def import_backup(request: Request, file: UploadFile = File(...)):
    """Import a .celerp-backup file."""
    import tempfile
    from pathlib import Path
    from fasthtml.common import Div
    from celerp.services.backup_import import validate_archive, run_import

    # Write uploaded file to temp
    tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
    content = await file.read()
    tmp.write(content)
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        meta = validate_archive(tmp_path)
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        return Div(str(exc), cls="flash flash--error", id="backup-flash")

    result = await run_import(tmp_path)
    tmp_path.unlink(missing_ok=True)

    if not result.ok:
        return Div(
            f"Import failed: {result.error or 'Unknown error'}",
            cls="flash flash--error",
            id="backup-flash",
        )
    return Div(
        f"Imported backup from {meta.company_name or 'unknown'}. Restart the application to apply changes.",
        cls="flash flash--success",
        id="backup-flash",
    )
