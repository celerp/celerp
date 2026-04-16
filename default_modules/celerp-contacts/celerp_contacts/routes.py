# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
_DATA_DIR = os.environ.get("CELERP_DATA_DIR", "data")


# ── Pydantic models ───────────────────────────────────────────────────────────

class ContactCreate(BaseModel):
    name: str
    company_name: str | None = None
    website: str | None = None
    currency: str | None = None
    email: str | None = None
    phone: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None
    contact_type: str = "customer"
    attributes: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class ContactUpdate(BaseModel):
    fields_changed: dict[str, dict] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TagBody(BaseModel):
    tags: list[str]
    idempotency_key: str | None = None


class ContactNoteCreate(BaseModel):
    note: str
    idempotency_key: str | None = None


class ContactNoteUpdate(BaseModel):
    note: str
    idempotency_key: str | None = None


class ContactPersonCreate(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    is_primary: bool = False


class ContactPersonUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    is_primary: bool | None = None


class ContactAddressCreate(BaseModel):
    address_type: str = "billing"  # billing, shipping, other
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    attn: str | None = None
    is_default: bool = False


class ContactAddressUpdate(BaseModel):
    address_type: str | None = None
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    attn: str | None = None
    is_default: bool | None = None


class MemoCreate(BaseModel):
    contact_id: str | None = None
    notes: str | None = None
    idempotency_key: str | None = None


class MemoItemAdd(BaseModel):
    item_id: str
    quantity: float | None = None
    idempotency_key: str | None = None


class MemoReturnItem(BaseModel):
    item_id: str
    quantity: float | None = None
    condition: str = "good"


class MemoReturnBody(BaseModel):
    items: list[MemoReturnItem]
    idempotency_key: str | None = None


class CRMImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


class CRMBatchImportRequest(BaseModel):
    records: list[CRMImportRecord]


# ── Contact CRUD ──────────────────────────────────────────────────────────────

_CONTACT_TYPE_FILTER: dict[str, tuple[str, ...]] = {
    "customer": ("customer", "both"),
    "vendor": ("vendor", "both"),
    "both": ("both",),
}


@router.post("/contacts")
async def create_contact(payload: ContactCreate, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    if not payload.name or not payload.name.strip():
        raise HTTPException(status_code=422, detail="Contact name is required and must be non-empty")
    entity_id = f"contact:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="contact",
        event_type="crm.contact.created",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@router.get("/contacts")
async def list_contacts(
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    contact_type: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "contact"))
    ).scalars().all()
    results = [r.state | {"id": r.entity_id} for r in rows]
    if q:
        q_lower = q.lower()
        results = [c for c in results if q_lower in (c.get("name") or "").lower()
                   or q_lower in (c.get("email") or "").lower()
                   or q_lower in (c.get("phone") or "").lower()
                   or q_lower in (c.get("company_name") or "").lower()
                   or any(q_lower in t.lower() for t in (c.get("tags") or []))]
    if contact_type and contact_type in _CONTACT_TYPE_FILTER:
        allowed = _CONTACT_TYPE_FILTER[contact_type]
        results = [c for c in results if (c.get("contact_type") or "customer") in allowed]
    return {"items": results[offset:offset + limit], "total": len(results)}


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row.state | {"id": row.entity_id}


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, payload: ContactUpdate, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.updated",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Tags ──────────────────────────────────────────────────────────────────────

