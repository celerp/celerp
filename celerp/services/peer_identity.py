# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Canonical external identities for restored company records."""
from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection

ENTITY_TYPES = frozenset({"contact", "item", "doc"})
BATCH = 500


def _digest(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


async def seed_restored_identities(session: AsyncSession, *, target_company: uuid.UUID,
                                    source_company: uuid.UUID, id_map: dict[str, str]) -> int:
    """Use the backup's existing remap within the restore transaction."""
    old_id = {new: old for old, new in id_map.items() if new != old}
    last = None
    count = 0
    while True:
        query = select(Projection.entity_id, Projection.entity_type, Projection.version).where(
            Projection.company_id == target_company,
            Projection.entity_type.in_(ENTITY_TYPES),
        )
        if last is not None:
            query = query.where(Projection.entity_id > last)
        rows = (await session.execute(query.order_by(Projection.entity_id).limit(BATCH))).all()
        if not rows:
            break
        values = []
        for entity_id, entity_type, version in rows:
            source_entity_id = old_id.get(entity_id, entity_id)
            values.append({
                "company_id": target_company, "source_company_id": source_company,
                "entity_type": entity_type, "source_entity_id": source_entity_id,
                "source_key_hash": _digest(source_entity_id),
                "target_entity_id": entity_id,
                "last_revision": version,
            })
        await session.execute(insert(CompanyPeerIdentity), values)
        count += len(values)
        last = rows[-1][0]
    return count


async def peer_identity(session: AsyncSession, *, target_company: uuid.UUID,
                        source_company: uuid.UUID, entity_type: str, source_entity_id: str) -> str | None:
    if entity_type not in ENTITY_TYPES or not isinstance(source_entity_id, str) or not source_entity_id:
        raise HTTPException(422, "Invalid external identity")
    row = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == target_company,
        CompanyPeerIdentity.source_company_id == source_company,
        CompanyPeerIdentity.entity_type == entity_type,
        CompanyPeerIdentity.source_key_hash == _digest(source_entity_id),
    ))
    if row is None:
        return None
    if row.source_entity_id != source_entity_id:
        raise HTTPException(409, "External identity conflict")
    other = await session.get(Projection, (target_company, row.target_entity_id))
    if other is None or other.entity_type != entity_type:
        raise HTTPException(409, "External identity no longer matches its record")
    return row.target_entity_id


def new_target_identity(source_company: uuid.UUID, entity_type: str, source_entity_id: str) -> str:
    if entity_type not in ENTITY_TYPES or not source_entity_id or len(source_entity_id) > 512:
        raise HTTPException(422, "Invalid external identity")
    target = uuid.uuid5(uuid.NAMESPACE_URL, f"celerp-peer:{source_company}:{entity_type}:{source_entity_id}")
    return f"{entity_type}:{target}"


async def receive_contact(session: AsyncSession, *, grant, source_entity_id: str,
                          source_revision: int, data: dict) -> str:
    """Write one contact through the canonical contact importer in this transaction."""
    from celerp.services.currencies import require_currency_code, require_phone
    from celerp_contacts.services import CRMImportRecord, import_contact_records

    if (not isinstance(source_revision, int) or isinstance(source_revision, bool) or
            not 1 <= source_revision <= 2**63 - 1 or
            not isinstance(source_entity_id, str) or not 1 <= len(source_entity_id) <= 512):
        raise HTTPException(422, "Invalid contact revision")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise HTTPException(422, "Invalid contact name")
    require_currency_code(data.get("currency"))
    require_phone(data.get("phone"))
    company_id, source_company = grant.company_id, grant.source_company_id
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=False).encode()).hexdigest()
    source_key = _digest(source_entity_id)
    mapping = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == company_id,
        CompanyPeerIdentity.source_company_id == source_company,
        CompanyPeerIdentity.entity_type == "contact",
        CompanyPeerIdentity.source_key_hash == source_key,
    ))
    if mapping is not None:
        if mapping.source_entity_id != source_entity_id:
            raise HTTPException(409, "External contact identity conflict")
        if source_revision < mapping.last_revision:
            return "skipped"
        if source_revision == mapping.last_revision:
            if mapping.last_payload_hash and mapping.last_payload_hash != digest:
                raise HTTPException(409, "Contact revision already used")
            return "skipped"
        target_entity_id = await peer_identity(session, target_company=company_id,
                                              source_company=source_company, entity_type="contact",
                                              source_entity_id=source_entity_id)
    else:
        target_entity_id = new_target_identity(source_company, "contact", source_entity_id)
        existing = await session.get(Projection, (company_id, target_entity_id))
        if existing is not None:
            raise HTTPException(409, "External contact identity conflicts with an existing record")
    record = CRMImportRecord(
        entity_id=target_entity_id, event_type="crm.contact.created", data=data,
        source="connector", idempotency_key=f"peer:contact:{source_company}:{source_key}")
    async with session.begin_nested():
        results = await import_contact_records(
            session, company_id=company_id, actor_id=None,
            records=[record], match_identity=False)
        result = results.records[0]
        if result.status not in ("created", "updated", "skipped"):
            raise HTTPException(409, "Contact update was not accepted")
    if mapping is None:
        mapping = CompanyPeerIdentity(
            company_id=company_id, source_company_id=source_company, entity_type="contact",
            source_entity_id=source_entity_id, source_key_hash=source_key,
            target_entity_id=target_entity_id, last_revision=source_revision,
            last_payload_hash=digest)
        session.add(mapping)
    else:
        mapping.last_revision = source_revision
        mapping.last_payload_hash = digest
    await session.flush()
    return result.status
