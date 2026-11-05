# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Billing an agreement's periods: the one claim every route and the dispatcher use.

A period is billed once. The claim runs under the company and agreement locks and keys
its document event on the agreement and period (``sub:{agreement}:p{k}:{kind}:r{n}``), so
the ledger's unique key refuses a second document even if the lock were bypassed.
Unattended billing (``run_due``) bills only agreements whose authorizer still holds the
permission, at most ``budget`` documents per pass, oldest period first, and never a
backlog older than the agreement's catch-up window. Sending is requested after commit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.models.subscription_email import SubscriptionEmailAttempt
from celerp.services import email
from celerp.services.business_time import business_today
from celerp.services.company_lock import lock_projections, locked_company
from celerp.services.permissions import missing_permission, role_has_permission
from celerp.services.record_quantities import template_label
from celerp.services.terms import resolve_document_terms
from celerp.services.units import require_moving_fits_units
from celerp_docs.doc_money import HEADER_MONEY_FIELDS, document_money
from celerp_docs.periodic import OperationReport
from celerp_subscriptions.periods import (BILLED, RELEASED, SCHEDULE_FIELDS, SKIPPED, UNRESOLVED, Schedule, ScheduleError,
                                          shown)
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES
from ui.i18n import t

log = logging.getLogger(__name__)

AUTO_MODES = frozenset({"auto_invoice"})
# Each unattended mode and the permission its authorizer must keep holding.
MODE_PERMISSION = {"generation": "automate_billing", "sending": "automate_sending"}
MODE_DOCUMENT_PERMISSION = {"generation": "finalize_documents", "sending": "edit_documents"}
# The settings each mode's consent covers: changing one while the mode runs is a new consent.
# Automatic billing covers every setting its decision reads (unattended_due, outside_window).
CONSENT_TERMS = {"generation": ("generation_mode", "catchup_periods", *SCHEDULE_FIELDS), "sending": ("auto_send",)}
# A send handed over with no answer ("unconfirmed"), or cut off before one was recorded ("sending").
UNCONFIRMED = ("sending", "unconfirmed")
CATCHUP_CAP = 12
# The agreement write_agreement is writing: the one writer allowed to change an agreement's
# status (guard_agreement_status), set by nothing else (consent_census holds it).
_WRITING: ContextVar[str | None] = ContextVar("agreement_write", default=None)
MAX_EMAIL_PAYLOAD_BYTES = 2 * 1024 * 1024
EMAIL_ATTEMPT_RETENTION = timedelta(days=2)
RECOVERY_RETRY_AGE = timedelta(hours=12)


def now_stamp() -> str:
    """Wall-clock time of a record (not the business day): what a summary acknowledgment compares."""
    return datetime.now(timezone.utc).isoformat()


def retryable_attempt_time(created_at: datetime | None) -> bool:
    """Refuse old unknown-outcome retries even after extended downtime."""
    if created_at is None:
        return False
    when = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return when > datetime.now(timezone.utc) - RECOVERY_RETRY_AGE


def doc_kind(state: dict) -> str:
    return "invoice" if state.get("doc_type") == "subscription_invoice" else "purchase_order"


def periods_of(state: dict) -> dict[str, dict]:
    return copy.deepcopy(dict(state.get("periods") or {}))


def review_floor(state: dict) -> int | None:
    """While earlier documents wait for review, the periods before the anchor are not billed."""
    return 0 if state.get("history_review") else None


def compute_due_date(issue_date: date, payment_terms: str | None, company: Company) -> str | None:
    """ISO due date from the payment terms' days in the company settings."""
    if not payment_terms:
        return None
    for term in (company.settings or {}).get("payment_terms", []):
        if term.get("name") == payment_terms:
            days = int(term.get("days") or 0)
            return (issue_date + timedelta(days=days)).isoformat() if days else None
    return None


async def locked_agreement(session: AsyncSession, company_id, entity_id: str, *,
                           scheduled: bool = True) -> Projection:
    """The agreement under the company lock and its own row lock, as the previous writer
    committed it: every schedule operation reads and writes it under this lock. With
    ``scheduled`` (every operation that reads the schedule; pause and cancel do not), a
    template not yet moved onto one is refused with a 409 naming it and why."""
    proj = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
    if scheduled and (reason := not_moved(proj.state)) is not None:
        raise not_moved_refusal(proj.state, entity_id, reason)
    return proj


def not_moved_refusal(state: dict, entity_id: str, reason: str) -> HTTPException:
    """The 409 for a template not yet on a schedule, naming it and why."""
    return HTTPException(status_code=409, detail=t(
        "subscriptions.err_not_moved", ref=state.get("ref_id") or entity_id, reason=reason))


def not_moved(state: dict) -> str | None:
    """Why a template still holds an earlier release's fields instead of a schedule, or None
    when it has a schedule."""
    from celerp_subscriptions import migration

    if not migration.is_legacy(state):
        return None
    try:
        migration.legacy_schedule(state)
    except ScheduleError as exc:
        return t(exc.key, **exc.params)
    return t("subscriptions.not_moved_pending")