@router.post("/contacts/{contact_id}/tags")
async def tag_contact(contact_id: str, payload: TagBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.tagged",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Files ─────────────────────────────────────────────────────────────────────


def _uploads_dir(contact_id: str) -> Path:
    return Path(_DATA_DIR) / "uploads" / "contacts" / contact_id


@router.post("/contacts/{contact_id}/files")
async def upload_contact_file(
    contact_id: str,
    file: UploadFile,
    description: str = Form(""),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {_MAX_FILE_BYTES // 1024 // 1024} MB limit")

    file_id = str(uuid.uuid4())
    filename = file.filename or f"file_{file_id}"
    content_type = file.content_type or "application/octet-stream"

    dest_dir = _uploads_dir(contact_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{file_id}_{filename}"
    dest.write_bytes(content)

    now = datetime.now(timezone.utc).isoformat()
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_attached",
        data={
            "file_id": file_id,
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
            "uploaded_at": now,
            "description": description,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {
        "event_id": entry.id,
        "file_id": file_id,
        "filename": filename,
        "content_type": content_type,
        "size": len(content),
    }


@router.get("/contacts/{contact_id}/files/{file_id}")
async def download_contact_file(
    contact_id: str,
    file_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    files = row.state.get("files", [])
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")

    dest = _uploads_dir(contact_id) / f"{file_id}_{match['filename']}"
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")

    return FileResponse(
        path=str(dest),
        filename=match["filename"],
        media_type=match.get("content_type", "application/octet-stream"),
    )


@router.delete("/contacts/{contact_id}/files/{file_id}")
async def delete_contact_file(
    contact_id: str,
    file_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    files = row.state.get("files", [])
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_removed",
        data={"file_id": file_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    # Remove file from disk (best-effort)
    dest = _uploads_dir(contact_id) / f"{file_id}_{match['filename']}"
    dest.unlink(missing_ok=True)

    updated = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    return (updated.state if updated else {}) | {"id": contact_id}


# ── Notes ─────────────────────────────────────────────────────────────────────

@router.get("/contacts/{contact_id}/notes")
async def list_contact_notes(
    contact_id: str,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "contact_note",
            )
        )
    ).scalars().all()
    notes = []
    for r in rows:
        state = r.state
        if state.get("contact_id") == contact_id and not state.get("deleted"):
            notes.append(state | {"id": r.entity_id})
    notes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    return notes


@router.post("/contacts/{contact_id}/notes")
async def add_contact_note(
    contact_id: str,
    payload: ContactNoteCreate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    note_id = f"note:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=note_id,
        entity_type="contact_note",
        event_type="crm.contact.note_added",
        data={
            "contact_id": contact_id,
            "note_id": note_id,
            "note": payload.note,
            "author_id": str(user.id),
            "author_name": getattr(user, "name", None) or user.email,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": note_id}


@router.patch("/contacts/{contact_id}/notes/{note_id}")
async def update_contact_note(
    contact_id: str,
    note_id: str,
    payload: ContactNoteUpdate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=note_id,
        entity_type="contact_note",
        event_type="crm.contact.note_updated",
        data={
            "contact_id": contact_id,
            "note_id": note_id,
            "note": payload.note,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.delete("/contacts/{contact_id}/notes/{note_id}")
async def delete_contact_note(
    contact_id: str,
    note_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=note_id,
        entity_type="contact_note",
        event_type="crm.contact.note_removed",
        data={"contact_id": contact_id, "note_id": note_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── People ────────────────────────────────────────────────────────────────────

@router.post("/contacts/{contact_id}/people")
async def add_contact_person(
    contact_id: str,
    payload: ContactPersonCreate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    person_id = f"person:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.person_added",
        data={"person_id": person_id, **payload.model_dump(exclude_none=True)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "person_id": person_id}


@router.patch("/contacts/{contact_id}/people/{person_id}")
async def update_contact_person(
    contact_id: str,
    person_id: str,
    payload: ContactPersonUpdate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.person_updated",
        data={"person_id": person_id, **payload.model_dump(exclude_none=True)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.delete("/contacts/{contact_id}/people/{person_id}")
async def remove_contact_person(
    contact_id: str,
    person_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.person_removed",
        data={"person_id": person_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Addresses ─────────────────────────────────────────────────────────────────

@router.post("/contacts/{contact_id}/addresses")
async def add_contact_address(
    contact_id: str,
    payload: ContactAddressCreate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    address_id = f"address:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.address_added",
        data={"address_id": address_id, **payload.model_dump(exclude_none=True)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "address_id": address_id}


@router.patch("/contacts/{contact_id}/addresses/{address_id}")
async def update_contact_address(
    contact_id: str,
    address_id: str,
    payload: ContactAddressUpdate,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.address_updated",
        data={"address_id": address_id, **payload.model_dump(exclude_none=True)},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.delete("/contacts/{contact_id}/addresses/{address_id}")
async def remove_contact_address(
    contact_id: str,
    address_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.address_removed",
        data={"address_id": address_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Memos ─────────────────────────────────────────────────────────────────────

@router.post("/memos")
async def create_memo(payload: MemoCreate, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entity_id = f"memo:{uuid.uuid4()}"
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="memo",
        event_type="crm.memo.created",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "id": entity_id}


@router.get("/memos")
async def list_memos(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "memo"))
    ).scalars().all()
    items = [r.state | {"id": r.entity_id} for r in rows]
    if status:
        items = [i for i in items if i.get("status") == status]
    # Sort newest first
    items.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    return {"items": items[offset:offset + limit], "total": len(items)}


@router.get("/memos/summary")
async def get_memo_summary(company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Memo exposure summary from projections.

    Active memos: status='out' (excludes returned/invoiced/cancelled).
    total field is the face value stored at creation time.
    """
    rows = (
        await session.execute(select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "memo"))
    ).scalars().all()

    active_total = Decimal(0)
    all_total = Decimal(0)
    count_by_status: dict[str, int] = {}

    for row in rows:
        state = row.state
        status = state.get("status", "")
        count_by_status[status] = count_by_status.get(status, 0) + 1
        try:
            v = state.get("total")
            if v is not None:
                d = Decimal(str(v))
                all_total += d
                if status == "out":
                    active_total += d
        except Exception:
            pass

    return {
        "memo_count": len(rows),
        "active_total": float(active_total),
        "all_total": float(all_total),
        "count_by_status": count_by_status,
    }


@router.post("/memos/{memo_id}/items")
async def add_memo_item(memo_id: str, payload: MemoItemAdd, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.item_added",
        data=payload.model_dump(exclude_none=True),
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.delete("/memos/{memo_id}/items/{item_id}")
async def remove_memo_item(memo_id: str, item_id: str, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.item_removed",
        data={"item_id": item_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/memos/{memo_id}/approve")
async def approve_memo(memo_id: str, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.approved",
        data={},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/memos/{memo_id}/cancel")
async def cancel_memo(memo_id: str, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.cancelled",
        data={},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


@router.post("/memos/{memo_id}/convert-to-invoice")
async def convert_memo_to_invoice(memo_id: str, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    memo = await session.get(Projection, {"company_id": company_id, "entity_id": memo_id})
    if memo is None or memo.entity_type != "memo":
        raise HTTPException(status_code=404, detail="Memo not found")
    if memo.state.get("status") == "cancelled":
        raise HTTPException(status_code=409, detail="Cannot invoice a cancelled memo")

    doc_id = f"doc:INV-MEMO-{uuid.uuid4().hex[:10].upper()}"
    line_items = [{"item_id": i.get("item_id"), "quantity": i.get("quantity") or 1, "unit_price": 0, "line_total": 0} for i in memo.state.get("items", [])]
    await emit_event(
        session,
        company_id=company_id,
        entity_id=doc_id,
        entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "source_memo_id": memo_id, "contact_id": memo.state.get("contact_id"), "line_items": line_items, "status": "draft", "total": 0, "amount_paid": 0, "amount_outstanding": 0},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.invoiced",
        data={"doc_id": doc_id, "items_invoiced": [x.get("item_id") for x in memo.state.get("items", [])]},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, "doc_id": doc_id}


@router.post("/memos/{memo_id}/return")
async def return_memo_items(memo_id: str, payload: MemoReturnBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    memo = await session.get(Projection, {"company_id": company_id, "entity_id": memo_id})
    if memo is None or memo.entity_type != "memo":
        raise HTTPException(status_code=404, detail="Memo not found")
    if memo.state.get("status") == "invoiced":
        raise HTTPException(status_code=409, detail="Cannot return items from invoiced memo")

    for item in payload.items:
        pr = await session.get(Projection, {"company_id": company_id, "entity_id": item.item_id})
        if pr and pr.entity_type == "item":
            await emit_event(
                session,
                company_id=company_id,
                entity_id=item.item_id,
                entity_type="item",
                event_type="item.status.set",
                data={"new_status": "available" if item.condition == "good" else "damaged"},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={"source_memo": memo_id},
            )

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=memo_id,
        entity_type="memo",
        event_type="crm.memo.returned",
        data={"items_returned": [x.model_dump() for x in payload.items]},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=payload.idempotency_key or str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id}


# ── Import endpoints (CIF) ───────────────────────────────────────────────────

@router.post("/contacts/import")
async def import_contact(
    body: CRMImportRecord,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accept a CIF contact record and emit the corresponding ledger event."""
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=body.entity_id,
        entity_type="contact",
        event_type=body.event_type,
        data=body.data,
        actor_id=user.id,
        location_id=None,
        source=body.source,
        idempotency_key=body.idempotency_key,
        metadata_={"source_ts": body.source_ts} if body.source_ts else {},
    )
    await session.commit()
    return {"event_id": entry.id, "idempotency_hit": False}


@router.post("/memos/import")
async def import_memo(
    body: CRMImportRecord,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accept a CIF memo record and emit the corresponding ledger event."""
    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=body.entity_id,
        entity_type="memo",
        event_type=body.event_type,
        data=body.data,
        actor_id=user.id,
        location_id=None,
        source=body.source,
        idempotency_key=body.idempotency_key,
        metadata_={"source_ts": body.source_ts} if body.source_ts else {},
    )
    await session.commit()
    return {"event_id": entry.id, "idempotency_hit": False}


# ── Batch import endpoints (CIF) ─────────────────────────────────────────────

async def _batch_import(
    records: list[CRMImportRecord],
    entity_type: str,
    company_id,
    user,
    session: AsyncSession,
) -> BatchImportResult:
    """Shared batch logic: pre-check existing keys, emit only new records."""
    from sqlalchemy import select as _select

    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in records]
    existing = set(
        (await session.execute(
            _select(LedgerEntry.idempotency_key).where(LedgerEntry.idempotency_key.in_(keys))
        )).scalars().all()
    )

    created = skipped = 0
    errors: list[str] = []

    for rec in records:
        if rec.idempotency_key in existing:
            skipped += 1
            continue
        try:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=rec.entity_id,
                entity_type=entity_type,
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing.add(rec.idempotency_key)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, errors=errors)


@router.post("/contacts/import/batch", response_model=BatchImportResult)
async def batch_import_contacts(
    body: CRMBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch-import CIF contact records. Idempotent on idempotency_key. Max 500 per call."""
    return await _batch_import(body.records, "contact", company_id, user, session)


@router.post("/memos/import/batch", response_model=BatchImportResult)
async def batch_import_memos(
    body: CRMBatchImportRequest,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch-import CIF memo records. Idempotent on idempotency_key. Max 500 per call."""
    return await _batch_import(body.records, "memo", company_id, user, session)


# ── CSV export ────────────────────────────────────────────────────────────────

@router.get("/contacts/export/csv")
async def export_contacts_csv(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
) -> StreamingResponse:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "contact")
    )).scalars().all()
    contacts = [r.state | {"entity_id": r.entity_id} for r in rows]
    if q:
        ql = q.lower()
        contacts = [c for c in contacts if ql in str(c.get("name", "")).lower() or ql in str(c.get("email", "")).lower()]

    _COLS = ["entity_id", "name", "phone", "email", "billing_address", "tax_id", "credit_limit", "contact_type"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLS, extrasaction="ignore")
    writer.writeheader()
    for c in contacts:
        writer.writerow({col: c.get(col, "") for col in _COLS})
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts.csv"},
    )


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/crm", tags=["crm"])
