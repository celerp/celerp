# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Canonical external identities for restored company records."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company

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


@dataclass(frozen=True)
class PeerRevisionClaim:
    mapping: CompanyPeerIdentity | None
    target_id: str
    source_key: str
    digest: str
    replay: bool


async def claim_peer_revision(
    session: AsyncSession, *, grant, entity_type: str,
    source_entity_id: str, source_revision: int, data: dict,
) -> PeerRevisionClaim:
    """Serialize a source revision and verify its mapped target before acknowledging replay."""
    if (entity_type not in ENTITY_TYPES or type(source_revision) is not int
            or not 1 <= source_revision <= 2**63 - 1
            or not isinstance(source_entity_id, str)
            or not 1 <= len(source_entity_id) <= 512):
        raise HTTPException(422, "Invalid source revision")
    company_id, source_company = grant.company_id, grant.source_company_id
    company = await locked_company(session, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(403, "Company unavailable")
    source_key = _digest(source_entity_id)
    digest = hashlib.sha256(json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    mapping = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == company_id,
        CompanyPeerIdentity.source_company_id == source_company,
        CompanyPeerIdentity.entity_type == entity_type,
        CompanyPeerIdentity.source_key_hash == source_key,
    ).with_for_update())
    if mapping is not None:
        if mapping.source_entity_id != source_entity_id:
            raise HTTPException(409, "External identity conflict")
        target = await peer_identity(
            session, target_company=company_id, source_company=source_company,
            entity_type=entity_type, source_entity_id=source_entity_id)
        if entity_type == "contact":
            row = await session.get(Projection, (company_id, target))
            if (row.state or {}).get("is_self"):
                raise HTTPException(403, "Company-owned contacts cannot be updated by an integration")
        if source_revision < mapping.last_revision:
            return PeerRevisionClaim(mapping, target, source_key, digest, True)
        if source_revision == mapping.last_revision:
            if mapping.last_payload_hash and mapping.last_payload_hash != digest:
                raise HTTPException(409, "Source revision already used")
            return PeerRevisionClaim(mapping, target, source_key, digest, True)
        return PeerRevisionClaim(mapping, target, source_key, digest, False)
    if entity_type == "doc":
        existing_source = await session.scalar(select(CompanyPeerIdentity.id).where(
            CompanyPeerIdentity.company_id == company_id,
            CompanyPeerIdentity.source_company_id == source_company,
            CompanyPeerIdentity.entity_type == "doc",
        ).limit(1))
        if existing_source is None:
            existing_documents = await session.scalar(select(Projection.entity_id).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            ).limit(1))
            if existing_documents is not None:
                raise HTTPException(
                    409, "Receiving company already contains documents. Use a new company "
                         "or a company copy with source identities preserved before "
                         "starting financial-document synchronization.")
    target = new_target_identity(source_company, entity_type, source_entity_id)
    if await session.get(Projection, (company_id, target)) is not None:
        raise HTTPException(409, "External identity conflicts with an existing record")
    return PeerRevisionClaim(None, target, source_key, digest, False)


async def commit_peer_revision(
    session: AsyncSession, *, grant, claim: PeerRevisionClaim,
    entity_type: str, source_entity_id: str, source_revision: int,
) -> None:
    """Persist source revision in the same transaction as its canonical domain write."""
    if claim.mapping is None:
        session.add(CompanyPeerIdentity(
            company_id=grant.company_id, source_company_id=grant.source_company_id,
            entity_type=entity_type, source_entity_id=source_entity_id,
            source_key_hash=claim.source_key, target_entity_id=claim.target_id,
            last_revision=source_revision, last_payload_hash=claim.digest))
    else:
        claim.mapping.last_revision = source_revision
        claim.mapping.last_payload_hash = claim.digest
    await session.flush()


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
    claim = await claim_peer_revision(
        session, grant=grant, entity_type="contact",
        source_entity_id=source_entity_id, source_revision=source_revision, data=data)
    if claim.replay:
        return "skipped"
    company_id, source_company = grant.company_id, grant.source_company_id
    source_key, target_entity_id = claim.source_key, claim.target_id
    record = CRMImportRecord(
        entity_id=target_entity_id, event_type="crm.contact.created", data=data,
        source="connector", idempotency_key=f"peer:contact:{source_company}:{source_key}")
    async with session.begin_nested():
        results = await import_contact_records(
            session, company_id=company_id, actor_id=None,
            records=[record], match_identity=False, allow_null_updates=True)
        result = results.records[0]
        if result.status not in ("created", "updated", "skipped"):
            raise HTTPException(409, "Contact update was not accepted")
    await commit_peer_revision(
        session, grant=grant, claim=claim, entity_type="contact",
        source_entity_id=source_entity_id, source_revision=source_revision)
    return result.status



async def receive_product(session: AsyncSession, *, grant, source_entity_id: str,
                          source_revision: int, data: dict) -> str:
    """Apply one product through the ordinary inventory importer, without stock movements."""
    from types import SimpleNamespace
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User
    from celerp_inventory.services import BatchImportRequest, ImportRecord, write_import_batch

    if (type(source_revision) is not int or not 1 <= source_revision <= 2**63 - 1
            or not isinstance(source_entity_id, str) or not 1 <= len(source_entity_id) <= 512):
        raise HTTPException(422, "Invalid product revision")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(422, "Product name required")
    # Product/catalog changes do not represent stock movements or expense postings.
    allowed = {"name", "sku", "description", "sell_by", "category",
               "barcode", "retail_price", "wholesale_price"}
    if set(data) - allowed:
        raise HTTPException(422, "Unsupported product fields")
    body = {key: value for key, value in data.items() if value is not None}
    claim = await claim_peer_revision(
        session, grant=grant, entity_type="item",
        source_entity_id=source_entity_id, source_revision=source_revision, data=data)
    if claim.replay:
        return "skipped"
    company_id, source_company = grant.company_id, grant.source_company_id
    target_id, key = claim.target_id, claim.source_key
    event_type = "item.patched" if claim.mapping is not None else "item.created"
    if claim.mapping is not None:
        for field in ("sku", "description", "category", "barcode",
                      "retail_price", "wholesale_price"):
            if field in data and data[field] is None:
                body[field] = None
    company = await session.get(Company, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(403, "Company unavailable")
    member = (await session.execute(select(User.id, UserCompany.role).join(
        UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == company_id,
        UserCompany.role == "owner",
        UserCompany.is_active.is_(True),
        User.is_active.is_(True),
    ).order_by(User.id).limit(1))).first()
    if member is None:
        raise HTTPException(403, "Company owner unavailable")
    record = ImportRecord(
        entity_id=target_id, event_type=event_type, data=body,
        source="connector",
        idempotency_key=f"peer:item:{source_company}:{key}:{source_revision}",
    )
    async with session.begin_nested():
        outcome, _batch = await write_import_batch(
            session, company_id, SimpleNamespace(id=member.id), member.role,
            dict(company.settings or {}), BatchImportRequest(records=[record]))
        result = outcome.records[0]
        if result.status not in ("created", "updated", "skipped"):
            raise HTTPException(409, "Product update was not accepted")
    await commit_peer_revision(
        session, grant=grant, claim=claim, entity_type="item",
        source_entity_id=source_entity_id, source_revision=source_revision)
    return result.status
