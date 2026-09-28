# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time move of earlier imports into Received.

Imports used to land as documents of our own (``doc.shared_import``), which put
another business's invoice among ours. An import nobody has touched since is
moved: its single ledger event is rewritten as a ``received_doc.imported``
event and its projection is replaced, so a rebuild from the ledger gives the
same result. An import that has any later activity (edited, converted, paid,
voided, a note or a file added, or referenced from another record) is part of
our books by now and stays a document.

Earlier imports carry no sender identity, so each becomes its own received
record keyed by its old document id. Runs in the lifespan and is gated by a
marker so it runs once per database.
"""

from __future__ import annotations

import logging

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

LEGACY_RECEIVED_KEY = "received_legacy_imports"
LEGACY_INSTALLATION = "legacy"
_LEGACY_ONLY_FIELDS = ("source_share_token", "source_origin", "status")


def legacy_received_data(data: dict, old_id: str, received_at: str | None) -> dict:
    """The received_doc.imported payload for an earlier doc.shared_import."""
    from celerp_docs.received import document_digest

    document = {k: v for k, v in (data or {}).items() if k not in _LEGACY_ONLY_FIELDS}
    token, origin = data.get("source_share_token"), data.get("source_origin")
    return {
        "source_installation": LEGACY_INSTALLATION,
        "source_document": old_id,
        "source_revision": None,
        "source_link": f"{origin.rstrip('/')}/share/{token}" if token and origin else None,
        "digest": document_digest(document),
        "received_at": received_at,
        "document": document,
    }


async def move_legacy_imports(session: AsyncSession) -> dict:
    """Move untouched earlier imports into Received. Caller owns the transaction."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp_docs.received import ENTITY_TYPE, received_id, revision_key

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, LEGACY_RECEIVED_KEY)):
        return {"changed": False, "moved": 0}

    imported = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.event_type == "doc.shared_import")
    )).scalars().all()
    moved = 0
    for entry in imported:
        # Notes, journal entries and conversions sit on their own entities and
        # name the document in their data, so they count as activity too.
        events = (await session.execute(
            select(func.count()).select_from(LedgerEntry).where(
                LedgerEntry.company_id == entry.company_id,
                or_(
                    LedgerEntry.entity_id == entry.entity_id,
                    cast(LedgerEntry.data, Text).like(f'%"{entry.entity_id}"%'),
                ),
            )
        )).scalar_one()
        if events != 1:
            continue
        old_id = entry.entity_id
        rid = received_id(LEGACY_INSTALLATION, "", old_id)
        data = legacy_received_data(entry.data, old_id, entry.ts.isoformat() if entry.ts else None)
        entry.entity_id = rid
        entry.entity_type = ENTITY_TYPE
        entry.event_type = "received_doc.imported"
        entry.data = data
        entry.idempotency_key = revision_key(rid, data["digest"], data.get("source_link"), entry.company_id)
        old = await session.get(Projection, (entry.company_id, old_id))
        if old is not None:
            await session.delete(old)
        await session.flush()
        await ProjectionEngine.apply_event(session, entry)
        moved += 1

    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, "done"))
    if moved:
        log.info("Moved %d earlier import(s) into Received", moved)
    return {"changed": True, "moved": moved}


async def move_legacy_imports_hook(session: AsyncSession) -> None:
    await move_legacy_imports(session)
