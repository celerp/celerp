# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Reconcile shared stock counts through the existing inventory audit lifecycle."""
from __future__ import annotations

import hashlib
import math
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.peer_identity import (
    _digest, claim_peer_revision, commit_peer_revision, peer_identity, resolve_peer_location,
)

_FIELDS = frozenset({"location_id", "location_name", "location_type", "counts",
                     "source_closed_event_id"})


async def receive_stock_audit(
    session: AsyncSession, *, grant, source_entity_id: str,
    source_revision: int, data: dict,
) -> str:
    """Apply one completed physical count and its journal atomically.

    Documents are handled first by the connector. The canonical audit computes
    any remaining stock variance, including differences caused by excluded sales.
    """
    from celerp_docs.routes import (
        AuditCreateBody, _set_list_fields, create_audit_list_record,
        finalize_list_record, adjust_audit_record,
    )

    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise HTTPException(422, "Invalid stock count")
    source_location = data.get("location_id")
    if (not isinstance(source_location, str)
            or not source_entity_id.startswith("list:")):
        raise HTTPException(422, "Invalid stock count identity")
    source_position = data.get("source_closed_event_id")
    if type(source_position) is not int or not 1 <= source_position <= 2**63 - 1:
        raise HTTPException(422, "Invalid source stock audit position")
    source_counts = data.get("counts")
    if not isinstance(source_counts, list) or not 1 <= len(source_counts) <= 1000:
        raise HTTPException(422, "Invalid stock count entries")
    counts: dict[str, float] = {}
    for record in source_counts:
        if not isinstance(record, dict) or set(record) != {"item_id", "counted_qty"}:
            raise HTTPException(422, "Invalid stock count entry")
        item_id, quantity = record["item_id"], record["counted_qty"]
        if (not isinstance(item_id, str) or not 1 <= len(item_id) <= 512
                or isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                or not math.isfinite(quantity) or quantity < 0
                or item_id in counts):
            raise HTTPException(422, "Invalid stock count quantity or item")
        counts[item_id] = float(quantity)
    if len(str(data)) > 200_000:
        raise HTTPException(413, "Stock count exceeds integration size limit")

    claim = await claim_peer_revision(
        session, grant=grant, entity_type="list",
        source_entity_id=source_entity_id, source_revision=source_revision, data=data)
    if claim.replay:
        return "skipped"

    # Serialize all completed counts from the same source location using the
    # durable peer identity ledger. Older source counts must never overwrite a
    # newer stock adjustment, even if delivery was interrupted or reordered.
    cursor = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == grant.company_id,
        CompanyPeerIdentity.source_company_id == grant.source_company_id,
        CompanyPeerIdentity.entity_type == "audit_cursor",
        CompanyPeerIdentity.source_key_hash == _digest(source_location),
    ).with_for_update())
    if cursor is not None:
        if cursor.source_entity_id != source_location:
            raise HTTPException(409, "Stock audit cursor identity conflict")
        if (source_position < cursor.last_revision
                or (source_position == cursor.last_revision
                    and cursor.last_payload_hash != _digest(source_entity_id))):
            raise HTTPException(409, "Stock audit is older than the last applied count")

    location_id = await resolve_peer_location(
        session, grant=grant, source_id=source_location,
        name=data["location_name"], location_type=data["location_type"])
    if cursor is not None and cursor.target_entity_id != location_id:
        raise HTTPException(409, "Stock audit location identity changed")
    mapped: dict[str, float] = {}
    for source_id, quantity in counts.items():
        target_id = await peer_identity(
            session, target_company=grant.company_id,
            source_company=grant.source_company_id,
            entity_type="item", source_entity_id=source_id)
        if target_id is None or target_id in mapped:
            raise HTTPException(409, "Stock count item must be synchronized uniquely")
        mapped[target_id] = quantity

    async def complete_revision() -> None:
        await commit_peer_revision(
            session, grant=grant, claim=claim, entity_type="list",
            source_entity_id=source_entity_id, source_revision=source_revision)
        nonlocal cursor
        if cursor is None:
            cursor = CompanyPeerIdentity(
                company_id=grant.company_id,
                source_company_id=grant.source_company_id,
                entity_type="audit_cursor", source_entity_id=source_location,
                source_key_hash=_digest(source_location), target_entity_id=location_id,
                last_revision=source_position,
                last_payload_hash=_digest(source_entity_id),
            )
            session.add(cursor)
        elif source_position > cursor.last_revision:
            cursor.last_revision = source_position
            cursor.last_payload_hash = _digest(source_entity_id)
        await session.flush()

    company = await session.get(Company, grant.company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(403, "Company unavailable")
    owner = (await session.execute(select(User.id, UserCompany.role).join(
        UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == grant.company_id,
        UserCompany.role == "owner", UserCompany.is_active.is_(True),
        User.is_active.is_(True)).order_by(User.id).limit(1))).first()
    if owner is None:
        raise HTTPException(403, "Company owner unavailable")
    actor = SimpleNamespace(id=owner.id)
    cid = str(grant.company_id)
    target = await session.get(Projection, (grant.company_id, claim.target_id),
                               with_for_update=True)
    if target is not None:
        if target.entity_type != "list" or (target.state or {}).get("list_type") != "audit":
            raise HTTPException(409, "Stock audit identity conflicts with existing record")
        state = target.state or {}
        if state.get("status") != "closed" or state.get("result") != "stock_adjusted":
            raise HTTPException(409, "Stock audit requires manual reconciliation")
        existing = {str(line.get("item_id")): float(line["counted_qty"])
                    for line in state.get("line_items", [])
                    if isinstance(line, dict) and line.get("counted_qty") is not None}
        if existing != mapped or str(state.get("location_id")) != location_id:
            raise HTTPException(409, "Completed stock count cannot be silently rewritten")
        await complete_revision()
        return "skipped"

    key = hashlib.sha256(
        (str(grant.source_company_id) + ":" + source_entity_id).encode()).hexdigest()
    await create_audit_list_record(
        AuditCreateBody(location_id=location_id, idempotency_key="peer:audit:" + key),
        cid, actor, session, commit=False, entity_id_override=claim.target_id)
    await finalize_list_record(claim.target_id, cid, actor, session, commit=False)
    row = await session.get(Projection, (grant.company_id, claim.target_id),
                            populate_existing=True)
    if row is None or row.state.get("status") != "finalized":
        raise HTTPException(409, "Stock audit could not be finalized")
    lines = [dict(line) for line in (row.state.get("line_items") or [])]
    found = set()
    for line in lines:
        item_id = str(line.get("item_id") or "")
        if item_id in mapped:
            line["counted_qty"] = mapped[item_id]
            found.add(item_id)
    if found != set(mapped):
        raise HTTPException(409, "Stock audit items are missing from their mapped location")
    await _set_list_fields(
        session, grant.company_id, claim.target_id, actor, {"line_items": lines})
    await adjust_audit_record(claim.target_id, cid, actor, session, commit=False)
    await complete_revision()
    return "created"
