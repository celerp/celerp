# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Records a connected company sent that the receiving company did not apply. One row per
record stays open, with its latest reason, until the record is applied. The reason is
stored as its keyed refusal (celerp.accounting_roles.refusal) in JSON, so each reader
reads it in their own language."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.json_text import dump_json
from celerp.models.company_peer import CompanyPeerAttention
from celerp.services.company_lock import lock_company

REFUSED = "refused"
# A journal of the receiving company's that the connected company declined to apply, and
# one the connected company reversed.
DECLINED = "declined"
REVERSAL = "reversal"
# An account sync added to the receiving company's chart with a record that needed it,
# open until the receiving company keeps, archives or removes it.
ADDED = "added"


async def record(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                 entity_type: str, source_entity_id: str, label: str, reason: dict,
                 kind: str = REFUSED, fix: dict | None = None) -> None:
    """Open the record's item, or replace its label, reason (a keyed refusal) and fix when
    it is already open. ``fix`` is what a held record needs to post
    (peer_accounts.AccountHold). Every writer of an item takes the company's lock before it."""
    reason = stored(reason)
    await lock_company(session, company_id)
    values = dict(company_id=company_id, source_company_id=source_company_id, kind=kind,
                  entity_type=entity_type, source_entity_id=source_entity_id,
                  label=label, reason=reason, fix=fix, created_at=datetime.now(timezone.utc))
    await session.execute(
        pg_insert(CompanyPeerAttention).values(id=uuid.uuid4(), **values)
        .on_conflict_do_update(constraint="uq_company_peer_attention_record",
                               set_={"label": label, "reason": reason, "fix": fix,
                                     "created_at": values["created_at"]}))


def stored(reason: dict) -> str:
    """An item's reason as its column holds it: a keyed refusal, never English alone."""
    from celerp.error_body import keyed

    if not keyed(reason) or not isinstance(reason, dict):
        raise TypeError(f"an attention reason is a keyed refusal, not {reason!r}")
    return dump_json(reason)


def reason_of(item: CompanyPeerAttention) -> dict:
    """An item's reason as the keyed refusal its writer stored. A reason an older release
    wrote as English text is shown as written."""
    from celerp.accounting_roles import catalog_refusal
    from celerp.error_body import keyed

    try:
        reason = json.loads(item.reason)
    except ValueError:
        reason = None
    if isinstance(reason, dict) and keyed(reason):
        return reason
    return catalog_refusal("error.refusal_as_written", text=item.reason)


async def clear(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                entity_type: str, source_entity_id: str, kind: str = REFUSED) -> None:
    """The record was applied: its item closes."""
    await lock_company(session, company_id)
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
