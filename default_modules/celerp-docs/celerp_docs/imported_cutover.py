# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""One-time correction of purchase orders and bills imported by an earlier release.

The opening balances hold an imported document: the goods received on it are opening
stock and what is owed on it is an opening payable. An earlier release booked the
import again on top of them (a purchase order's whole total as received, a bill's whole
entry), and left the goods received on it with no receipt record, so they could not be
returned. Each such document is brought to where the books would be had it been
imported now (auto_je.correct_earlier_import), its received goods are marked as a
receipt marks them, and the company is told which documents were corrected.

Runs in the lifespan and is gated by a marker so it runs once per database. A company
staged for a migration is skipped, and a document whose entries sit in a locked period
is left for the next start; either way the marker stays unset until all are done.
Every event the correction writes carries auto_je.IMPORTED_CUTOVER in its metadata.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

CUTOVER_KEY = "imported_doc_cutover"
_TITLE = "Imported purchase orders and bills corrected"


_WAITING_TITLE = "Imported purchase orders and bills not corrected yet"
_WAITING = {
    "locked": ("notice.imported_doc_cutover.locked",
               "These purchase orders and bills imported before this update sit in a locked period and "
               "could not be corrected yet: {numbers}. Move the period lock in Settings > Accounting so "
               "an open date exists; the correction runs again at the next start."),
    "failed": ("notice.imported_doc_cutover.failed",
               "These purchase orders and bills imported before this update could not be corrected: "
               "{numbers}. Open each one and check its lines and payments; the correction runs again at "
               "the next start."),
}


def _body(numbers: list[str]) -> str:
    return ("Purchase orders and bills imported before this update were booked a second time on top of "
            "the opening balances, which already hold them. Those entries were reversed, so payables "
            "and stock now match the opening balances plus what happened on each document after it was "
            f"imported. Corrected: {', '.join(numbers)}.")


async def _correct(session: AsyncSession, company_id, doc_id: str) -> bool:
    """Correct one document. Returns whether anything changed."""
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection
    from celerp.services import auto_je
    from celerp_docs.routes import mark_received_goods

    row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    if row is None:
        return False
    state = dict(row.state or {})
    changed = await auto_je.correct_earlier_import(session, company_id=company_id, doc_id=doc_id, doc=state)
    # Goods no line prices stay unmarked, so they cannot be returned on the document.
    marked, _unpriced = await mark_received_goods(session, company_id, state)
    if marked is not state:
        await emit_event(
            session, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.updated",
            data={"fields_changed": {"received_items": {"old": state.get("received_items"),
                                                        "new": marked["received_items"]}}},
            actor_id=None, location_id=None, source="imported_cutover",
            idempotency_key=f"imported-cutover:mark:{doc_id}",
            metadata_={auto_je.IMPORTED_CUTOVER: True},
        )
        changed = True
    return changed


async def _number(session: AsyncSession, company_id, doc_id: str) -> str:
    from celerp.models.projections import Projection

    row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    state = (row.state if row else None) or {}
    return str(state.get("doc_number") or state.get("ref_id") or doc_id)


async def repair_imported_documents(session: AsyncSession) -> dict:
    """Correct every document an earlier release imported. Caller owns the transaction."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.ledger import LedgerEntry
    from celerp.notifications.service import notify_once
    from celerp.services import auto_je
    from celerp.services.migrations import is_company_migration_staged

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, CUTOVER_KEY)):
        return {"changed": False, "corrected": 0, "deferred": 0}

    imports = (await session.execute(
        select(LedgerEntry.company_id, LedgerEntry.entity_id, LedgerEntry.metadata_, LedgerEntry.data)
        .where(LedgerEntry.event_type == "doc.created").order_by(LedgerEntry.id)
    )).all()
    corrected: dict = {}
    waiting: dict = {}
    deferred = errored = 0
    staged: dict = {}
    for company_id, doc_id, meta, data in imports:
        meta = meta or {}
        if (not meta.get(auto_je.IMPORTED_SNAPSHOT) or meta.get(auto_je.IMPORTED_OPENING)
                or meta.get(auto_je.IMPORT_TREATMENT)
                or auto_je.imported_issue_kind(data or {}) not in ("purchase_order", "bill")):
            continue
        if company_id not in staged:
            staged[company_id] = await is_company_migration_staged(session, company_id)
        if staged[company_id]:
            continue
        try:
            async with session.begin_nested():
                changed = await _correct(session, company_id, doc_id)
        except HTTPException as exc:
            if exc.status_code == 503:
                raise  # backup in progress: nothing lands, the next start retries
            if exc.status_code == 422 and "locked" in str(exc.detail).lower():
                deferred += 1
                reason = "locked"
                log.warning("Imported document %s is in a locked period; correcting it on a later start", doc_id)
            else:
                errored += 1
                reason = "failed"
                log.warning("Could not correct imported document %s: %s", doc_id, exc.detail)
        except Exception:
            errored += 1
            reason = "failed"
            log.exception("Could not correct imported document %s", doc_id)
        else:
            reason = "corrected" if changed else None
        if reason is None:
            continue
        number = await _number(session, company_id, doc_id)
        if reason == "corrected":
            corrected.setdefault(company_id, []).append(number)
        else:
            waiting.setdefault((company_id, reason), []).append(number)

    for company_id, numbers in corrected.items():
        listed = ", ".join(numbers)
        await notify_once(session, company_id, "system", _TITLE, _body(numbers),
                          i18n={"title": "notice.imported_doc_cutover.title",
                                "body": "notice.imported_doc_cutover.body", "params": {"numbers": listed}})
    for (company_id, reason), numbers in waiting.items():
        key, text = _WAITING[reason]
        listed = ", ".join(numbers)
        await notify_once(session, company_id, "system", _WAITING_TITLE, text.format(numbers=listed),
                          i18n={"title": "notice.imported_doc_cutover.waiting_title", "body": key,
                                "params": {"numbers": listed}})
    pending = deferred or errored or any(staged.values())
    if not pending:
        await conn.run_sync(lambda c: set_meta(c, CUTOVER_KEY, "done"))
    count = sum(len(n) for n in corrected.values())
    if count or pending:
        log.info("Corrected %d imported document(s); %d deferred, %d failed%s", count, deferred, errored,
                 " (marker left unset; the next start retries)" if pending else "")
    return {"changed": True, "corrected": count, "deferred": deferred, "errored": errored}


async def imported_cutover_hook(*, session: AsyncSession) -> None:
    await repair_imported_documents(session)
