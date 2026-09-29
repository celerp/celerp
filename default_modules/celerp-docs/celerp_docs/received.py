# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Received documents: what another business sent us, kept apart from our own books.

A received document is its own entity type, so lists, accounting, Doctor,
dashboards, reports and inventory never see it. Booking is the one explicit
step that creates something of ours: a local draft made through the normal
create path, with our own number, linked back to the received record.

Identity is exact: the sender's installation, the sender's company on it and
the sender's document id. Re-imports, refreshed links and retries land on the
same received record. Revisions are tracked separately from identity. The
sender's revision number, when the bundle carries one, identifies a revision
and orders it: a repeat is a no-op, a newer one becomes current, an older one
arriving late is kept in history, and the same number with different content
is refused. Without a revision number, arrival order decides and content
equal to the current revision is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.company_lock import lock_company
from celerp.services.money import round_money, to_decimal
from celerp.services.permissions import get_current_company_settings, require_permission
from celerp_docs.doc_projections import _recalc_list_totals

ENTITY_TYPE = "received_document"


class BookTarget(NamedTuple):
    kind: str  # "doc" or "list": which canonical create path books it
    type: str  # the doc_type or list_type it becomes


# What a received document becomes when booked. The sender's view of the deal
# flips to ours: their invoice is our bill, their purchase order is a
# quotation we answer with, their consignment out is our consignment in.
# Types missing here stay in Received for review only.
BOOK_TARGETS: dict[str, BookTarget] = {
    "invoice": BookTarget("doc", "bill"),
    "proforma": BookTarget("doc", "purchase_order"),
    "quotation": BookTarget("doc", "purchase_order"),
    "purchase_order": BookTarget("list", "quotation"),
    "memo": BookTarget("doc", "consignment_in"),
    "consignment_in": BookTarget("doc", "memo"),
}

# Revision states a received document can be in, derived at read time.
NOT_BOOKABLE = "not_bookable"
UNBOOKED = "unbooked"
BOOKED = "booked"
UPDATE_AVAILABLE = "update_available"
NEEDS_RECONCILIATION = "needs_reconciliation"
REVIEW_ONLY = "review_only"
SOURCE_CHANGED = "source_changed"

_MAX_SOURCE_ID = 256

