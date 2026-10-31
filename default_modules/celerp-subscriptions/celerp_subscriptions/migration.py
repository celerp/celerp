# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""One-time move of templates from an earlier release onto agreement schedules.

An earlier release kept a template's ``next_run_date`` and the ids of the documents it made
(``generated_doc_ids``). This pass anchors each template's schedule on its ``next_run_date``,
so the date anyone sees does not move, and places each document in the period before the
anchor that contains its date. A clean history (one document per period, contiguous back
from the anchor) gets its claims; anything else (two documents in one period, a gap, a
document dated on or after the old next date or before the start date, a document that no
longer exists) leaves every document waiting in history review with its candidate period,
and nothing is guessed. Every template is billed by hand afterwards (``manual_due``).

Runs at startup per company, gated by a marker. A company staged for a migration, or that
has Subscriptions turned off, is skipped and waits for a later start. Each template moves in
its own savepoint: one that fails is rolled back alone and logged, and the marker stays
unset so the next start tries it again. The integrity check runs after every pass; a
problem is logged and notified once and also keeps the marker unset. The hook never raises.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp_subscriptions import billing
from celerp_subscriptions.periods import BILLED, RELEASED, UNRESOLVED, Schedule

log = logging.getLogger(__name__)

_MARKER = "subscription_schedules:{}"
_KEY = "subscription-schedule-migration:"
_LEGACY = ("next_run_date", "generated_doc_ids")
_NOTICE = "subscriptions.notice_schedule_check"
# The document types a claimed period may point at: a purchase order finalizes into a bill.
_DOC_TYPES = {"invoice": {"invoice"}, "purchase_order": {"purchase_order", "bill"}}


def _is_legacy(state: dict) -> bool:
    return "anchor_date" not in state and any(f in state for f in _LEGACY)


async def _placements(session, company_id, state: dict, schedule: Schedule) -> tuple[dict, list]:
    """Each old document's period, and the reasons the history is not clean."""
    start = state.get("start_date")
    placed: dict[str, list[int]] = {}
    problems = []
    for doc_id in state.get("generated_doc_ids") or []:
        doc = await billing.doc_state(session, company_id, doc_id)
        day = (doc or {}).get("issue_date")
        if not day:
            placed[doc_id] = []
            problems.append(f"{doc_id} missing")
            continue
        k = schedule.current(date.fromisoformat(day))
        placed[doc_id] = [k]
        if k >= 0:
            problems.append(f"{doc_id} dated on or after the next date")
        if start and day < start:
            problems.append(f"{doc_id} dated before the start date")
    occupied = sorted(k for ks in placed.values() for k in ks)
    if len(occupied) != len(set(occupied)):
        problems.append("two documents in one period")
    if occupied and occupied != list(range(-len(occupied), 0)):
        problems.append("a gap between documents")
    return placed, problems


async def _migrate_template(session: AsyncSession, company_id, proj: Projection) -> bool | None:
    """Move one template onto a schedule; True when it waits for history review, None when
    another start already moved it. Caller holds the savepoint and commits."""
    row = await billing.locked_agreement(session, company_id, proj.entity_id)
    state = row.state
    if not _is_legacy(state):
        return None
    old_next = state.get("next_run_date")
    changes: dict = {"generation_mode": "manual_due", "billing_timing": "advance"}
    review: list = []
    anchor = old_next or state.get("start_date")
    if anchor:
        changes["anchor_date"] = anchor
        schedule = Schedule.from_state({**state, **changes, "periods": {}})
        placed, problems = await _placements(session, company_id, state, schedule)
        periods = {}
        if problems:
            review = [{"doc_id": d, "candidates": ks} for d, ks in placed.items()]
            periods = {str(k): {"state": UNRESOLVED, "release_count": 0} for ks in placed.values() for k in ks}
            log.info("Template %s waits for history review: %s", proj.entity_id, "; ".join(problems))
        else:
            periods = {str(ks[0]): {"state": BILLED, "doc_id": d, "release_count": 0} for d, ks in placed.items()}
        changes["periods"] = periods
        if review:
            changes["history_review"] = review
    await emit_event(
        session, company_id=company_id, entity_id=proj.entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {**{k: {"new": v} for k, v in changes.items()},
                                 **{f: {"drop": True} for f in _LEGACY if f in state}}},
        actor_id=None, location_id=None, source="system", idempotency_key=f"{_KEY}{proj.entity_id}",
        metadata_={"next_run_date": old_next},
    )
    return bool(review)


