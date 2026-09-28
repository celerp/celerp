# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Received documents: what another business sent us, kept apart from our own books.

A received document is its own entity type, so lists, accounting, Doctor,
dashboards, reports and inventory never see it. Booking is the one explicit
step that creates something of ours: a local draft made through the normal
create path, with our own number, linked back to the received record.

Identity is exact: the sender's installation plus the sender's document id.
Re-imports, refreshed links and retries land on the same received record.
Revisions are tracked separately from identity: an identical revision is a
no-op, a different one updates the record and keeps the history.
"""

from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import get_current_company_settings, require_permission

ENTITY_TYPE = "received_document"

# What a received document becomes when booked. The sender's view of the deal
# flips to ours: their invoice is our bill, their purchase order is our sale.
# Types missing here stay in Received for review only.
BOOK_TARGETS: dict[str, str] = {
    "invoice": "bill",
    "proforma": "bill",
    "quotation": "purchase_order",
    "purchase_order": "invoice",
    "memo": "consignment_in",
}

# Revision states a received document can be in, derived at read time.
NOT_BOOKABLE = "not_bookable"
UNBOOKED = "unbooked"
BOOKED = "booked"
UPDATE_AVAILABLE = "update_available"
NEEDS_RECONCILIATION = "needs_reconciliation"
REVIEW_ONLY = "review_only"

_MAX_SOURCE_ID = 256

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Identity and revision
# ---------------------------------------------------------------------------

def document_digest(document: dict) -> str:
    """Content digest of a sanitized document; equal digests mean the same revision."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, default=str).encode()).hexdigest()


def source_identity(bundle: dict, link: str | None, digest: str) -> tuple[str, str, str | None]:
    """(installation, document, revision) naming where a bundle came from.

    Stable identity comes from the bundle's source block. Bundles without one
    (older senders) fall back to the share page they were fetched from, then to
    the file's own content. These fields are untrusted: they only ever pick
    which received record an import lands on, never what anyone may access."""
    src = bundle.get("source")
    if isinstance(src, dict):
        inst, doc = src.get("installation"), src.get("document")
        if isinstance(inst, str) and isinstance(doc, str) and inst.strip() and doc.strip():
            rev = src.get("revision")
            rev_s = str(rev)[:64] if isinstance(rev, (str, int)) and not isinstance(rev, bool) else None
            return inst.strip()[:_MAX_SOURCE_ID], doc.strip()[:_MAX_SOURCE_ID], rev_s
    if link:
        return "link", link[:_MAX_SOURCE_ID * 8], None
    return "file", digest, None


def received_id(installation: str, document: str) -> str:
    key = f"{installation}\n{document}".encode()
    return f"rcv:{hashlib.sha256(key).hexdigest()[:24]}"


def _sender_number(document: dict) -> str | None:
    return document.get("ref_id") or document.get("doc_number")


def _supersedes(new: str | None, current: str | None) -> bool:
    """Whether an arriving revision replaces the current one. Numeric sender
    revisions order; an older one arriving late is kept in history only."""
    if current is None or new is None:
        return True
    try:
        return int(new) >= int(current)
    except ValueError:
        return True


def apply_received_event(state: dict, event_type: str, data: dict) -> dict:
    """Projection handler for received_doc.* events."""
    s = dict(state)
    if event_type in ("received_doc.imported", "received_doc.revised"):
        digest = data["digest"]
        revisions = list(s.get("revisions") or [])
        if any(r.get("digest") == digest for r in revisions):
            return s
        document = data.get("document") or {}
        revisions.append({
            "digest": digest,
            "sender_revision": data.get("source_revision"),
            "received_at": data.get("received_at"),
            "total": document.get("total"),
            "doc_number": _sender_number(document),
        })
        s["revisions"] = revisions
        s.setdefault("source_installation", data.get("source_installation"))
        s.setdefault("source_document", data.get("source_document"))
        s.setdefault("first_received_at", data.get("received_at"))
        if data.get("source_link"):
            s["source_link"] = data["source_link"]
        if not s.get("current_digest") or _supersedes(data.get("source_revision"), s.get("current_revision")):
            s.update({
                "document": document,
                "current_digest": digest,
                "current_revision": data.get("source_revision"),
                "last_received_at": data.get("received_at"),
                "sender_name": document.get("company_name"),
                "doc_type": document.get("doc_type"),
                "sender_doc_number": _sender_number(document),
                "issue_date": document.get("issue_date"),
                "due_date": document.get("due_date"),
                "total": document.get("total"),
                "currency": document.get("currency"),
            })
    elif event_type == "received_doc.booked":
        s["booked"] = {
            "target_id": data["target_id"],
            "target_type": data["target_type"],
            "revision_digest": data["revision_digest"],
            "target_version": data["target_version"],
        }
    elif event_type == "received_doc.draft_updated":
        booked = dict(s.get("booked") or {})
        booked.update(revision_digest=data["revision_digest"], target_version=data["target_version"])
        s["booked"] = booked
    return s


