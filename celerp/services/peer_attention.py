# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Records a connected company sent that the receiving company did not apply. One row per
record stays open, with its latest reason, until the record is applied."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company_peer import CompanyPeerAttention

REFUSED = "refused"
# A journal of the receiving company's that the connected company declined to apply, and
# one the connected company reversed.
DECLINED = "declined"
REVERSAL = "reversal"


async def record(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                 entity_type: str, source_entity_id: str, label: str, reason: str,
                 kind: str = REFUSED) -> None:
    """Open the record's item, or replace its label and reason when it is already open."""
    values = dict(company_id=company_id, source_company_id=source_company_id, kind=kind,
                  entity_type=entity_type, source_entity_id=source_entity_id,
                  label=label, reason=reason, created_at=datetime.now(timezone.utc))
    await session.execute(
        pg_insert(CompanyPeerAttention).values(id=uuid.uuid4(), **values)
        .on_conflict_do_update(constraint="uq_company_peer_attention_record",
                               set_={"label": label, "reason": reason,
                                     "created_at": values["created_at"]}))


async def clear(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                entity_type: str, source_entity_id: str, kind: str = REFUSED) -> None:
    """The record was applied: its item closes."""
    await session.execute(delete(CompanyPeerAttention).where(
        CompanyPeerAttention.company_id == company_id,
        CompanyPeerAttention.source_company_id == source_company_id,
        CompanyPeerAttention.kind == kind,
        CompanyPeerAttention.entity_type == entity_type,
        CompanyPeerAttention.source_entity_id == source_entity_id))


async def open_items(session: AsyncSession, company_ids: list[uuid.UUID]) -> list[CompanyPeerAttention]:
    """Every open item of these companies, newest first."""
    if not company_ids:
        return []
    return list((await session.scalars(select(CompanyPeerAttention).where(
        CompanyPeerAttention.company_id.in_(company_ids))
        .order_by(CompanyPeerAttention.created_at.desc()))).all())
