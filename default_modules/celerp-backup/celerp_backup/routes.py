# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Backup router — /backup/*

Endpoints:
  POST /backup/trigger          Run a backup (database or files)
  GET  /backup/list             List backups from relay (proxied)
  POST /backup/restore/{id}     Restore a cloud backup
  GET  /backup/export           Export full local backup (.celerp-backup)
  GET  /backup/export/{id}      Export a cloud backup as .celerp-backup
  POST /backup/import           Import a .celerp-backup file
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from celerp.config import settings
from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers
from celerp.services.auth import get_current_user
from celerp.services.backup import BackupResult
from ui.i18n import t

router = APIRouter(dependencies=[Depends(get_current_user)])


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
async def trigger_backup(type: str = "database"):
    """Run a backup (database or files). Returns flash + HX-Trigger to refresh list."""
    if type == "database":
        from celerp.services.backup import run_backup
        result: BackupResult = await run_backup(label="manual")
    elif type == "files":
        from celerp.services.backup_files import run_file_backup
        result = await run_file_backup(label="manual")
    else:
        raise HTTPException(status_code=400, detail="type must be 'database' or 'files'")

    if not result.ok:
        return _flash(f"Backup failed: {result.error or 'Unknown error'}", "error")

    resp = _flash(f"Backup complete ({_fmt_size(result.size_bytes)})")
    resp.headers["HX-Trigger"] = "backupDone"
    return resp


@router.get("/list")
async def list_backups(request: Request, backup_type: str | None = None):
    """Proxy relay GET /backup/ with optional type filter.

    Returns rendered HTML table on HTMX requests, else JSON.
    Returns empty-state when relay is not connected.
    """
    from celerp.gateway.state import get_session_token

    if not get_session_token():
        if request.headers.get("HX-Request"):
            from fasthtml.common import Div, to_xml
            return Response(
                content=to_xml(Div(t("settings.cloud_not_connected"), cls="empty-state-msg")),
                media_type="text/html",
            )
        return {"items": []}

    params = {"backup_type": backup_type} if backup_type else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{relay_http_url()}/backup/",
                headers=relay_session_headers(),
                params=params,
            )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:200])
        data = r.json()
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
    """Restore a cloud backup. Creates safety backup first."""
    from celerp.services.backup import run_restore
    result = await run_restore(backup_id)
    if not result.ok:
        return _flash(f"Restore failed: {result.error or 'Unknown error'}", "error")
    return _flash(t("settings.database_restored_restart_the_application_to_apply"))


@router.get("/export")
async def export_local() -> FileResponse:
    """Export full local backup as .celerp-backup download."""
    from celerp.services.backup_export import export_full
    path = await export_full()
    return FileResponse(path=str(path), filename=path.name, media_type="application/gzip")


@router.get("/export/{backup_id}")
async def export_cloud(backup_id: str) -> FileResponse:
    """Export a cloud backup as .celerp-backup download."""
    from celerp.services.backup_export import export_from_cloud
    path = await export_from_cloud(backup_id)
    return FileResponse(path=str(path), filename=path.name, media_type="application/gzip")


@router.post("/import")
async def import_backup(request: Request, file: UploadFile = File(...)):
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

    result = await run_import(tmp_path)
    tmp_path.unlink(missing_ok=True)

    if not result.ok:
        return _flash(f"Import failed: {result.error or 'Unknown error'}", "error")
    return _flash(
        f"Imported backup from {meta.company_name or 'unknown'}. "
        "Restart the application to apply changes."
    )