def revision_state(state: dict, target: Projection | None) -> str:
    """Where a received document stands against the draft booked from it."""
    booked = state.get("booked")
    if not booked:
        return UNBOOKED if state.get("doc_type") in BOOK_TARGETS else NOT_BOOKABLE
    if booked.get("revision_digest") == state.get("current_digest"):
        return BOOKED
    if target is None or (target.state or {}).get("status") != "draft":
        return REVIEW_ONLY
    if target.version == booked.get("target_version"):
        return UPDATE_AVAILABLE
    return NEEDS_RECONCILIATION


# ---------------------------------------------------------------------------
# Recording an import
# ---------------------------------------------------------------------------

async def record_received(
    session: AsyncSession,
    company_id,
    actor_id,
    *,
    bundle: dict,
    document: dict,
    link: str | None,
) -> str:
    """Store a sanitized document in Received and return its id. Caller commits.

    The idempotency key is the revision itself, so a repeat of the same
    revision (retry, double submit, refreshed link) changes nothing."""
    digest = document_digest(document)
    installation, source_doc, revision = source_identity(bundle, link, digest)
    rid = received_id(installation, source_doc)
    existing = await session.get(Projection, (company_id, rid))
    await emit_event(
        session,
        company_id=company_id,
        entity_id=rid,
        entity_type=ENTITY_TYPE,
        event_type="received_doc.revised" if existing is not None else "received_doc.imported",
        data={
            "source_installation": installation,
            "source_document": source_doc,
            "source_revision": revision,
            "source_link": link,
            "digest": digest,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "document": document,
        },
        actor_id=actor_id,
        location_id=None,
        source="share_import",
        idempotency_key=f"{rid}:r:{digest}:{company_id}",
        metadata_={},
    )
    return rid


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

_LINE_FIELDS = ("name", "description", "quantity", "unit", "unit_price", "line_total", "taxes", "pieces", "weight")
_TOTAL_FIELDS = ("subtotal", "tax", "total", "doc_taxes", "discount", "shipping")
_BILL_FIELDS = ("issue_date", "due_date", "payment_terms")


def draft_fields(document: dict, target_type: str) -> dict:
    """The fields a booked draft takes from a received document.

    The sender's company becomes our counterparty, their number our reference.
    Nothing carries over that names an entity in the sender's system: no
    contact id, item id or SKU. Terms and notes stay the sender's."""
    fields: dict = {
        "contact_name": document.get("company_name"),
        "contact_email": document.get("company_email"),
        "contact_phone": document.get("company_phone"),
        "contact_billing_address": document.get("company_address"),
        "contact_tax_id": document.get("company_tax_id"),
        "reference": _sender_number(document),
        "currency": document.get("currency"),
        "line_items": [
            {k: li[k] for k in _LINE_FIELDS if li.get(k) is not None}
            for li in document.get("line_items") or []
        ],
    }
    for key in _TOTAL_FIELDS:
        if document.get(key) is not None:
            fields[key] = document[key]
    if target_type == "bill":
        for key in _BILL_FIELDS:
            if document.get(key):
                fields[key] = document[key]
    return {k: v for k, v in fields.items() if v is not None}


async def _received_row(session: AsyncSession, company_id, rid: str) -> Projection:
    row = await session.get(Projection, (company_id, rid))
    if row is None or row.entity_type != ENTITY_TYPE:
        raise HTTPException(status_code=404, detail="Received document not found")
    return row


