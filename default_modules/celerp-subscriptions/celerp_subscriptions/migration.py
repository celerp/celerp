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
and nothing is guessed. Every template is billed by hand afterwards (``manual_due``). A
template paused at the move is marked to restart from its resume day, as the earlier release
resumed, and the company is told once which active templates come out with more than one
period due.

Runs at startup per company, gated by a marker, and again (whatever the marker says) when a
company is restored or turns Subscriptions on, with the same per-template rules. A company staged for a migration, or that
has Subscriptions turned off, is skipped and waits for a later start. Each template moves in
its own savepoint: one that fails is rolled back alone, logged and named in a notice with
why, and the marker is cleared, by the pass a restore or turning Subscriptions on forces
too, so the next start tries it again; correcting its schedule moves it at once (``move_template``). The
integrity check runs after every pass; a problem is logged and notified once and also leaves
the marker unset. The hook never raises.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.business_time import company_today
from celerp_docs.doc_constants import SCHEDULE_REMOVED_FIELDS
from celerp_subscriptions import billing
from celerp_subscriptions.periods import BILLED, RELEASED, UNRESOLVED, Schedule, ScheduleError

log = logging.getLogger(__name__)

_MARKER = "subscription_schedules:{}"
_KEY = "subscription-schedule-migration:"
_NOTICE = "subscriptions.notice_schedule_check"
# The document types a claimed period may point at: a purchase order finalizes into a bill.
_DOC_TYPES = {"invoice": {"invoice"}, "purchase_order": {"purchase_order", "bill"}}


def is_legacy(state: dict) -> bool:
    """The template still holds an earlier release's fields instead of a schedule."""
    return "anchor_date" not in state and any(f in state for f in SCHEDULE_REMOVED_FIELDS)


def legacy_schedule(state: dict) -> dict:
    """The schedule fields an agreement not yet on a schedule means (a template an earlier
    release ran, or a draft from either release), read as the earlier release read them: an
    interval typed as text ("14") is that many days. A template it ran is anchored on its
    next run date (or start date), is billed by hand from here on, and with a custom interval
    left empty bills the 30 days it billed on; a draft is anchored on its start date and keeps
    its own settings, even when it holds a next run date, which the earlier release's activate
    never read. ScheduleError saying why when they describe no schedule."""
    ran = is_legacy(state) and state.get("status") != "draft"
    anchor = state.get("anchor_date") or (ran and state.get("next_run_date")) or state.get("start_date")
    if not anchor:
        raise ScheduleError("subscriptions.not_moved_no_date")
    fields = {"anchor_date": anchor}
    if ran:
        fields.update(generation_mode="manual_due", billing_timing="advance")
    if (state.get("frequency") or "monthly") == "custom":
        days = state.get("custom_interval_days")
        days = int(days) if isinstance(days, str) and days.strip().isdigit() else days
        if days and type(days) is not int:
            raise ScheduleError("subscriptions.not_moved_custom_days" if ran else "subscriptions.err_custom_days",
                                days=days)
        fields["custom_interval_days"] = days or (30 if ran else None)
    Schedule.from_state({**state, **fields, "periods": {}})
    return fields


async def _placements(session, company_id, state: dict, schedule: Schedule,
                      conflicts: set[str]) -> tuple[dict, list]:
    """Each old document's period, and the reasons the history is not clean."""
    start = state.get("start_date")
    placed: dict[str, list[int]] = {}
    problems = []
    for doc_id in state.get("generated_doc_ids") or []:
        doc = await billing.doc_state(session, company_id, doc_id)
        day = (doc or {}).get("issue_date")
        if doc_id in conflicts:
            problems.append(f"{doc_id} is referenced by another agreement")
        if doc is not None:
            if doc.get("doc_type") not in _DOC_TYPES[billing.doc_kind(state)]:
                problems.append(f"{doc_id} is not the expected document type")
            if not billing.bills(doc):
                problems.append(f"{doc_id} has an unissued or voided status")
            for field in ("source_agreement", "subscription_id"):
                if doc.get(field) and doc[field] != state.get("id"):
                    problems.append(f"{doc_id} belongs to a different agreement")
        if not day:
            placed[doc_id] = []
            problems.append(f"{doc_id} missing")
            continue
        try:
            k = schedule.current(date.fromisoformat(day))
        except ValueError:
            placed[doc_id] = []
            problems.append(f"{doc_id} has an invalid issue date")
            continue
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


