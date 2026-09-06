# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.attachments import remove_attachment, store_upload
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.permissions import require_permission

from celerp_contacts.search import search_contacts

router = APIRouter(dependencies=[Depends(get_current_user)])


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


@router.post("/contacts")
async def create_contact(payload: ContactCreate, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), _: None = require_permission("edit_contacts"), session: AsyncSession = Depends(get_session)) -> dict:
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
    include_deleted: bool = False,
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Deterministic order (name, then unique id) so OFFSET pagination over this Python-sliced list
    # is stable - otherwise the DB's arbitrary row order lets a contact be skipped between pages.
    results = await search_contacts(
        session, company_id, q, include_deleted=include_deleted, contact_type=contact_type
    )
    return {"items": results[offset:offset + limit], "total": len(results)}


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str, company_id: str = Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row.state | {"id": row.entity_id}


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, payload: ContactUpdate, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), _: None = require_permission("edit_contacts"), session: AsyncSession = Depends(get_session)) -> dict:
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
async def tag_contact(contact_id: str, payload: TagBody, company_id: str = Depends(get_current_company_id), user=Depends(get_current_user), _: None = require_permission("edit_contacts"), session: AsyncSession = Depends(get_session)) -> dict:
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


def _get_contact_file(files: list[dict], file_id: str) -> dict:
    match = next((f for f in files if f.get("id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")
    return match


@router.post("/contacts/{contact_id}/files")
async def upload_contact_file(
    contact_id: str,
    file: UploadFile,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")

    try:
        meta = await store_upload(company_id, file)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_attached",
        data={
            "entity_id": contact_id,
            "entity_type": "contact",
            "file_id": meta["id"],
            "filename": meta["filename"],
            "mime": meta["mime"],
            "size": meta["size"],
            "url": meta["url"],
            "document_tag": None,
            "description": None,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"event_id": entry.id, **meta}


@router.post("/contacts/{contact_id}/files/{file_id}/tag")
async def tag_contact_file(
    contact_id: str,
    file_id: str,
    document_tag: str = Form(""),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update the document_tag on an existing uploaded file."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    f = _get_contact_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_tagged",
        data={"entity_id": contact_id, "entity_type": "contact", "file_id": file_id, "document_tag": document_tag, "filename": f.get("filename")},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    return (updated.state if updated else {}) | {"id": contact_id}


@router.patch("/contacts/{contact_id}/files/{file_id}/description")
async def update_contact_file_description(
    contact_id: str,
    file_id: str,
    description: str = Form(""),
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    f = _get_contact_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_description_updated",
        data={"entity_id": contact_id, "entity_type": "contact", "file_id": file_id, "description": description, "filename": f.get("filename")},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    return (updated.state if updated else {}) | {"id": contact_id}


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

    match = _get_contact_file(row.state.get("files", []), file_id)
    url = match.get("url", "")
    from celerp.config import settings
    # Local backend: url = /static/attachments/<company_id>/<file>
    # Resolve against data_dir — url is web-relative, not cwd-relative.
    # Legacy records (pre-fix) have no url stored; reconstruct from file_id + filename.
    if not url:
        ext = Path(match.get("filename", "")).suffix
        url = f"/static/attachments/{company_id}/{file_id}{ext}"
    dest = settings.data_dir / url.lstrip("/")
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")

    return FileResponse(
        path=str(dest),
        filename=match["filename"],
        media_type=match.get("mime", "application/octet-stream"),
    )


@router.delete("/contacts/{contact_id}/files/{file_id}")
async def delete_contact_file(
    contact_id: str,
    file_id: str,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=404, detail="Not found")
    f = _get_contact_file(row.state.get("files", []), file_id)

    entry = await emit_event(
        session,
        company_id=company_id,
        entity_id=contact_id,
        entity_type="contact",
        event_type="crm.contact.file_deleted",
        data={"entity_id": contact_id, "entity_type": "contact", "file_id": file_id, "filename": f.get("filename")},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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
    _: None = require_permission("edit_contacts"),
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



# ── Import endpoints (CIF) ───────────────────────────────────────────────────

@router.post("/contacts/import")
async def import_contact(
    body: CRMImportRecord,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
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



# ── CSV export ────────────────────────────────────────────────────────────────

@router.get("/contacts/export/csv")
async def export_contacts_csv(
    company_id: str = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    selected: list[str] = Query(default=[]),
) -> StreamingResponse:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == "contact")
    )).scalars().all()
    contacts = [r.state | {"entity_id": r.entity_id} for r in rows]
    if selected:
        selected_set = set(selected)
        contacts = [c for c in contacts if c.get("entity_id") in selected_set]
    elif q:
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


class BulkContactDeleteBody(BaseModel):
    contact_ids: list[str]


@router.post("/contacts/bulk/delete")
async def bulk_delete_contacts(
    payload: BulkContactDeleteBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not payload.contact_ids:
        raise HTTPException(status_code=422, detail="No contacts selected.")

    # Validate all contacts exist and are not already deleted
    contact_rows = []
    for cid in payload.contact_ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": cid})
        if row is None or row.state.get("deleted"):
            raise HTTPException(status_code=404, detail=f"Contact '{cid}' not found.")
        contact_rows.append(row)

    # Block deletion if any contact has ANY documents (regardless of status).
    # Provide a detailed breakdown by doc_type so the user knows exactly what's linked.
    doc_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
        )
    )).scalars().all()
    contact_id_set = set(payload.contact_ids)
    # blocking: {contact_id: {doc_type: count}}
    blocking: dict[str, dict[str, int]] = {}
    for dr in doc_rows:
        cid = dr.state.get("contact_id")
        if cid in contact_id_set:
            doc_type = dr.state.get("doc_type", "document")
            blocking.setdefault(cid, {})
            blocking[cid][doc_type] = blocking[cid].get(doc_type, 0) + 1
    if blocking:
        names = {r.entity_id: r.state.get("name", r.entity_id) for r in contact_rows}
        parts = []
        for cid, type_counts in blocking.items():
            summary = ", ".join(f"{n} {dt}(s)" for dt, n in sorted(type_counts.items()))
            parts.append(f"{names.get(cid, cid)}: {summary}")
        detail = "Cannot delete contact(s) with associated documents: " + "; ".join(parts)
        raise HTTPException(status_code=422, detail=detail)

    for row in contact_rows:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=row.entity_id,
            entity_type="contact",
            event_type="crm.contact.updated",
            data={"fields_changed": {"deleted": {"new": True}}},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )
    await session.commit()
    return {"deleted": len(contact_rows)}


class ContactMergeBody(BaseModel):
    target_contact_id: str
    source_contact_ids: list[str]


async def merge_contacts_service(
    session: AsyncSession,
    company_id: str,
    actor_id,
    target_contact_id: str,
    source_contact_ids: list[str],
) -> dict:
    """Merge source contacts into the target: union people/addresses/tags onto the winner, tombstone the
    sources (deleted + merged_into), and re-point every doc/deal referencing a source to the winner.
    Emits events only (the caller commits), so it is replay-safe and reusable by both the merge route
    and the self-contact migration - one merge implementation (DRY)."""
    from types import SimpleNamespace
    payload = SimpleNamespace(target_contact_id=target_contact_id, source_contact_ids=source_contact_ids)
    user = SimpleNamespace(id=actor_id)
    # 1. Validate inputs
    if not payload.source_contact_ids:
        raise HTTPException(status_code=422, detail="source_contact_ids must not be empty.")
    if payload.target_contact_id in payload.source_contact_ids:
        raise HTTPException(status_code=422, detail="target_contact_id must not be in source_contact_ids.")

    # 2. Validate target
    target_row = await session.get(Projection, {"company_id": company_id, "entity_id": payload.target_contact_id})
    if target_row is None or target_row.state.get("entity_type") not in ("contact", None):
        raise HTTPException(status_code=404, detail=f"Target contact '{payload.target_contact_id}' not found.")
    if target_row.state.get("deleted"):
        raise HTTPException(status_code=422, detail="Cannot merge into a deleted contact.")

    # 3. Validate sources
    source_rows = []
    for sid in payload.source_contact_ids:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": sid})
        if row is None:
            raise HTTPException(status_code=404, detail=f"Source contact '{sid}' not found.")
        if row.state.get("deleted"):
            raise HTTPException(status_code=422, detail=f"Contact '{sid}' is already deleted.")
        if row.state.get("merged_into"):
            raise HTTPException(
                status_code=422,
                detail=f"Contact '{sid}' is already merged into '{row.state['merged_into']}'.",
            )
        source_rows.append(row)

    warnings: list[str] = []
    winner = target_row.state
    winner_name = winner.get("name", "")

    # Currency mismatch warning
    winner_currency = winner.get("currency") or ""
    for row in source_rows:
        src_currency = row.state.get("currency") or ""
        if src_currency and winner_currency and src_currency != winner_currency:
            warnings.append(
                f"Currency mismatch: winner currency '{winner_currency}' applies to contact record; "
                "existing documents keep their own currencies."
            )
            break

    # 4. Compute merged people (deduplicate by email, then by name)
    def _email_key(p: dict) -> str:
        return (p.get("email") or "").lower().strip()

    def _name_key(p: dict) -> str:
        return (p.get("name") or "").lower().strip()

    merged_people = list(winner.get("people") or [])
    existing_emails = {_email_key(p) for p in merged_people if _email_key(p)}
    existing_names = {_name_key(p) for p in merged_people if not _email_key(p) and _name_key(p)}
    for row in source_rows:
        for p in (row.state.get("people") or []):
            ek = _email_key(p)
            nk = _name_key(p)
            if ek:
                if ek not in existing_emails:
                    merged_people.append(p)
                    existing_emails.add(ek)
            elif nk and nk not in existing_names:
                merged_people.append(p)
                existing_names.add(nk)

    # 5. Compute merged addresses (deduplicate by line1 + postcode)
    def _addr_key(a: dict) -> tuple:
        return (
            (a.get("line1") or "").lower().strip(),
            (a.get("postcode") or a.get("postal_code") or "").lower().strip(),
        )

    merged_addresses = list(winner.get("addresses") or [])
    existing_addr_keys = {_addr_key(a) for a in merged_addresses}
    for row in source_rows:
        for a in (row.state.get("addresses") or []):
            ak = _addr_key(a)
            if ak not in existing_addr_keys:
                merged_addresses.append(a)
                existing_addr_keys.add(ak)

    # 6. Compute merged tags
    all_tags = list(set(winner.get("tags") or []))
    for row in source_rows:
        for tag in (row.state.get("tags") or []):
            if tag not in all_tags:
                all_tags.append(tag)
    merged_tags = sorted(all_tags)

    # 7. Emit single crm.contact.merged event on winner (carries all merged data)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=payload.target_contact_id,
        entity_type="contact",
        event_type="crm.contact.merged",
        data={
            "source_contact_ids": payload.source_contact_ids,
            "merged_people": merged_people,
            "merged_addresses": merged_addresses,
            "merged_tags": merged_tags,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    # 8. Tombstone sources
    for row in source_rows:
        await emit_event(
            session,
            company_id=company_id,
            entity_id=row.entity_id,
            entity_type="contact",
            event_type="crm.contact.updated",
            data={"fields_changed": {
                "deleted": {"new": True},
                "merged_into": {"new": payload.target_contact_id},
            }},
            actor_id=user.id,
            location_id=None,
            source="api",
            idempotency_key=str(uuid.uuid4()),
            metadata_={},
        )

    # 9. Re-point documents (contact_id + contact_name, regardless of doc status)
    source_ids = set(payload.source_contact_ids)
    doc_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "doc",
        )
    )).scalars().all()
    docs_updated = 0
    for dr in doc_rows:
        if dr.state.get("contact_id") in source_ids:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=dr.entity_id,
                entity_type="doc",
                event_type="doc.updated",
                data={"fields_changed": {
                    "contact_id": {"old": dr.state["contact_id"], "new": payload.target_contact_id},
                    "contact_name": {"old": dr.state.get("contact_name"), "new": winner_name},
                }},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={},
            )
            docs_updated += 1

    # 10. Re-point deals
    deal_rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "deal",
        )
    )).scalars().all()
    for dr in deal_rows:
        if dr.state.get("contact_id") in source_ids:
            await emit_event(
                session,
                company_id=company_id,
                entity_id=dr.entity_id,
                entity_type="deal",
                event_type="crm.deal.updated",
                data={"fields_changed": {
                    "contact_id": {"old": dr.state["contact_id"], "new": payload.target_contact_id},
                }},
                actor_id=user.id,
                location_id=None,
                source="api",
                idempotency_key=str(uuid.uuid4()),
                metadata_={},
            )

    # 11. Notes: NOT re-parented. Contact detail page queries merged_from IDs.
    # No events emitted for notes.

    return {
        "merged_into": payload.target_contact_id,
        "sources_merged": len(source_rows),
        "docs_updated": docs_updated,
        "warnings": warnings,
    }


@router.post("/contacts/merge")
async def merge_contacts(
    payload: ContactMergeBody,
    company_id: str = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await merge_contacts_service(
        session, company_id, actor_id=user.id,
        target_contact_id=payload.target_contact_id,
        source_contact_ids=payload.source_contact_ids,
    )
    await session.commit()
    return result


def setup_api_routes(app) -> None:
    app.include_router(router, prefix="/crm", tags=["crm"])


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
    _: None = require_permission("edit_contacts"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch-import CIF contact records. Idempotent on idempotency_key. Max 500 per call."""
    return await _batch_import(body.records, "contact", company_id, user, session)