async def write_agreement(session: AsyncSession, company_id, entity_id: str, changes: dict, actor_id, *,
                          drop: tuple[str, ...] = (), key: str | None = None, metadata: dict | None = None) -> None:
    """Record a change to the agreement's own fields (``{field: new value}``); ``drop``
    removes fields from its state. Only what changes as the agreement reads it is recorded
    (periods.shown: a field not stored reads as its unset value): a field given a value that
    reads as the one it holds is left out, a field dropped while what it holds reads as unset is
    cleared only beside a change that is recorded, and with nothing left no event is written,
    so History never shows a row that says nothing. Each field keeps its value before the
    change, and a change to periods keeps the dates and the document number of each period it
    touched, so History can name them. Every agreement change in this module goes through here.

    It is also where an unattended mode comes into force: a mode in force after the change
    that was not before, or whose consent terms change, needs both its permissions from the
    actor's role (403 naming the first missing) and records the actor's consent; the consents
    kept are exactly the modes in force after the change, so pausing or cancelling drops them."""
    before = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id == entity_id))).scalar_one_or_none() or {}
    changes = {k: v for k, v in changes.items() if shown(k, before.get(k)) != shown(k, v)}
    drop = tuple(k for k in drop if k in before and k not in changes)
    if not changes and all(shown(k, before[k]) == shown(k, None) for k in drop):
        drop = ()
    merged = {**{k: v for k, v in before.items() if k not in drop}, **changes}
    running, in_force = modes_in_force(before), modes_in_force(merged)
    needed = [m for m in in_force if m not in running or any(f in changes for f in CONSENT_TERMS[m])]
    if needed:
        company = await session.get(Company, company_id)
        role = await _member_role(session, company_id, actor_id)
        for mode in needed:
            if (missing := missing_mode_permission((company.settings if company else None) or {}, role, mode)):
                raise missing_permission(missing)
    kept = before.get("consents") or {}
    consents = {**{m: kept[m] for m in in_force if m in kept},
                **{m: {"by": str(actor_id), "at": now_stamp()} for m in needed}}
    if consents != kept:
        changes, drop = ({**changes, "consents": consents}, drop) if consents else (changes, (*drop, "consents"))
    if not changes and not drop:
        return
    after = {**{k: v for k, v in before.items() if k not in drop}, **changes}
    fields = {**{k: {"old": before.get(k), "new": v} for k, v in changes.items()},
              **{k: {"old": before.get(k), "drop": True} for k in drop}}
    old_periods, new_periods = before.get("periods") or {}, after.get("periods") or {}
    touched = {k for k in {*old_periods, *new_periods} if old_periods.get(k) != new_periods.get(k)}
    if (failed := (changes.get("billing_failure") or {}).get("period_index")) is not None:
        touched.add(str(failed))
    if touched and after.get("anchor_date"):
        try:
            schedule = Schedule.from_state(after)
            dates = {k: [schedule.start(int(k)).isoformat(), schedule.service_end(int(k)).isoformat()] for k in touched}
        except ValueError:
            dates = {}
        for k in ("periods", "billing_failure"):
            if dates and k in changes:
                fields[k]["dates"] = dates
    if "periods" in changes:
        # The document each touched period was billed on, by its number then, so History names it.
        claimed = {k: (new_periods.get(k) or {}).get("doc_id") or (old_periods.get(k) or {}).get("doc_id")
                   for k in touched}
        if refs := {k: await doc_ref(session, company_id, d) for k, d in claimed.items() if d}:
            fields["periods"]["refs"] = refs
    writing = _WRITING.set(entity_id)
    try:
        await emit_event(
            session, company_id=company_id, entity_id=entity_id, entity_type="doc", event_type="doc.updated",
            data={"fields_changed": fields},
            actor_id=actor_id, location_id=None, source="subscription",
            idempotency_key=key or str(uuid.uuid4()), metadata_=metadata,
        )
    finally:
        _WRITING.reset(writing)


async def guard_agreement_status(*, session, entry, transition) -> None:
    """doc_status_guard: the status of an agreement that runs an unattended mode, before or
    after the change, changes only in write_agreement (activate, pause, resume, cancel on its
    own page), where a mode coming into force asks for its consent and a mode stopping drops
    it. Every other writer (void, unvoid, finalize, revert, an import) is refused (409)."""
    if _WRITING.get() == entry.entity_id or transition.after.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        return
    if modes_in_force(transition.before or {}) or modes_in_force(transition.after):
        raise HTTPException(status_code=409, detail=t("subscriptions.err_agreement_status_elsewhere"))


def refuse_status(state: dict) -> None:
    status = state.get("status")
    if status in ("draft", "paused", "cancelled"):
        key = "subscriptions.err_generate_cancelled" if status == "cancelled" else f"subscriptions.err_bill_{status}"
        raise HTTPException(status_code=409, detail=t(key))


async def doc_state(session, company_id, doc_id: str) -> dict | None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
    return (row.state or {}) if row is not None else None


async def doc_ref(session, company_id, doc_id: str) -> str:
    return ((await doc_state(session, company_id, doc_id)) or {}).get("ref_id") or doc_id


# The one rule for "is this period billed": its claimed document bills it only while the
# document is issued. A draft posts nothing; a void, cancelled or deleted one bills nothing.
NOT_BILLING = frozenset({"draft", "void", "cancelled"})


def bills(doc: dict | None) -> bool:
    return doc is not None and (doc.get("status") or "draft") not in NOT_BILLING


async def billing_doc(session, company_id, doc_id: str) -> dict:
    """The document a period claims, refused (409) when it does not bill the period: back in
    draft (finalize it, or Reopen), or void or gone (Reopen)."""
    doc = await doc_state(session, company_id, doc_id)
    if not bills(doc):
        key = "subscriptions.err_period_draft" if (doc or {}).get("status") == "draft" else "subscriptions.err_period_void"
        raise HTTPException(status_code=409, detail=t(key, ref=(doc or {}).get("ref_id") or doc_id))
    return doc