async def _migrate_template(session: AsyncSession, company_id, proj: Projection,
                            conflicts: set[str] | None = None) -> bool | None:
    """Move one template onto a schedule; True when it waits for history review, None when
    another start already moved it. Caller holds the savepoint and commits."""
    row = await billing.locked_agreement(session, company_id, proj.entity_id, scheduled=False)
    state = row.state
    if not is_legacy(state):
        return None
    old_next = state.get("next_run_date")
    changes = legacy_schedule(state)
    review: list = []
    schedule = Schedule.from_state({**state, **changes, "periods": {}})
    placed, problems = await _placements(session, company_id,
                                          {**state, "id": proj.entity_id}, schedule, conflicts or set())
    periods = {}
    if problems:
        review = [{"doc_id": d, "candidates": ks} for d, ks in placed.items()]
        periods = {str(k): {"state": UNRESOLVED, "release_count": 0} for ks in placed.values() for k in ks}
        log.info("Template %s waits for history review: %s", proj.entity_id, "; ".join(problems))
    else:
        periods = {str(ks[0]): {"state": BILLED, "doc_id": d, "release_count": 0} for d, ks in placed.items()}
    changes["periods"] = periods
    if state.get("status") == "paused":
        changes["resume_restarts"] = True
    if review:
        changes["history_review"] = review
    await emit_event(
        session, company_id=company_id, entity_id=proj.entity_id, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {**{k: {"new": v} for k, v in changes.items()},
                                 **{f: {"drop": True} for f in sorted(SCHEDULE_REMOVED_FIELDS) if f in state}}},
        actor_id=None, location_id=None, source="system", idempotency_key=f"{_KEY}{proj.entity_id}",
        metadata_={"next_run_date": old_next},
    )
    return bool(review)


def _shared_documents(agreements: list[Projection]) -> set[str]:
    """The documents more than one agreement names, by its old ids or its claims."""
    owners: dict[str, str] = {}
    shared: set[str] = set()
    for agreement in agreements:
        doc_ids = list(agreement.state.get("generated_doc_ids") or [])
        doc_ids.extend(e["doc_id"] for e in (agreement.state.get("periods") or {}).values()
                       if e.get("state") == BILLED and e.get("doc_id"))
        for doc_id in doc_ids:
            if owners.setdefault(doc_id, agreement.entity_id) != agreement.entity_id:
                shared.add(doc_id)
    return shared


async def move_template(session: AsyncSession, company_id, entity_id: str) -> None:
    """Move one template onto a schedule now, as the startup pass would; ScheduleError saying
    why when its fields describe no schedule. Caller holds the savepoint and commits."""
    agreements = await billing.agreements(session, company_id)
    proj = next(a for a in agreements if a.entity_id == entity_id)
    await _migrate_template(session, company_id, proj, _shared_documents(agreements))


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


async def migrate_company(session: AsyncSession, company_id, *, force: bool = False) -> dict | None:
    """Move one company's templates; None when the company waits for a later start. Caller commits."""
    from celerp.migrations._data_reconcile import delete_meta, get_meta, set_meta
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
    if not force and await conn.run_sync(lambda c: get_meta(c, marker)):
        return result
    agreements = await billing.agreements(session, company_id)
    conflicts = _shared_documents(agreements)
    failed, moved = [], []
    for proj in agreements:
        if not is_legacy(proj.state):
            continue
        try:
            async with session.begin_nested():
                review = await _migrate_template(session, company_id, proj, conflicts)
        except Exception:
            failed.append(f"{_ref(proj)}: {billing.not_moved(proj.state)}")
            log.exception("Could not move template %s onto a schedule", proj.entity_id)
            continue
        if review is not None:
            moved.append(proj.entity_id)
            result["review"] += int(review)
    result["migrated"], result["failed"] = len(moved), len(failed)
    if failed:
        await billing.notify(session, company_id, "notice_schedule_not_moved", "/subscriptions?direction=sales",
                              templates="; ".join(failed))
    today = await company_today(session, company_id)
    now = {p.entity_id: p for p in await billing.agreements(session, company_id)}
    behind = [f"{_ref(now[eid])} ({len(due[1])})" for eid in moved
              if (due := billing.due_periods(now[eid].state, today)) and len(due[1]) > 1]
    if behind:
        await billing.notify(session, company_id, "notice_back_periods", "/subscriptions?direction=sales",
                              templates="; ".join(behind))
    problems = await check_template_schedule_integrity(session, company_id)
    for line in problems:
        log.error("Subscription schedule check: %s", line)
    if problems:
        await notify_once(session, company_id, "system", t(f"{_NOTICE}_title", "en"), t(f"{_NOTICE}_body", "en"),
                          action_url="/subscriptions?direction=sales",
                          i18n={"title": f"{_NOTICE}_title", "body": f"{_NOTICE}_body"})
    result["marked"] = not failed and not problems
    if not result["marked"]:
        await conn.run_sync(lambda c: delete_meta(c, marker))
    elif not force:
        await conn.run_sync(lambda c: set_meta(c, marker, "done"))
    return result


def _ref(proj: Projection) -> str:
    return proj.state.get("ref_id") or proj.entity_id


async def reconcile_company_schedules(*, session: AsyncSession, company_id) -> None:
    """on_company_module_enabled: move the company's templates as a start does, before it is
    usable; one that cannot move stays refused by name and the rest bill."""
    await migrate_company(session, company_id, force=True)


async def reconcile_restored_company_schedules(*, session: AsyncSession, company_id) -> None:
    """on_company_restored: move the templates as a start does; restored agreements require
    fresh consent before unattended operations resume."""
    await reconcile_company_schedules(session=session, company_id=company_id)
    for agreement in await billing.agreements(session, company_id):
        state = agreement.state
        if (state.get("generation_mode") in billing.AUTO_MODES or state.get("auto_send")
                or state.get("consents")):
            await billing.locked_agreement(session, company_id, agreement.entity_id, scheduled=False)
            await billing.write_agreement(
                session, company_id, agreement.entity_id,
                {"generation_mode": "manual_due", "auto_send": False}, None)


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
