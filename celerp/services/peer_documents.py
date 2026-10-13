# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Apply peer documents through the canonical import and lifecycle services."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.peer_identity import (
    claim_peer_revision, commit_peer_revision, peer_identity,
)

# The source may supply only document inputs. No lifecycle, posting, payment,
# stock, attachment, or destination-owned fields can cross this path.
_FIELDS = frozenset({
    "doc_type", "ref_id", "status", "contact_id", "contact_name",
    "line_items", "currency", "conversion_rate", "issue_date", "due_date",
    "payment_terms", "notes", "reference", "doc_taxes", "discount",
    "shipping", "subtotal", "total", "tax", "terms_text", "customer_note",
    "purchase_kind", "discount_type",
})
_SOURCE_ITEM_KEYS = ("item_id", "entity_id")


async def _map_identity(session, grant, entity_type: str, source_id: str) -> str:
    mapped = await peer_identity(
        session, target_company=grant.company_id,
        source_company=grant.source_company_id,
        entity_type=entity_type, source_entity_id=source_id)
    if mapped is None:
        raise HTTPException(409, "Source document dependency is not synchronized")
    return mapped


async def receive_draft_document(
    session: AsyncSession, *, grant, source_entity_id: str,
    source_revision: int, data: dict,
) -> str:
    """Create or edit a draft without bypassing canonical status and money checks."""
    from celerp_docs.doc_constants import LIFECYCLE_OWNED_FIELDS
    from celerp_docs.import_service import import_doc_records
    from celerp_docs.routes import DocImportRecord, finalize_document
    from celerp_docs.sequences import require_doc_type

    if not isinstance(data, dict):
        raise HTTPException(422, "Invalid document")
    if set(data) - _FIELDS or set(data) & (LIFECYCLE_OWNED_FIELDS - {"status"}):
        raise HTTPException(422, "Unapproved document fields")
    if (data.get("doc_type") is None or not isinstance(data.get("line_items", []), list)
            or len(data.get("line_items", [])) > 200):
        raise HTTPException(422, "Invalid document")
    if len(json.dumps(data, separators=(",", ":"))) > 1_000_000:
        raise HTTPException(413, "Document exceeds integration size limit")
    try:
        require_doc_type(data["doc_type"])
    except (ValueError, TypeError):
        raise HTTPException(422, "Unsupported document type") from None
    claim = await claim_peer_revision(
        session, grant=grant, entity_type="doc",
        source_entity_id=source_entity_id, source_revision=source_revision,
        data=data)
    if claim.replay:
        return "skipped"
    source_status = data.get("status", "draft")
    if source_status not in (
        "draft", "final", "sent", "partial", "paid", "awaiting_payment",
    ):
        raise HTTPException(409, "Document lifecycle requires reconciliation")
    if source_status != "draft" and data["doc_type"] not in ("invoice", "bill"):
        raise HTTPException(409, "Unsupported issued-document type")

    incoming = dict(data)
    # Settlement and stock movement belong to separate domain operations.
    incoming["status"] = "draft" if source_status == "draft" else "final"
    if source_status != "draft" and data["doc_type"] == "bill":
        incoming["import_treatment"] = "record_now"
    source_contact = incoming.get("contact_id")
    if source_contact:
        if not isinstance(source_contact, str):
            raise HTTPException(422, "Invalid contact identity")
        incoming["contact_id"] = await _map_identity(session, grant, "contact", source_contact)
    mapped_lines = []
    for line in incoming.get("line_items", []):
        if not isinstance(line, dict):
            raise HTTPException(422, "Invalid document line")
        moved = dict(line)
        for key in _SOURCE_ITEM_KEYS:
            if moved.get(key):
                if not isinstance(moved[key], str):
                    raise HTTPException(422, "Invalid item reference")
                if key == "item_id" or moved[key].startswith("item:"):
                    moved[key] = await _map_identity(session, grant, "item", moved[key])
        mapped_lines.append(moved)
    if "line_items" in incoming:
        incoming["line_items"] = mapped_lines

    company = await session.get(Company, grant.company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(403, "Company unavailable")
    member = (await session.execute(select(User.id, UserCompany.role).join(
        UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == grant.company_id,
        UserCompany.role == "owner", UserCompany.is_active.is_(True),
        User.is_active.is_(True),
    ).order_by(User.id).limit(1))).first()
    if member is None:
        raise HTTPException(403, "Company owner unavailable")
    key = hashlib.sha256(source_entity_id.encode("utf-8")).hexdigest()
    record = DocImportRecord(
        entity_id=claim.target_id, event_type="doc.created",
        data=incoming, source="connector",
        idempotency_key=f"peer:doc:{grant.source_company_id}:{key}",
    )
    from celerp.models.projections import Projection
    target = await session.get(Projection, (grant.company_id, claim.target_id))
    target_status = (target.state or {}).get("status") if target is not None else None
    if source_status == "draft" and target_status not in (None, "draft"):
        raise HTTPException(409, "Previously issued document requires reconciliation")
    if source_status != "draft" and target_status == "draft":
        # Update its draft fields before the canonical finalization posts the books.
        incoming["status"] = "draft"
        incoming.pop("import_treatment", None)
        record.data = incoming
    outcome = await import_doc_records(
        session, grant.company_id, SimpleNamespace(id=member.id), member.role,
        dict(company.settings or {}), [record], upsert=True, post_ledger=True)
    if len(outcome.records) != 1 or outcome.records[0].status not in (
        "created", "updated", "skipped",
    ):
        raise HTTPException(409, "Receiving document needs review")
    if source_status != "draft" and target_status == "draft":
        await finalize_document(claim.target_id, str(grant.company_id),
                                SimpleNamespace(id=member.id), session, commit=False)
    await commit_peer_revision(
        session, grant=grant, claim=claim, entity_type="doc",
        source_entity_id=source_entity_id, source_revision=source_revision)
    return outcome.records[0].status