async def guard_bill_status(*, session, entry, transition) -> None:
    """doc_status_guard: a document an agreement period let go (Reopen records it in
    ``released_bills``) never bills again, whichever writer issues it (unvoid, finalize,
    send). Refused (409) naming the bill the period has now, or saying it was reopened."""
    if bills(transition.before) or not bills(transition.after):
        return
    doc_id = entry.entity_id
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == entry.company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(SUBSCRIPTION_DOC_TYPES),
        Projection.state["released_bills"][doc_id].as_string().is_not(None),
    ).order_by(Projection.entity_id))).scalars().all()
    for agreement in rows:
        k = int(agreement.state["released_bills"][doc_id])
        schedule = Schedule.from_state(agreement.state)
        newer = (schedule.periods.get(k) or {}).get("doc_id") if schedule.state(k) == BILLED else None
        newer_doc = await doc_state(session, entry.company_id, newer) if newer else None
        params = {"ref": transition.before.get("ref_id") or doc_id, "agreement": template_label(agreement.state, agreement.entity_id),
                  "start": schedule.start(k).isoformat(), "end": schedule.service_end(k).isoformat()}
        if bills(newer_doc):
            raise HTTPException(status_code=409, detail=t(
                "subscriptions.err_bill_rebilled", newer=newer_doc.get("ref_id") or newer, **params))
        raise HTTPException(status_code=409, detail=t("subscriptions.err_bill_reopened", **params))


async def replay_result(session, company_id, template_id: str, request_key: str,
                        *, early: bool = False, period_index: int | None = None) -> dict | None:
    """Return the committed receipt for precisely this billing request, without claiming a new
    period, while its document still bills the period (billing_doc refuses one that stopped)."""
    event = await find_event_by_idempotency(session, company_id, f"subreq:{template_id}:{request_key}")
    if event is None:
        return None
    if (event.metadata_ or {}).get("request") != {"early": early, "period_index": period_index}:
        raise HTTPException(status_code=409, detail=t("subscriptions.err_request_key_reused"))
    result = event.metadata_["result"]
    await billing_doc(session, company_id, result["doc_id"])
    return result


async def _result(session, company_id, doc_id: str, k: int, schedule: Schedule, state: dict, *,
                  already_billed: bool) -> dict:
    """A billing answer. ``already_billed``: the document was billed before this request, so
    the answer bills nothing and a count of what was billed leaves it out."""
    nxt = schedule.next_bill_date(floor=review_floor(state))
    return {"doc_id": doc_id, "ref_id": await doc_ref(session, company_id, doc_id), **period_row(schedule, k),
            "next_bill_date": nxt.isoformat() if nxt else None, "already_billed": already_billed}


async def _billed_answer(session, company_id, k: int, schedule: Schedule, state: dict) -> dict:
    """The answer for period ``k``, billed before this request: its document, or, when that
    document no longer bills it, billing_doc's refusal saying what to do."""
    doc_id = schedule.periods[k]["doc_id"]
    await billing_doc(session, company_id, doc_id)
    return await _result(session, company_id, doc_id, k, schedule, state, already_billed=True)


async def _bill(session, company, company_id, tid: str, state: dict, schedule: Schedule, k: int, actor,
                *, auto: bool, send: bool, request: dict | None = None, key: str | None = None) -> str:
    """Create, finalize and claim period ``k``'s document. Caller holds the locks and commits.
    A billing ``request`` under its idempotency ``key`` keeps its answer on the claim's own
    event, for replay_result to repeat."""
    from celerp_docs.routes import finalize_document
    from celerp_docs.sequences import next_draft_ref

    kind = doc_kind(state)
    entry = (state.get("periods") or {}).get(str(k)) or {}
    release = entry.get("release_count", 0)
    claim_key = f"sub:{tid}:p{k}:{kind}:r{release}"
    if (earlier := await find_event_by_idempotency(session, company_id, claim_key)) is not None:
        return earlier.entity_id
    today = business_today((company.settings or {}).get("timezone"))
    contact_id = state.get("contact_id")
    contact = (await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})) if contact_id else None
    contact_state = (contact.state or {}) if contact is not None and contact.entity_type == "contact" else {}
    service = {"source_agreement": tid, "period_index": k,
               "service_start": schedule.start(k).isoformat(), "service_end": schedule.service_end(k).isoformat()}
    lines = [{**li, **service} for li in copy.deepcopy(list(state.get("line_items") or []))]
    template_name = template_label(state, tid)
    auto_note = f"Auto-created from subscription template {template_name}"
    notes = f"{state['notes']}\n{auto_note}".strip() if state.get("notes") else auto_note
    money_inputs = {f: state[f] for f in HEADER_MONEY_FIELDS if f in state}
    currency = money_inputs["currency"] = state.get("currency") or (company.settings or {}).get("currency", "USD")
    money = document_money(money_inputs, lines, currency, keep_unrated_tax=True)
    ref = next_draft_ref(company, kind)
    doc_id = f"doc:{ref}"
    doc = {
        "doc_type": kind, "ref_id": ref, "contact_id": contact_id,
        "contact_name": contact_state.get("name") or "", "contact_company_name": contact_state.get("company_name") or "",
        "line_items": lines, "payment_terms": state.get("payment_terms"), **money_inputs, **money, "amount_outstanding": money["total"], "issue_date": today.isoformat(),
        "notes": notes, **service,
    }
    if due := compute_due_date(today, state.get("payment_terms"), company):
        doc["due_date"] = due
    # The template may predate a precision change; what it makes is held to today's units.
    await require_moving_fits_units(session, company_id, doc_id, "doc", doc, template=template_name)
    doc.update(resolve_document_terms(state, company.settings or {}, kind))
    if "customer_note" in state:
        doc["customer_note"] = state.get("customer_note") or ""
    await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
                     data=doc, actor_id=actor.id, location_id=None, source="subscription", idempotency_key=claim_key)
    await session.flush()
    await finalize_document(doc_id, company_id, actor, session, commit=False)
    await session.flush()
    periods = periods_of(state)
    periods[str(k)] = {"state": BILLED, "doc_id": doc_id, "release_count": release, "at": now_stamp(),
                       "by": str(actor.id) if actor.id else None, "auto": auto,
                       **({"send": {"state": "pending", "n": 0, "at": now_stamp()}} if send else {})}
    drop = ("billing_failure",) if state.get("billing_failure") else ()
    metadata = None
    if key:
        after = {**{f: v for f, v in state.items() if f not in drop}, "periods": periods}
        metadata = {"request": request, "result": await _result(
            session, company_id, doc_id, k, Schedule.from_state(after), after, already_billed=False)}
    await write_agreement(session, company_id, tid, {"periods": periods}, actor.id, drop=drop, key=key, metadata=metadata)
    return doc_id


