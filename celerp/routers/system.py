# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""System export/import — full portable snapshot of a company's data.

Export: POST /system/export
  Returns a .celerp ZIP archive containing:
    meta.json      — format version, exported_at, company slug
    company.json   — company settings, locations (password hashes excluded), users
    ledger.jsonl   — all ledger entries in ascending ts order (newline-delimited JSON)
    attachments/   — all attachment files (from static/attachments/<company_id>/)

Import: POST /system/import
  Accepts the .celerp ZIP. Creates the company (slug collision → append suffix),
  re-creates locations and users, replays all ledger events (rebuilding projections),
  and restores attachment files. Idempotent per idempotency_key.

Both endpoints require admin role.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company, Location, User
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.auth import get_current_company_id, require_admin

_FORMAT_VERSION = 1
_ATTACHMENT_ROOT = Path("static/attachments")

router = APIRouter(dependencies=[Depends(require_admin)])


# ── Export ────────────────────────────────────────────────────────────────────


@router.post("/export")
async def export_system(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream a full portable .celerp snapshot of the authenticated company."""
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")

    locations = (
        await session.execute(select(Location).where(Location.company_id == company_id))
    ).scalars().all()

    users = (
        await session.execute(select(User).where(User.company_id == company_id))
    ).scalars().all()

    ledger_rows = (
        await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.company_id == company_id)
            .order_by(LedgerEntry.ts.asc(), LedgerEntry.id.asc())
        )
    ).scalars().all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # meta.json
        zf.writestr(
            "meta.json",
            json.dumps(
                {
                    "format_version": _FORMAT_VERSION,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "company_slug": company.slug,
                },
                indent=2,
            ),
        )

        # company.json — auth_hash excluded intentionally
        zf.writestr(
            "company.json",
            json.dumps(
                {
                    "id": str(company.id),
                    "name": company.name,
                    "slug": company.slug,
                    "settings": company.settings,
                    "created_at": company.created_at.isoformat(),
                    "locations": [
                        {
                            "id": str(loc.id),
                            "name": loc.name,
                            "type": loc.type,
                            "address": loc.address,
                            "is_default": loc.is_default,
                        }
                        for loc in locations
                    ],
                    "users": [
                        {
                            "id": str(u.id),
                            "email": u.email,
                            "name": u.name,
                            "role": u.role,
                            "is_active": u.is_active,
                        }
                        for u in users
                    ],
                },
                indent=2,
            ),
        )

        # ledger.jsonl
        ledger_lines = "\n".join(
            json.dumps(
                {
                    "id": row.id,
                    "entity_id": row.entity_id,
                    "entity_type": row.entity_type,
                    "event_type": row.event_type,
                    "data": row.data,
                    "source": row.source,
                    "idempotency_key": row.idempotency_key,
                    "metadata_": row.metadata_,
                    "ts": row.ts.isoformat() if row.ts else None,
                }
            )
            for row in ledger_rows
        )
        zf.writestr("ledger.jsonl", ledger_lines)

        # attachments/
        att_dir = _ATTACHMENT_ROOT / str(company_id)
        if att_dir.exists():
            for att_file in att_dir.rglob("*"):
                if att_file.is_file():
                    arc_name = "attachments/" + att_file.relative_to(att_dir).as_posix()
                    zf.write(att_file, arc_name)

    buf.seek(0)
    filename = f"{company.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.celerp"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Import ────────────────────────────────────────────────────────────────────


class _ImportResult:
    def __init__(self) -> None:
        self.events_replayed: int = 0
        self.events_skipped: int = 0
        self.attachments_restored: int = 0
        self.company_id: str = ""
        self.slug: str = ""


@router.post("/import")
async def import_system(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Import a .celerp archive. Creates company, replays ledger, restores attachments."""
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid .celerp archive.")

    names = set(zf.namelist())
    if "meta.json" not in names or "company.json" not in names or "ledger.jsonl" not in names:
        raise HTTPException(status_code=400, detail="Archive missing required files.")

    meta = json.loads(zf.read("meta.json"))
    if meta.get("format_version") != _FORMAT_VERSION:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported format_version {meta.get('format_version')}. Expected {_FORMAT_VERSION}.",
        )

    company_data = json.loads(zf.read("company.json"))
    result = _ImportResult()

    # ── Create or locate company ──────────────────────────────────────────────
    slug = company_data["slug"]
    existing = (await session.execute(select(Company).where(Company.slug == slug))).scalar_one_or_none()
    if existing is not None:
        # Disambiguate slug
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    company = Company(
        id=uuid.uuid4(),
        name=company_data["name"],
        slug=slug,
        settings=company_data.get("settings", {}),
    )
    session.add(company)
    await session.flush()

    result.company_id = str(company.id)
    result.slug = slug

    # ── Create locations ──────────────────────────────────────────────────────
    loc_id_map: dict[str, uuid.UUID] = {}
    for loc in company_data.get("locations", []):
        new_loc = Location(
            id=uuid.uuid4(),
            company_id=company.id,
            name=loc["name"],
            type=loc["type"],
            address=loc.get("address"),
            is_default=loc.get("is_default", False),
        )
        session.add(new_loc)
        loc_id_map[loc["id"]] = new_loc.id
    await session.flush()

    # ── Create users (no auth_hash — require password reset) ─────────────────
    for u in company_data.get("users", []):
        new_user = User(
            id=uuid.uuid4(),
            company_id=company.id,
            email=u["email"],
            name=u["name"],
            role=u.get("role", "user"),
            is_active=u.get("is_active", True),
        )
        session.add(new_user)
    await session.flush()

    # ── Replay ledger ─────────────────────────────────────────────────────────
    ledger_text = zf.read("ledger.jsonl").decode("utf-8")
    for line in ledger_text.splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)

        # Remap location_id if present in data
        loc_id_raw = ev.get("data", {}).get("location_id")
        if loc_id_raw and loc_id_raw in loc_id_map:
            ev["data"]["location_id"] = str(loc_id_map[loc_id_raw])

        try:
            await emit_event(
                session,
                company_id=company.id,
                entity_id=ev["entity_id"],
                entity_type=ev["entity_type"],
                event_type=ev["event_type"],
                data=ev["data"],
                source=ev.get("source", "system_import"),
                idempotency_key=f"import:{result.company_id}:{ev['idempotency_key']}",
                metadata_=ev.get("metadata_"),
            )
            result.events_replayed += 1
        except Exception:
            result.events_skipped += 1

    # ── Restore attachments ───────────────────────────────────────────────────
    att_prefix = "attachments/"
    att_files = [n for n in names if n.startswith(att_prefix) and not n.endswith("/")]
    att_dest = _ATTACHMENT_ROOT / str(company.id)
    att_dest.mkdir(parents=True, exist_ok=True)
    for arc_name in att_files:
        rel_path = arc_name[len(att_prefix):]
        dest = att_dest / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(arc_name))
        result.attachments_restored += 1

    await session.commit()

    return {
        "ok": True,
        "company_id": result.company_id,
        "slug": result.slug,
        "events_replayed": result.events_replayed,
        "events_skipped": result.events_skipped,
        "attachments_restored": result.attachments_restored,
    }


# ── Graceful restart ──────────────────────────────────────────────────────────

def _restart_sentinel_path() -> "Path":
    from celerp.config import config_path
    return config_path().parent / ".restart_requested"


def _send_sigterm() -> None:
    """Write restart sentinel then SIGTERM self.

    The sentinel tells the `celerp start` process manager to respawn rather
    than exit when it detects the subprocess death.
    """
    import os, signal, time
    _restart_sentinel_path().touch()
    time.sleep(0.2)  # let the response flush
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def restart_server(
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
) -> dict:
    """Gracefully restart the server process (SIGTERM → process manager respawns).

    Used by the setup wizard after applying a preset so new modules are loaded.
    Returns immediately; the restart happens ~200ms later in a background task.
    """
    background_tasks.add_task(_send_sigterm)
    return {"ok": True, "restarting": True}