async def _old_next_dates(session, company_id) -> dict[str, str]:
    rows = (await session.execute(select(LedgerEntry.entity_id, LedgerEntry.metadata_).where(
        LedgerEntry.company_id == company_id, LedgerEntry.idempotency_key.startswith(_KEY, autoescape=True)))).all()
    return {eid: (meta or {}).get("next_run_date") for eid, meta in rows}


async def check_template_schedule_integrity(session: AsyncSession, company_id) -> list[str]:
    """Every problem with the agreements' period claims, as readable lines (empty when sound):
    a claim on a missing document or one of the wrong kind, a document claimed twice, and a
    migrated template whose next bill date moved before any later period was touched."""
    problems = []
    claimed: dict[str, str] = {}
    old_next = await _old_next_dates(session, company_id)
    for row in await billing.agreements(session, company_id):
        state, tid = row.state, row.entity_id
        kinds = _DOC_TYPES[billing.doc_kind(state)]
        for k, entry in (state.get("periods") or {}).items():
            doc_id = entry.get("doc_id")
            if entry.get("state") != BILLED or not doc_id:
                continue
            doc = await billing.doc_state(session, company_id, doc_id)
            if doc is None:
                problems.append(f"{tid} period {k} claims {doc_id}, which does not exist")
            elif doc.get("doc_type") not in kinds:
                problems.append(f"{tid} period {k} claims {doc_id}, a {doc.get('doc_type')}")
            here = f"{tid} period {k}"
            if doc_id in claimed:
                problems.append(f"{doc_id} is claimed by {claimed[doc_id]} and {here}")
            claimed.setdefault(doc_id, here)
        before = old_next.get(tid)
        if not before or not state.get("anchor_date") or state["anchor_date"] != before:
            continue
        entries = {int(k): e for k, e in (state.get("periods") or {}).items()}
        if any(k >= 0 for k in entries) or any(e.get("state") == RELEASED for e in entries.values()):
            continue
        nxt = Schedule.from_state(state).next_bill_date(floor=billing.review_floor(state))
        if nxt is None or nxt.isoformat() != before:
            problems.append(f"{tid} next bills on {nxt}, not {before} as before the move to schedules")
    return problems


async def migrate_company(session: AsyncSession, company_id) -> dict | None:
    """Move one company's templates; None when the company waits for a later start. Caller commits."""
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.company import Company
    from celerp.modules.registry import uses_module
    from celerp.notifications.service import notify_once
    from celerp.services.migrations import is_company_migration_staged
    from ui.i18n import t

    company = await session.get(Company, company_id)
    if (company is None or await is_company_migration_staged(session, company_id)
            or not uses_module(company.settings, "celerp-subscriptions")):
        return None
    marker = _MARKER.format(company_id)
    conn = await session.connection()
    result = {"migrated": 0, "review": 0, "failed": 0, "marked": True}
    if await conn.run_sync(lambda c: get_meta(c, marker)):
        return result
    for proj in await billing.agreements(session, company_id):
        if not _is_legacy(proj.state):
            continue
        try:
            async with session.begin_nested():
                review = await _migrate_template(session, company_id, proj)
        except Exception:
            result["failed"] += 1
            log.exception("Could not move template %s onto a schedule", proj.entity_id)
            continue
        if review is not None:
            result["migrated"] += 1
            result["review"] += int(review)
    problems = await check_template_schedule_integrity(session, company_id)
    for line in problems:
        log.error("Subscription schedule check: %s", line)
    if problems:
        await notify_once(session, company_id, "system", t(f"{_NOTICE}_title", "en"), t(f"{_NOTICE}_body", "en"),
                          action_url="/subscriptions?direction=sales",
                          i18n={"title": f"{_NOTICE}_title", "body": f"{_NOTICE}_body"})
    result["marked"] = not result["failed"] and not problems
    if result["marked"]:
        await conn.run_sync(lambda c: set_meta(c, marker, "done"))
    return result


async def migrate_schedules_hook(*, session: AsyncSession) -> None:
    """on_modules_ready: move every company's templates onto schedules."""
    from celerp.models.company import Company

    for company_id in (await session.execute(select(Company.id).order_by(Company.id))).scalars().all():
        try:
            async with session.begin_nested():
                result = await migrate_company(session, company_id)
        except Exception:
            log.exception("Moving templates of company %s onto schedules failed; the next start retries", company_id)
            continue
        if result and (result["migrated"] or result["failed"]):
            log.info("Moved %d template(s) of company %s onto schedules; %d wait for history review, %d failed",
                     result["migrated"], company_id, result["review"], result["failed"])