async def claim_period(session: AsyncSession, company_id, template_id: str, *, actor, early: bool = False,
                       period_index: int | None = None, request_key: str | None = None) -> dict:
    """Bill the agreement's oldest open period: due (its bill date has come) or, with
    ``early``, the next one ahead of its date (refused while a period is due). With nothing
    due, the latest due period's document when it is billed and not void, marked
    ``already_billed``; otherwise a refusal naming why. A ``request_key`` repeats the first
    answer to the same request. Caller commits."""
    company = await locked_company(session, company_id)
    req = f"subreq:{template_id}:{request_key}" if request_key else None
    request = {"early": early, "period_index": period_index}
    if request_key and (prior := await replay_result(
            session, company_id, template_id, request_key, early=early, period_index=period_index)) is not None:
        return prior
    proj = await locked_agreement(session, company_id, template_id)
    state = proj.state
    refuse_status(state)
    schedule = Schedule.from_state(state)
    today = business_today((company.settings or {}).get("timezone"))
    k = schedule.oldest_open(floor=review_floor(state))
    if period_index is not None and period_index != k:
        # A stale preview or repeated batch must never advance to a different period.
        if schedule.state(period_index) == BILLED and not early:
            return await _billed_answer(session, company_id, period_index, schedule, state)
        raise HTTPException(status_code=409, detail=t("subscriptions.err_early_not_next"))
    if early:
        if k is None:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_no_period_left"))
        if schedule.bill_date(k) <= today:
            # Bill early bills ahead of the bill date; a due period is Generate due's.
            raise HTTPException(status_code=409, detail=t(
                "subscriptions.err_early_due", date=schedule.bill_date(k).isoformat()))
    elif k is None or schedule.bill_date(k) > today:
        if schedule.ended(today):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_bill_ended"))
        latest = schedule.latest_due(today)
        if latest is not None and schedule.state(latest) == BILLED:
            return await _billed_answer(session, company_id, latest, schedule, state)
        nxt = schedule.next_bill_date(floor=review_floor(state))
        raise HTTPException(status_code=409, detail=(
            t("subscriptions.err_nothing_due", date=nxt.isoformat()) if nxt
            else t("subscriptions.err_no_period_left")))
    doc_id = await _bill(session, company, company_id, template_id, state, schedule, k, actor, auto=False, send=False,
                         request=request, key=req)
    state = (await session.get(Projection, {"company_id": company_id, "entity_id": template_id})).state
    return await _result(session, company_id, doc_id, k, Schedule.from_state(state), state, already_billed=False)


async def agreements(session: AsyncSession, company_id) -> list[Projection]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(SUBSCRIPTION_DOC_TYPES),
    ).order_by(Projection.entity_id))).scalars().all()
    return list(rows)


async def discard_email_attempts(session: AsyncSession, company_id, doc_id: str) -> None:
    """Discard obsolete in-flight attempts when their billed document is released."""
    await session.execute(delete(SubscriptionEmailAttempt).where(
        SubscriptionEmailAttempt.company_id == company_id,
        SubscriptionEmailAttempt.doc_id == doc_id))


async def require_history_document(session: AsyncSession, company_id, template_id: str,
                                   state: dict, doc_id: str) -> None:
    """A historical placement must reference an eligible, unclaimed company document."""
    doc = await doc_state(session, company_id, doc_id)
    allowed = {"invoice"} if doc_kind(state) == "invoice" else {"purchase_order", "bill"}
    if (doc is None or doc.get("doc_type") not in allowed
            or not bills(doc)
            or any(doc.get(field) and doc[field] != template_id
                   for field in ("source_agreement", "subscription_id"))):
        raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))
    for agreement in await agreements(session, company_id):
        if any(entry.get("state") == BILLED and entry.get("doc_id") == doc_id
               for entry in (agreement.state.get("periods") or {}).values()):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))