async def _fresh(session: AsyncSession, company_id, entity_id: str) -> Projection | None:
    return (await session.execute(
        select(Projection)
        .where(Projection.company_id == company_id, Projection.entity_id == entity_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def book(session: AsyncSession, company_id, rid: str, *, role: str, settings: dict, user) -> dict:
    """Create our own draft from a received document, at most once."""
    from celerp_docs.routes import DocCreatePayload, create_doc

    state = (await _received_row(session, company_id, rid)).state or {}
    if state.get("booked"):
        return {"id": state["booked"]["target_id"]}
    target_type = BOOK_TARGETS.get(state.get("doc_type") or "")
    if target_type is None:
        raise HTTPException(
            status_code=422,
            detail="This document type is kept in Received for review and cannot be booked.",
        )
    digest = state["current_digest"]
    payload = DocCreatePayload(
        doc_type=target_type,
        status="draft",
        source_received_id=rid,
        idempotency_key=f"book:{rid}:{company_id}",
        **draft_fields(state.get("document") or {}, target_type),
    )
    created = await create_doc(payload, company_id, None, role, settings, user, session)
    target_id = created["id"]
    target = await _fresh(session, company_id, target_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=rid,
        entity_type=ENTITY_TYPE,
        event_type="received_doc.booked",
        data={
            "target_id": target_id,
            "target_type": target_type,
            "revision_digest": digest,
            "target_version": target.version if target is not None else 0,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=f"{rid}:booked:{company_id}",
        metadata_={},
    )
    await session.commit()
    return {"id": target_id}


async def update_draft(session: AsyncSession, company_id, rid: str, *, role: str, settings: dict, user) -> dict:
    """Apply the latest received revision to the untouched draft booked from it."""
    from celerp_docs.routes import DocPatch, patch_doc

    state = (await _received_row(session, company_id, rid)).state or {}
    booked = state.get("booked") or {}
    target = await _fresh(session, company_id, booked["target_id"]) if booked else None
    status = revision_state(state, target)
    if status != UPDATE_AVAILABLE:
        messages = {
            BOOKED: "The draft already matches the latest revision.",
            NEEDS_RECONCILIATION: "The draft was edited here, so the new revision has to be reconciled by hand.",
            REVIEW_ONLY: "The booked document is no longer a draft. The new revision is kept here for review.",
        }
        raise HTTPException(status_code=409, detail=messages.get(status, "Book this document first."))
    digest = state["current_digest"]
    fields = draft_fields(state.get("document") or {}, booked["target_type"])
    current = target.state or {}
    changed = {k: {"old": current.get(k), "new": v} for k, v in fields.items() if current.get(k) != v}
    if changed:
        await patch_doc(
            booked["target_id"],
            DocPatch(fields_changed=changed, idempotency_key=f"{rid}:update:{digest}:{company_id}"),
            company_id, None, role, settings, user, session,
        )
    target = await _fresh(session, company_id, booked["target_id"])
    await emit_event(
        session,
        company_id=company_id,
        entity_id=rid,
        entity_type=ENTITY_TYPE,
        event_type="received_doc.draft_updated",
        data={"revision_digest": digest, "target_version": target.version if target is not None else 0},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=f"{rid}:draft_updated:{digest}:{company_id}",
        metadata_={},
    )
    await session.commit()
    return {"id": booked["target_id"]}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _summary(row: Projection, target: Projection | None) -> dict:
    s = row.state or {}
    return {
        "id": row.entity_id,
        "sender_name": s.get("sender_name"),
        "doc_type": s.get("doc_type"),
        "sender_doc_number": s.get("sender_doc_number"),
        "issue_date": s.get("issue_date"),
        "due_date": s.get("due_date"),
        "total": s.get("total"),
        "currency": s.get("currency"),
        "first_received_at": s.get("first_received_at"),
        "last_received_at": s.get("last_received_at"),
        "revision_count": len(s.get("revisions") or []),
        "revision_state": revision_state(s, target),
        "book_target": BOOK_TARGETS.get(s.get("doc_type") or ""),
        "booked_id": (s.get("booked") or {}).get("target_id"),
        "source_link": s.get("source_link"),
    }


async def _targets(session: AsyncSession, company_id, rows: list[Projection]) -> dict[str, Projection]:
    ids = [(r.state or {}).get("booked", {}).get("target_id") for r in rows]
    ids = [i for i in ids if i]
    if not ids:
        return {}
    found = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_id.in_(ids))
    )).scalars().all()
    return {p.entity_id: p for p in found}


@router.get("/docs/received")
async def list_received(
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_documents"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id, Projection.entity_type == ENTITY_TYPE)
    )).scalars().all()
    targets = await _targets(session, company_id, rows)
    items = [_summary(r, targets.get(((r.state or {}).get("booked") or {}).get("target_id"))) for r in rows]
    items.sort(key=lambda i: i.get("last_received_at") or "", reverse=True)
    return {"items": items}


@router.get("/docs/received/{rid}")
async def get_received(
    rid: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("view_documents"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _received_row(session, company_id, rid)
    targets = await _targets(session, company_id, [row])
    s = row.state or {}
    target = targets.get((s.get("booked") or {}).get("target_id"))
    return {
        **_summary(row, target),
        "document": s.get("document") or {},
        "revisions": sorted(s.get("revisions") or [], key=lambda r: r.get("received_at") or "", reverse=True),
        "booked_status": (target.state or {}).get("status") if target is not None else None,
    }


@router.post("/docs/received/{rid}/book")
async def book_received(
    rid: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await book(session, company_id, rid, role=role, settings=settings, user=user)


@router.post("/docs/received/{rid}/update-draft")
async def update_received_draft(
    rid: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await update_draft(session, company_id, rid, role=role, settings=settings, user=user)
