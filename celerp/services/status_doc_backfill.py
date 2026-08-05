# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One-time backfill of the inventory status-to-document pairing.

The status column links to the document behind a doc-driven status (sold, memo,
consignment-in). The link is a projection field, ``status_doc_id`` /
``status_doc_number``, stamped by ``apply_item_event`` when the status change
carries its source document. Items whose status was set before that field shipped
never received it, and a release-to-release upgrade does not rebuild projections
(``dev_release_guard``), so the status column shows the bare status with no link
for pre-existing sold, memo, and consigned items.

This replays each affected item's own events through the same handler the live
system uses (the single source of truth), reads the pairing it derives, and
writes back only those two fields. Every other projection field is left exactly
as it stands, so a wrong derivation can at worst leave a status link missing,
which the next status change re-stamps, and can never alter stock, cost, or
document state. Runs in the lifespan, where the projection handlers are
registered, and is gated by a marker so it runs once per database.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

STATUS_DOC_BACKFILL_KEY = "status_doc_backfill"


def _needs_backfill(state: dict) -> bool:
    """An item that could carry a status document but has none stamped.

    Only items fulfilled onto a document (``fulfilled_for_docs``) or held on
    consignment-in can earn the pairing, so this is a strict superset of the
    affected items rather than a full-catalog scan.
    """
    if state.get("status_doc_id"):
        return False
    return bool(state.get("fulfilled_for_docs")) or state.get("consignment_flag") == "in"


async def run_status_doc_backfill(session: "AsyncSession") -> dict:
    """Stamp the status-to-document pairing on pre-existing items. Caller owns txn.

    Idempotent and marker-gated: sets the marker once run so later boots skip.
    Returns a summary; a genuine error propagates to the lifespan's non-fatal
    guard rather than being swallowed here.
    """
    from sqlalchemy import select

    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, STATUS_DOC_BACKFILL_KEY)):
        return {"changed": False, "stamped": 0}

    stamped = 0
    projections = (
        await session.execute(select(Projection).where(Projection.entity_type == "item"))
    ).scalars().all()
    for proj in projections:
        state = proj.state or {}
        if not _needs_backfill(state):
            continue
        # Replay this item's own events through the live handler to derive the
        # pairing exactly as a fresh fulfilment would, with no parallel logic.
        events = (
            await session.execute(
                select(LedgerEntry)
                .where(
                    LedgerEntry.company_id == proj.company_id,
                    LedgerEntry.entity_id == proj.entity_id,
                )
                .order_by(LedgerEntry.id.asc())
            )
        ).scalars().all()
        replayed: dict = {}
        for entry in events:
            replayed = ProjectionEngine._apply(replayed, entry.event_type, entry.data)
        doc_id = replayed.get("status_doc_id")
        if not doc_id:
            continue
        # Write back only the two pairing fields; leave every other live field as
        # it is, so the backfill cannot change anything but the status link.
        new_state = dict(state)
        new_state["status_doc_id"] = doc_id
        new_state["status_doc_number"] = replayed.get("status_doc_number") or ""
        proj.state = new_state
        stamped += 1

    await conn.run_sync(lambda c: set_meta(c, STATUS_DOC_BACKFILL_KEY, "done"))
    if stamped:
        log.info("Status-doc backfill: stamped %d inventory item(s)", stamped)
    return {"changed": True, "stamped": stamped}