def period_row(schedule: Schedule, k: int) -> dict:
    return {"period_index": k, "service_start": schedule.start(k).isoformat(),
            "service_end": schedule.service_end(k).isoformat(), "bill_date": schedule.bill_date(k).isoformat()}


# What the Periods table says of a period: its claim, else where its bill date stands today.
PERIOD_STATUSES = ("billed", "draft", "voided", "skipped", "unresolved", "overdue", "due", "upcoming")


def period_status(state: str | None, doc: dict | None, bill_date: date, today: date) -> str:
    """One of PERIOD_STATUSES. A billed period reads "billed" only while its document bills
    it: back in draft it reads "draft", void or gone "voided"; an open or reopened one reads
    by its bill date."""
    if state == BILLED:
        return "billed" if bills(doc) else "draft" if (doc or {}).get("status") == "draft" else "voided"
    if state in (SKIPPED, UNRESOLVED):
        return state
    return "overdue" if bill_date < today else "due" if bill_date == today else "upcoming"


async def replayable_operations(session, company_id) -> set[str]:
    """The email attempts whose durable payload is still inside the transport's retry window."""
    attempts = (await session.execute(select(
        SubscriptionEmailAttempt.operation_id, SubscriptionEmailAttempt.created_at).where(
            SubscriptionEmailAttempt.company_id == company_id))).all()
    return {str(operation_id) for operation_id, created_at in attempts if retryable_attempt_time(created_at)}


def sends_again(send: dict | None, replayable: set[str]) -> bool:
    """A cut-off send is sent again as a new message, behind a confirm that it may already have
    arrived, unless the relay replays the same message (it cannot arrive twice)."""
    send = send or {}
    return send.get("state") in UNCONFIRMED and not (
        send.get("deduplicated") and email.relay_send_is_idempotent() and send.get("operation_id") in replayable)


async def _listed_row(session, company_id, state: dict, schedule: Schedule, k: int, today: date,
                      replayable: set[str]) -> dict:
    """Period ``k`` as the Periods table lists it: dates, claim, status, document, the earlier
    documents it let go, send state, and whether sending it again is a new message."""
    entry = schedule.periods.get(k) or {}
    doc_id = entry.get("doc_id")
    doc = (await doc_state(session, company_id, doc_id)) if doc_id else None
    send = send_status(state, k, today, doc)
    released = [{"doc_id": d, "ref_id": await doc_ref(session, company_id, d)}
                for d, held in sorted((state.get("released_bills") or {}).items()) if int(held) == k]
    return {**period_row(schedule, k), "state": entry.get("state"), "reopened": entry.get("state") == RELEASED and bool(entry.get("reopened")),
            "status": period_status(entry.get("state"), doc, schedule.bill_date(k), today),
            "doc_id": doc_id, "ref_id": ((doc or {}).get("ref_id") or doc_id) if doc_id else None,
            "reason": entry.get("reason"), "history": bool(entry.get("history")), "released": released,
            "send": send, "again": send == "unconfirmed" and sends_again(entry.get("send"), replayable)}


async def period_list(session: AsyncSession, company_id, state: dict, today: date, *,
                      limit: int = 24, before: int | None = None) -> dict:
    """An agreement's periods from its lowest to the one after today (and any claimed later),
    newest first, at most ``limit``, with each claim's state, status, document and email send state; its next bill date;
    the earlier documents waiting for history review, each with its candidate periods; and the
    ones set aside as billing no period of their own. A template not yet on a schedule has
    none of these, and ``not_moved`` says why."""
    if not state.get("anchor_date"):
        return {"next_bill_date": None, "rows": [], "review": [], "set_aside": [],
                "older_before": None, "oldest_due_row": None, "not_moved": not_moved(state)}
    schedule = Schedule.from_state(state)
    nxt = schedule.next_bill_date(floor=review_floor(state))
    first = schedule.first()
    replayable = await replayable_operations(session, company_id)
    rows = []
    if first is not None:
        top = max([schedule.current(today) + 1, *schedule.periods])
        if schedule.term_end is not None:
            end = schedule.current(schedule.term_end)
            top = min(top, end - (1 if schedule.start(end) == schedule.term_end else 0))
        if schedule.last is not None:
            top = min(top, schedule.last)
        k = min(top, before - 1) if before is not None else top
        while k >= first and len(rows) < limit:
            if schedule.exists(k):
                rows.append(await _listed_row(session, company_id, state, schedule, k, today, replayable))
            k -= 1
    older_before = rows[-1]["period_index"] if rows and first is not None and k >= first else None
    oldest_open = schedule.oldest_open(floor=review_floor(state))
    oldest_due_row = None
    if (before is None and oldest_open is not None and oldest_open not in
            {r["period_index"] for r in rows} and schedule.bill_date(oldest_open) <= today):
        oldest_due_row = await _listed_row(session, company_id, state, schedule, oldest_open, today, replayable)
    review = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id")),
               "candidates": [period_row(schedule, c) for c in r.get("candidates") or []]}
              for r in state.get("history_review") or []]
    set_aside = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id"))}
                 for r in state.get("history_set_aside") or []]
    return {"next_bill_date": nxt.isoformat() if nxt else None,
            "oldest_open_index": oldest_open, "oldest_due_row": oldest_due_row,
            "older_before": older_before, "rows": rows, "review": review, "set_aside": set_aside}


