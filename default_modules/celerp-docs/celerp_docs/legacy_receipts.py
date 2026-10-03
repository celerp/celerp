# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time fill-in for documents whose goods came in before receipts were recorded.

A receipt now records the status it found the document in (``pre_receipt_status``), so
undoing it can return a draft bill to draft. Projections written by an earlier release
lack that record, and some also lack the ``finalized`` flag the undo reads with it. Both
are taken from the document's own ledger, replayed through the document projection the
same way a rebuild would; nothing else on the projection changes. A document whose
ledger records no pre-receipt status is left as it is.

An earlier release also marked a never-issued bill final when its receipt was undone.
Where the ledger says that bill is a draft, its status goes back to draft.

Runs in the lifespan and is gated by a marker so it runs once per database. A company
staged for a migration is skipped and the marker stays unset until it is finished.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

LEGACY_RECEIPTS_KEY = "legacy_receipt_status"
_RECEIPT_EVENTS = ("doc.received", "doc.receive_undone")
_FILLED_FIELDS = ("pre_receipt_status", "finalized")


def _filled_in(state: dict, replayed: dict) -> dict | None:
    """The projection state with what the ledger says was missing, or None if nothing was."""
    after = dict(state)
    for field in _FILLED_FIELDS:
        if field not in after and field in replayed:
            after[field] = replayed[field]
    if after.get("status") == "final" and not after.get("finalized") and replayed.get("status") == "draft":
        after["status"] = "draft"
    return after if after != state else None


async def record_legacy_receipts(session: AsyncSession) -> dict:
    """Fill in receipt records on earlier documents. Caller owns the transaction."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.migrations import is_company_migration_staged

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, LEGACY_RECEIPTS_KEY)):
        return {"changed": False, "filled": 0}

    docs = (await session.execute(
        select(LedgerEntry.company_id, LedgerEntry.entity_id)
        .where(LedgerEntry.event_type.in_(_RECEIPT_EVENTS)).distinct()
    )).all()
    filled, staged = 0, False
    for company_id, entity_id in docs:
        if await is_company_migration_staged(session, company_id):
            staged = True
            continue
        row = (await session.execute(
            select(Projection).where(Projection.company_id == company_id, Projection.entity_id == entity_id)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None or row.entity_type != "doc":
            continue
        replayed: dict = {}
        for entry in (await session.execute(
            select(LedgerEntry).where(LedgerEntry.company_id == company_id, LedgerEntry.entity_id == entity_id)
            .order_by(LedgerEntry.id)
        )).scalars():
            replayed = ProjectionEngine._apply(replayed, entry.event_type, entry.data)
        state = _filled_in(row.state or {}, replayed)
        if state is not None:
            row.state = state
            filled += 1

    if not staged:
        await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIPTS_KEY, "done"))
    if filled:
        log.info("Filled in receipt records on %d earlier document(s)", filled)
    return {"changed": True, "filled": filled}


async def record_legacy_receipts_hook(session: AsyncSession) -> None:
    await record_legacy_receipts(session)