router = APIRouter(dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Identity and revision
# ---------------------------------------------------------------------------

class SourceIdentity(NamedTuple):
    installation: str
    company: str
    document: str
    revision: str | None


def document_digest(document: dict) -> str:
    """Content digest of a sanitized document; equal digests mean the same revision."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, default=str).encode()).hexdigest()


def _source_str(value) -> str | None:
    return value.strip()[:_MAX_SOURCE_ID] if isinstance(value, str) and value.strip() else None


def source_identity(bundle: dict, link: str | None, digest: str) -> SourceIdentity:
    """Where a bundle came from.

    Stable identity comes from the bundle's source block: the sender's
    installation, the company on it (bundles from before companies were
    named leave it empty) and the document. Bundles without a source block
    fall back to the share page they were fetched from, then to the file's
    own content. These fields are untrusted: they only ever pick which
    received record an import lands on, never what anyone may access."""
    src = bundle.get("source")
    if isinstance(src, dict):
        inst, doc = _source_str(src.get("installation")), _source_str(src.get("document"))
        if inst and doc:
            rev = src.get("revision")
            rev_s = str(rev)[:64] if isinstance(rev, (str, int)) and not isinstance(rev, bool) else None
            return SourceIdentity(inst, _source_str(src.get("company")) or "", doc, rev_s)
    if link:
        return SourceIdentity("link", "", link[:_MAX_SOURCE_ID * 8], None)
    return SourceIdentity("file", "", digest, None)


def received_id(installation: str, company: str, document: str) -> str:
    key = f"{installation}\n{company}\n{document}".encode()
    return f"rcv:{hashlib.sha256(key).hexdigest()[:24]}"


def revision_key(rid: str, position: str, digest: str, link: str | None, company_id) -> str:
    """Idempotency key of one revision arriving through one link. ``position``
    is the sender's revision, or for unnumbered revisions the local sequence it
    arrives after, so content that comes back after a change is a new arrival."""
    link_key = hashlib.sha256((link or "").encode()).hexdigest()[:16]
    return f"{rid}:r:{position}:{digest}:{link_key}:{company_id}"


def _sender_number(document: dict) -> str | None:
    return document.get("ref_id") or document.get("doc_number")


def _supersedes(new: str | None, current: str | None) -> bool:
    """Whether an arriving revision replaces the current one. Numeric sender
    revisions order; an older one arriving late is kept in history only.
    Unnumbered revisions go by arrival order."""
    if current is None or new is None:
        return True
    try:
        return int(new) > int(current)
    except ValueError:
        return True


def _is_repeat(state: dict, revision: str | None, digest: str) -> bool:
    """Whether an arrival adds nothing to the history: its sender revision is
    already recorded, or, unnumbered, its content is the current revision's."""
    if revision is not None:
        return any(r.get("sender_revision") == revision for r in state.get("revisions") or [])
    return digest == state.get("current_digest")


def apply_received_event(state: dict, event_type: str, data: dict) -> dict:
    """Projection handler for received_doc.* events."""
    s = dict(state)
    if event_type in ("received_doc.imported", "received_doc.revised"):
        # The latest link the document arrived through, even when its content is
        # unchanged: a refreshed link replaces one the sender has since revoked.
        if data.get("source_link"):
            s["source_link"] = data["source_link"]
        digest, revision = data["digest"], data.get("source_revision")
        if _is_repeat(s, revision, digest):
            return s
        document = data.get("document") or {}
        revisions = list(s.get("revisions") or [])
        seq = len(revisions) + 1
        revisions.append({
            "seq": seq,
            "digest": digest,
            "sender_revision": data.get("source_revision"),
            "received_at": data.get("received_at"),
            "total": document.get("total"),
            "doc_number": _sender_number(document),
        })
        s["revisions"] = revisions
        s.setdefault("source_installation", data.get("source_installation"))
        s.setdefault("source_company", data.get("source_company"))
        s.setdefault("source_document", data.get("source_document"))
        s.setdefault("first_received_at", data.get("received_at"))
        if not s.get("current_digest") or _supersedes(revision, s.get("current_revision")):
            s.update({
                "document": document,
                "current_seq": seq,
                "current_digest": digest,
                "current_revision": revision,
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
            "target_kind": data["target_kind"],
            "target_type": data["target_type"],
            "revision_seq": data["revision_seq"],
            "revision_digest": data["revision_digest"],
            "target_version": data["target_version"],
        }
    elif event_type in ("received_doc.draft_updated", "received_doc.reconciled"):
        # Updated from the revision, or reconciled with it by hand: either way
        # the draft now stands for this revision at this version.
        booked = dict(s.get("booked") or {})
        booked.update(
            revision_seq=data["revision_seq"],
            revision_digest=data["revision_digest"],
            target_version=data["target_version"],
        )
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
    if not _fits(state, booked, target):
        return SOURCE_CHANGED
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

    The idempotency key is the revision plus the link it came through, so a
    repeat of the same revision over the same link (retry, double submit)
    changes nothing, and the same revision over a refreshed link only
    records the new link."""
    digest = document_digest(document)
    source = source_identity(bundle, link, digest)
    rid = received_id(source.installation, source.company, source.document)
    # Two imports of one revision with different content must not both pass the
    # history check below: the second waits here and then reads the first.
    await lock_company(session, company_id)
    existing = await session.get(Projection, (company_id, rid), populate_existing=True)
    state = (existing.state or {}) if existing is not None else {}
    if source.revision is not None:
        if any(
            r.get("sender_revision") == source.revision and r.get("digest") != digest
            for r in state.get("revisions") or []
        ):
            raise HTTPException(
                status_code=422,
                detail="This revision was received before with different content, so it was not imported.",
            )
        position = f"v{source.revision}"
    else:
        # A repeat of the current content keeps the position it first arrived
        # at, so a retry over the same link is the same arrival.
        seq = state.get("current_seq") or 0
        position = f"after{seq - 1 if _is_repeat(state, None, digest) else seq}"
    await emit_event(
        session,
        company_id=company_id,
        entity_id=rid,
        entity_type=ENTITY_TYPE,
        event_type="received_doc.revised" if existing is not None else "received_doc.imported",
        data={
            "source_installation": source.installation,
            "source_company": source.company,
            "source_document": source.document,
            "source_revision": source.revision,
            "source_link": link,
            "digest": digest,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "document": document,
        },
        actor_id=actor_id,
        location_id=None,
        source="share_import",
        idempotency_key=revision_key(rid, position, digest, link, company_id),
        metadata_={},
    )
    return rid


# ---------------------------------------------------------------------------
# Source-managed fields
# ---------------------------------------------------------------------------

# Booking fills these from the received document, and Update draft keeps them
# in step with it, including clearing one the sender removed. Every other field
# on the draft is ours and is never touched. Nothing carries over that names an
# entity in the sender's system: no contact id, item id or SKU.
_COUNTERPARTY = {
    "contact_name": "company_name",
    "contact_email": "company_email",
    "contact_phone": "company_phone",
    "contact_billing_address": "company_address",
    "contact_tax_id": "company_tax_id",
}
# A document line carries its discount inside line_total; a list line keeps the
# percentage it was given.
_DOC_LINE_FIELDS = ("name", "description", "quantity", "unit", "unit_price", "line_total", "taxes", "tax_rate", "pieces", "weight")
_LIST_LINE_FIELDS = ("name", "description", "quantity", "unit", "unit_price", "discount_pct", "line_total", "pieces", "weight")
# Amounts a document always holds, with the value an absent one has on create.
_DOC_TOTALS = {
    "subtotal": 0, "tax": 0, "total": 0, "doc_taxes": [], "discount": 0,
    "discount_type": "flat", "discount_amount": 0, "shipping": 0, "tax_rate": 0,
}
_BILL_FIELDS = ("issue_date", "due_date", "payment_terms")


def _tax_rate(taxes) -> float | None:
    """A list line carries one tax rate; the sender's line taxes collapse to their sum."""
    if not isinstance(taxes, list) or not taxes:
        return None
    return sum(float(t.get("rate") or 0) for t in taxes if isinstance(t, dict)) or None


def _lines(document: dict, kind: str) -> list[dict]:
    fields = _LIST_LINE_FIELDS if kind == "list" else _DOC_LINE_FIELDS
    out = []
    for li in document.get("line_items") or []:
        line = {k: li[k] for k in fields if li.get(k) is not None}
        if kind == "list":
            rate = li.get("tax_rate") or _tax_rate(li.get("taxes"))
            if rate:
                line["tax_rate"] = rate
        out.append(line)
    return out


def managed_fields(document: dict, target: BookTarget) -> dict:
    """Every field the received document manages on a draft of this target,
    None where the sender left it empty."""
    fields: dict = {local: document.get(sender) for local, sender in _COUNTERPARTY.items()}
    fields["reference"] = _sender_number(document)
    fields["line_items"] = _lines(document, target.kind)
    fields["currency"] = document.get("currency")
    if target.kind == "list":
        # A list holds its tax as one rate per line or one header rate.
        fields["discount"] = document.get("discount") or 0
        fields["discount_type"] = document.get("discount_type") or "flat"
        fields["tax"] = _tax_rate(document.get("doc_taxes")) or document.get("tax_rate") or 0
        return fields
    for key, absent in _DOC_TOTALS.items():
        fields[key] = document.get(key) or absent
    if target.type == "bill":
        for key in _BILL_FIELDS:
            fields[key] = document.get(key) or None
    return fields


def _money(value, currency) -> Decimal:
    return round_money(to_decimal(value or 0), currency)


def unrepresentable(document: dict, target: BookTarget) -> str | None:
    """Why a draft of this target cannot carry the document's money exactly, or None.

    A document keeps the sender's amounts as they are. A quotation list
    recomputes its own total from its lines, one discount and simple tax rates,
    so a revision it cannot hold without changing the total is refused rather
    than booked or applied with different money."""
    if target.kind != "list":
        return None
    currency = document.get("currency")
    doc_taxes = document.get("doc_taxes") or []
    line_taxes = [t for li in document.get("line_items") or [] for t in li.get("taxes") or []]
    if _money(document.get("shipping"), currency):
        return "A quotation has no shipping charge, so this document cannot become one without changing its total."
    if any(t.get("is_compound") for t in (*doc_taxes, *line_taxes)):
        return "A quotation has no compound tax, so this document cannot become one without changing its total."
    if doc_taxes and line_taxes:
        return (
            "A quotation taxes either each line or the whole document, not both, "
            "so this document cannot become one without changing its total."
        )
    totals = _recalc_list_totals(dict(managed_fields(document, target)))
    if _money(totals["total"], currency) != _money(document.get("total"), currency):
        return "A quotation would total this document differently, so it cannot become one without changing its total."
    return None


def _fits(state: dict, booked: dict, target: Projection) -> bool:
    """Whether the current revision can update the booked draft: its type
    still books to the same kind of draft, in the draft's currency, and the
    draft can carry its money exactly."""
    kind = BookTarget(booked["target_kind"], booked["target_type"])
    return (
        BOOK_TARGETS.get(state.get("doc_type") or "") == kind
        and state.get("currency") == (target.state or {}).get("currency")
        and unrepresentable(state.get("document") or {}, kind) is None
    )


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

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


async def _emit(session: AsyncSession, company_id, rid: str, event_type: str, data: dict, user, key: str) -> None:
    await emit_event(
        session, company_id=company_id, entity_id=rid, entity_type=ENTITY_TYPE,
        event_type=event_type, data=data, actor_id=user.id, location_id=None,
        source="api", idempotency_key=key, metadata_={},
    )


async def book(session: AsyncSession, company_id, rid: str, *, role: str, settings: dict, user) -> dict:
    """Create our own draft from a received document, at most once.

    The draft is made through the canonical create path for its kind, and the
    version recorded is that create event's own id: a retry after the draft
    was created replays the same event rather than creating a second draft."""
    from celerp_docs.routes import DocCreatePayload, ListCreatePayload, create_doc, create_list

    state = (await _received_row(session, company_id, rid)).state or {}
    if state.get("booked"):
        booked = state["booked"]
        return {"id": booked["target_id"], "kind": booked["target_kind"]}
    target = BOOK_TARGETS.get(state.get("doc_type") or "")
    if target is None:
        raise HTTPException(
            status_code=422,
            detail="This document type is kept in Received for review and cannot be booked.",
        )
    document = state.get("document") or {}
    if (reason := unrepresentable(document, target)) is not None:
        raise HTTPException(status_code=422, detail=reason)
    fields = {k: v for k, v in managed_fields(document, target).items() if v is not None}
    common = {"status": "draft", "source_received_id": rid, "idempotency_key": f"book:{rid}:{company_id}"}
    if target.kind == "list":
        created = await create_list(
            ListCreatePayload(list_type=target.type, **common, **fields),
            company_id, None, role, settings, user, session,
        )
    else:
        created = await create_doc(
            DocCreatePayload(doc_type=target.type, **common, **fields),
            company_id, None, role, settings, user, session,
        )
    await _emit(session, company_id, rid, "received_doc.booked", {
        "target_id": created["id"],
        "target_kind": target.kind,
        "target_type": target.type,
        "revision_seq": state["current_seq"],
        "revision_digest": state["current_digest"],
        "target_version": created["event_id"],
    }, user, f"{rid}:booked:{company_id}")
    await session.commit()
    return {"id": created["id"], "kind": target.kind}


_NOT_DRAFT = "The booked document is no longer a draft. The new revision is kept here for review."
_NOT_UPDATABLE = {
    NEEDS_RECONCILIATION: "The draft was edited here, so the new revision has to be reconciled by hand.",
    SOURCE_CHANGED: (
        "The new revision changed its type, currency or amounts in a way the draft cannot follow, "
        "so it has to be reconciled by hand."
    ),
    REVIEW_ONLY: _NOT_DRAFT,
}


async def update_draft(session: AsyncSession, company_id, rid: str, *, role: str, settings: dict, user) -> dict:
    """Apply the latest received revision to the untouched draft booked from it.

    The patch is a compare-and-set against the version recorded at booking or
    at the last update, so a local edit landing at the same moment wins and
    this call is rejected. If the patch committed but recording it did not,
    a retry finds the patch by its idempotency key and records that version."""
    from celerp_docs.routes import DocPatch, patch_doc, patch_list

    state = (await _received_row(session, company_id, rid)).state or {}
    booked = state.get("booked")
    if not booked:
        raise HTTPException(status_code=409, detail="Book this document first.")
    result = {"id": booked["target_id"], "kind": booked["target_kind"]}
    seq, digest = state["current_seq"], state["current_digest"]
    if booked["revision_digest"] == digest:
        return result
    patch_key = f"{rid}:update:{seq}:{company_id}"
    applied = await find_event_by_idempotency(session, company_id, patch_key)
    if applied is not None:
        version = applied.id
    else:
        target = await _fresh(session, company_id, booked["target_id"])
        status = revision_state(state, target)
        if status != UPDATE_AVAILABLE:
            raise HTTPException(status_code=409, detail=_NOT_UPDATABLE.get(status, "Book this document first."))
        kind = BookTarget(booked["target_kind"], booked["target_type"])
        wanted = managed_fields(state.get("document") or {}, kind)
        current = target.state or {}
        changed = {k: {"old": current.get(k), "new": v} for k, v in wanted.items() if current.get(k) != v}
        version = booked["target_version"]
        if changed:
            patch = patch_list if kind.kind == "list" else patch_doc
            patched = await patch(
                booked["target_id"],
                DocPatch(fields_changed=changed, idempotency_key=patch_key, expected_version=version),
                company_id, None, role, settings, user, session,
            )
            version = patched["event_id"] or version
    await _emit(session, company_id, rid, "received_doc.draft_updated",
                {"revision_seq": seq, "revision_digest": digest, "target_version": version},
                user, f"{rid}:draft_updated:{seq}:{company_id}")
    await session.commit()
    return result


async def mark_reconciled(session: AsyncSession, company_id, rid: str, *, user) -> dict:
    """Record that the booked draft was brought in line with the current
    revision by hand. The draft itself is not touched: what is recorded is the
    revision it now stands for and its version at this moment, so a later
    sender revision is offered as a normal update again."""
    state = (await _received_row(session, company_id, rid)).state or {}
    booked = state.get("booked")
    if not booked:
        raise HTTPException(status_code=409, detail="Book this document first.")
    result = {"id": booked["target_id"], "kind": booked["target_kind"]}
    seq, digest = state["current_seq"], state["current_digest"]
    if booked["revision_digest"] == digest:
        return result
    target = await _fresh(session, company_id, booked["target_id"])
    if target is None or (target.state or {}).get("status") != "draft":
        raise HTTPException(status_code=409, detail=_NOT_DRAFT)
    await _emit(session, company_id, rid, "received_doc.reconciled",
                {"revision_seq": seq, "revision_digest": digest, "target_version": target.version},
                user, f"{rid}:reconciled:{seq}:{target.version}:{company_id}")
    await session.commit()
    return result


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _target_summary(target: BookTarget | None) -> dict | None:
    return {"kind": target.kind, "type": target.type} if target is not None else None


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
        "book_target": _target_summary(BOOK_TARGETS.get(s.get("doc_type") or "")),
        "booked_id": (s.get("booked") or {}).get("target_id"),
        "booked_kind": (s.get("booked") or {}).get("target_kind"),
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


@router.post("/docs/received/{rid}/mark-reconciled")
async def mark_received_reconciled(
    rid: str,
    company_id: _uuid.UUID = Depends(get_current_company_id),
    _: None = require_permission("edit_documents"),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await mark_reconciled(session, company_id, rid, user=user)