def due_periods(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and its due open periods, for an active agreement with a schedule."""
    if state.get("status") != "active" or not state.get("anchor_date"):
        return None
    schedule = Schedule.from_state(state)
    return schedule, schedule.due(today, floor=review_floor(state))


async def due_preview(session: AsyncSession, company_id, today: date) -> dict:
    """Every active agreement with a period due, and the next bill date after them."""
    items, upcoming = [], []
    for row in await agreements(session, company_id):
        found = due_periods(row.state, today)
        if found is None:
            continue
        schedule, due = found
        if due:
            items.append({"entity_id": row.entity_id, "periods": [period_row(schedule, k) for k in due]})
        elif (nxt := schedule.next_bill_date(floor=review_floor(row.state))) is not None:
            upcoming.append(nxt)
    return {"items": items, "total": len(items),
            "next_bill_date": min(upcoming).isoformat() if upcoming else None}


def outside_window(state: dict, schedule: Schedule, due: list[int], today: date) -> bool:
    """The oldest due period is further back than the agreement's catch-up window."""
    latest = schedule.latest_due(today)
    # A terminated agreement can have no recent due period. Compare with the
    # present schedule index, not only its final eligible period.
    bill_index = schedule.current(today) - int(schedule.timing == "arrears")
    age = max(bill_index, latest if latest is not None else bill_index) - due[0]
    return bool(due) and age > int(shown("catchup_periods", state.get("catchup_periods")))


def waiting_sends(state: dict, today: date) -> list[int]:
    """The periods whose email waits to go out on the next pass: billed with Send on, never
    sent (the app stopped first, or sending was off), and inside the catch-up window. One
    further back waits for the user to send it from the summary, as an unbilled period
    that far back waits for the catch-up preview."""
    waiting = sorted(int(k) for k, entry in (state.get("periods") or {}).items()
                     if (entry.get("send") or {}).get("state") == "pending")
    if not waiting or not state.get("anchor_date") or not sends_itself(state):
        return []
    schedule = Schedule.from_state(state)
    return [k for k in waiting if not outside_window(state, schedule, [k], today)]


def unattended_due(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and due periods of an agreement that bills itself, with no earlier
    document waiting for review; None when it is not one or nothing is due."""
    if state.get("generation_mode") not in AUTO_MODES or state.get("history_review"):
        return None
    found = due_periods(state, today)
    return found if found and found[1] else None


async def catch_up_preview(session: AsyncSession, company_id, today: date) -> list[dict]:
    """Unattended agreements behind by more than their catch-up window: billed only from here."""
    items = []
    for row in await agreements(session, company_id):
        found = unattended_due(row.state, today)
        if found and outside_window(row.state, *found, today):
            schedule, due = found
            items.append({"entity_id": row.entity_id, "periods": [period_row(schedule, k) for k in due]})
    return items


async def _member_role(session, company_id, user_id) -> str | None:
    if not user_id:
        return None
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    return await session.scalar(select(UserCompany.role).where(
        UserCompany.company_id == company_id, UserCompany.user_id == uid, UserCompany.is_active.is_(True)))


async def authorizer(session, company_id, settings: dict, state: dict, mode: str):
    """The member whose consent runs ``mode`` unattended, while they still hold its
    permission; None when the consent is missing or has lapsed."""
    by = ((state.get("consents") or {}).get(mode) or {}).get("by")
    if missing_mode_permission(settings, await _member_role(session, company_id, by), mode):
        return None
    return SimpleNamespace(id=uuid.UUID(str(by)))


def missing_mode_permission(settings: dict, role: str | None, mode: str) -> str | None:
    """The first permission running ``mode`` unattended needs that ``role`` lacks (no role
    lacks them all), or None when it holds both."""
    for permission in (MODE_PERMISSION[mode], MODE_DOCUMENT_PERMISSION[mode]):
        if role is None or not role_has_permission(settings, role, permission):
            return permission
    return None


def sends_itself(state: dict) -> bool:
    """Whether a billed period's waiting email goes out on its own: automatic sending is in
    force. Otherwise it waits only for someone to send it."""
    return "sending" in modes_in_force(state)


def send_status(state: dict, k, today: date, doc: dict | None = None) -> str | None:
    """How period ``k``'s email reads, for the Periods table, the summary and the send answer
    alike: "sent" once its record or its document says it went (the document's own page can
    send it), "waiting" while the next pass sends it (waiting_sends), "not_sent" when nothing
    will send it unless someone does, "failed", or "unconfirmed" (it may not have arrived).
    None when the period has no email."""
    recorded = ((periods_of(state).get(str(k)) or {}).get("send") or {}).get("state")
    if recorded is None:
        return None
    if recorded == "sent" or (doc or {}).get("sent_via"):
        return "sent"
    if recorded == "pending":
        return "waiting" if int(k) in waiting_sends(state, today) else "not_sent"
    return "unconfirmed" if recorded in UNCONFIRMED else recorded


def modes_in_force(state: dict) -> list[str]:
    """The unattended modes that run for an agreement, by their consent key. A mode runs only
    while the agreement is active, and sending runs only on what automatic billing makes, so
    Send automatically on a manual agreement is not one of them."""
    if state.get("status") != "active" or state.get("generation_mode") not in AUTO_MODES:
        return []
    return ["generation", *(["sending"] if state.get("auto_send") else [])]


async def run_due(*, session: AsyncSession, company_id, today: date, budget: int) -> OperationReport:
    """The dispatcher's billing pass for one company (the ``periodic_operation`` slot)."""
    report = OperationReport()
    from celerp.modules.registry import uses_module

    held: list[int] = []  # due periods of each agreement held for the catch-up preview
    queued_sends = 0
    for row in await agreements(session, company_id):
        state = row.state
        for k in waiting_sends(state, today)[:max(budget - queued_sends, 0)]:
            report.after_commit.append(_deliverer(company_id, row.entity_id, k))
            queued_sends += 1
        found = unattended_due(state, today)
        if found is None:
            continue
        if outside_window(state, *found, today):
            held.append(len(found[1]))
            continue
        while report.done < budget:
            failed_period = None
            try:
                async with session.begin_nested():
                    # Decided again from the agreement as locked: the read above may be stale.
                    company = await locked_company(session, company_id)
                    if (company is None or not company.is_active
                            or not uses_module(company.settings, "celerp-subscriptions")):
                        break
                    proj = await locked_agreement(session, company_id, row.entity_id)
                    found = unattended_due(proj.state, today)
                    if found is None or outside_window(proj.state, *found, today):
                        break
                    settings = company.settings or {}
                    actor = await authorizer(session, company_id, settings, proj.state, "generation")
                    if actor is None:
                        break
                    send = (bool(proj.state.get("auto_send"))
                            and await authorizer(session, company_id, settings, proj.state, "sending") is not None)
                    k = found[1][0]
                    failed_period = k
                    await _bill(session, company, company_id, row.entity_id, proj.state, found[0], k, actor,
                                auto=True, send=send)
            except Exception:
                log.exception("Unattended billing of %s failed", row.entity_id)
                if failed_period is not None:
                    try:
                        proj = await locked_agreement(session, company_id, row.entity_id)
                        prior = proj.state.get("billing_failure") or {}
                        if prior.get("period_index") != failed_period:
                            await write_agreement(session, company_id, row.entity_id, {
                                "billing_failure": {"period_index": failed_period, "at": now_stamp()},
                            }, None)
                            await notify(session, company_id, "notice_billing_failed",
                                          "/subscriptions/summary")
                    except Exception:
                        log.exception("Recording failed billing of %s failed", row.entity_id)
                break
            report.done += 1
            if send:
                report.after_commit.append(_deliverer(company_id, row.entity_id, k))
    if report.done:
        await notify(session, company_id, "notice_billed", "/subscriptions/summary", day=today.isoformat())
    if held:
        await notify(session, company_id, "notice_catchup", "/subscriptions/catch-up", n=len(held), m=sum(held))
    report.after_commit.append(_expire_attempts_for(company_id))
    return report


def _expire_attempts_for(company_id):
    async def _run(factory) -> None:
        async with factory() as session:
            if await locked_company(session, company_id) is None:
                return
            await session.execute(delete(SubscriptionEmailAttempt).where(
                SubscriptionEmailAttempt.company_id == company_id,
                SubscriptionEmailAttempt.created_at < datetime.now(timezone.utc) - EMAIL_ATTEMPT_RETENTION))
            await session.commit()
    return _run


async def notify(session, company_id, notice: str, url: str, **params) -> None:
    """Tell the company once about ``notice`` with these figures; new figures tell it again."""
    from celerp.notifications.service import notify_once

    title, body = f"subscriptions.{notice}_title", f"subscriptions.{notice}_body"
    await notify_once(session, company_id, "system", t(title, "en"), t(body, "en", **params), action_url=url,
                      i18n={"title": title, "body": body, "params": params})


def _deliverer(company_id, template_id: str, k: int):
    async def _run(factory) -> None:
        await deliver(factory, company_id, template_id, k)
    return _run


async def deliver(factory, company_id, template_id: str, k: int, *, retry_by=None, again: bool = False) -> dict:
    """Send a period's document using one immutable message per transport attempt. A send
    that was cut off is replayed as the same attempt, or with ``again`` (the user confirmed
    it may already have arrived) sent as a new one. A document already sent from its own
    page settles the period's email instead of sending it twice."""
    from celerp.modules.registry import uses_module
    from celerp_docs.doc_constants import NO_SEND_DOC_TYPES
    from celerp_docs.routes import DocSendBody, doc_email, mark_doc_sent

    relay_idempotent = email.relay_send_is_idempotent()
    async with factory() as session:
        proj = await locked_agreement(session, company_id, template_id)
        company = await session.get(Company, company_id)
        if retry_by is None:
            if (company is None or not company.is_active
                    or not uses_module(company.settings, "celerp-subscriptions")
                    or not sends_itself(proj.state)
                    or await authorizer(session, company_id, company.settings or {}, proj.state, "sending") is None):
                return {"state": "pending"}
        periods = periods_of(proj.state)
        entry = periods.get(str(k)) or {}
        send = dict(entry.get("send") or {})
        if entry.get("state") != BILLED:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_not_billed"))
        if send.get("state") not in (("pending", "failed", *UNCONFIRMED) if retry_by else ("pending",)):
            return send

        uncertain = send.get("state") in UNCONFIRMED and not again
        if uncertain and not (relay_idempotent and send.get("deduplicated")):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_unverified"))
        doc_id = entry["doc_id"]
        doc = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        via = (doc.state or {}).get("sent_via") if doc is not None else None
        if via and not again:
            send = {"state": "sent", "n": int(send.get("n") or 0), "at": now_stamp(), "via": via}
            periods[str(k)]["send"] = send
            await write_agreement(session, company_id, template_id, {"periods": periods}, retry_by)
            await session.commit()
            return send
        if uncertain and (doc is None or (doc.state or {}).get("status") == "void"):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_unverified"))
        by = retry_by or ((proj.state.get("consents") or {}).get("sending") or {}).get("by") or entry.get("by")
        actor = SimpleNamespace(id=uuid.UUID(str(by)) if by else None)

        if uncertain:
            # A retry is the SAME attempt, never a new attempt with a reused key.
            n = int(send.get("n") or 0)
            operation_id = send.get("operation_id") or ""
            try:
                attempt = await session.get(
                    SubscriptionEmailAttempt,
                    {"company_id": company_id, "operation_id": uuid.UUID(operation_id)})
            except ValueError:
                attempt = None
            if (attempt is None or attempt.doc_id != doc_id or attempt.attempt_number != n
                    or not retryable_attempt_time(attempt.created_at)):
                raise HTTPException(status_code=409, detail=t("subscriptions.err_send_unverified"))
            message = json.loads(attempt.payload_json)
            digest = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
            if digest != send.get("payload_digest"):
                raise HTTPException(status_code=409, detail=t("subscriptions.err_send_unverified"))
        else:
            n = int(send.get("n") or 0) + 1
            contact_id = proj.state.get("contact_id")
            contact = (await session.get(Projection, {"company_id": company_id, "entity_id": contact_id})
                       if contact_id else None)
            to = ((contact.state or {}).get("email") if contact is not None else None) or ""
            if (doc is None or (doc.state or {}).get("status") == "void" or
                    (doc.state or {}).get("doc_type") in NO_SEND_DOC_TYPES or not to):
                send = {"state": "failed", "n": n, "at": now_stamp(),
                        "error_key": "subscriptions.err_send_no_email" if doc is not None and not to
                        else "subscriptions.err_send_void"}
                periods[str(k)]["send"] = send
                await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
                await session.commit()
                return send
            try:
                async with session.begin_nested():
                    message = await doc_email(session, company_id, doc_id, doc.state, to, actor)
                    html, body = email._with_footer(message["body_html"], message["body_text"])
                    message = {**message, "body_html": html, "body_text": body, "branded": False}
                    payload_json = json.dumps(message, sort_keys=True)
                    if len(payload_json.encode()) > MAX_EMAIL_PAYLOAD_BYTES:
                        raise ValueError("email too large")
            except Exception as exc:
                log.warning("Preparing subscription email failed (%s)", type(exc).__name__)
                send = {"state": "failed", "n": n, "at": now_stamp(),
                        "error_key": "subscriptions.err_send_preparation"}
                periods[str(k)]["send"] = send
                await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
                await session.commit()
                return send

            operation_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"subscription-email:{company_id}:{doc_id}:{n}"))
            digest = hashlib.sha256(payload_json.encode()).hexdigest()
            periods[str(k)]["send"] = {
                "state": "sending", "n": n, "at": now_stamp(), "operation_id": operation_id,
                "deduplicated": relay_idempotent, "payload_digest": digest}
            await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
            session.add(SubscriptionEmailAttempt(
                company_id=company_id, operation_id=uuid.UUID(operation_id), doc_id=doc_id,
                attempt_number=n, payload_json=payload_json))
            await session.commit()

    try:
        result = await email.send_email(**message, operation_id=operation_id, fail_closed_on_uncertain=True)
    except Exception as exc:
        log.warning("Sending subscription email failed (%s)", type(exc).__name__)
        result = email.EmailResult(False, "delivery outcome unknown", unconfirmed=True)
    async with factory() as session:
        proj = await locked_agreement(session, company_id, template_id)
        periods = periods_of(proj.state)
        current = periods.get(str(k)) or {}
        sending = current.get("send") or {}
        operation_uuid = uuid.UUID(operation_id)
        if (current.get("state") != BILLED or current.get("doc_id") != doc_id
                or sending.get("n") != n or sending.get("state") not in UNCONFIRMED
                or sending.get("operation_id") != operation_id):
            await session.execute(delete(SubscriptionEmailAttempt).where(
                SubscriptionEmailAttempt.company_id == company_id,
                SubscriptionEmailAttempt.operation_id == operation_uuid))
            await session.commit()
            return sending

        outcome = "sent" if result.ok else ("unconfirmed" if uncertain or result.unconfirmed else "failed")
        send = {"state": outcome, "n": n, "at": now_stamp(), "operation_id": operation_id,
                "deduplicated": relay_idempotent, "payload_digest": digest,
                **({"error_key": result.reason} if outcome == "failed" else {})}
        periods[str(k)]["send"] = send
        await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
        if result.ok:
            try:
                async with session.begin_nested():
                    await mark_doc_sent(session, company_id, doc_id, actor, DocSendBody(
                        sent_via="email", sent_to=message["to"],
                        idempotency_key=f"send:{doc_id}:{n}"), source="subscription")
            except HTTPException as exc:
                log.warning("Document was emailed but could not be marked sent (%s)", exc.status_code)
        if outcome != "unconfirmed":
            await session.execute(delete(SubscriptionEmailAttempt).where(
                SubscriptionEmailAttempt.company_id == company_id,
                SubscriptionEmailAttempt.operation_id == operation_uuid))
        await session.commit()
    return send


@asynccontextmanager
async def same_session(session: AsyncSession):
    """A session factory that hands out the caller's own session."""
    yield session
