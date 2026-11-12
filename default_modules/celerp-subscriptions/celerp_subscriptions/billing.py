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

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.models.subscription_email import SubscriptionEmailAttempt
from celerp.services import email
from celerp.services.business_time import business_today
from celerp.services.company_lock import lock_projections, locked_company
from celerp.services.document_lines import strip_line_ids
from celerp.services.money import document_line_amount, document_line_unit, round_money, to_stored_float
from celerp.services.peer_lines import granted_from_stored
from celerp.services.permissions import missing_permission, role_has_permission
from celerp.services.record_quantities import template_label
from celerp.services.terms import resolve_document_terms
from celerp.services.units import require_moving_fits_units
from celerp_docs.doc_money import HEADER_MONEY_FIELDS, document_money
from celerp_docs.periodic import OperationReport
from celerp_subscriptions.periods import (AUTO_MODES, BILLED, COLLECT_MODE, CLAIMED_STATES, RELEASED, SCHEDULE_FIELDS, SKIPPED,
                                          UNRESOLVED, TERMS, Schedule, ScheduleError, check_terms, shown, term_error)
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES
from ui.i18n import t

log = logging.getLogger(__name__)

MODULE = "celerp-subscriptions"
# Each unattended mode and the permission its authorizer must keep holding.
MODE_PERMISSION = {"generation": "automate_billing", "sending": "automate_sending",
                   "collection": "automate_collection"}
MODE_DOCUMENT_PERMISSION = {"generation": "finalize_documents", "sending": "edit_documents",
                            "collection": "record_payments"}
# The settings each mode's consent covers: changing one while the mode runs is a new consent.
# Automatic billing covers every setting its decision reads (unattended_due, outside_window).
CONSENT_TERMS = {"generation": ("generation_mode", "catchup_periods", *SCHEDULE_FIELDS), "sending": ("auto_send",),
                 "collection": ("generation_mode",)}
# The longest a send waits for the mail server, and the longest a send can take with recording its
# answer: a "sending" record younger than SEND_TIMEOUT may still be with the mail server (send_state).
TRANSPORT_TIMEOUT = timedelta(minutes=4)
SEND_TIMEOUT = TRANSPORT_TIMEOUT + timedelta(minutes=1)
# The agreement write_agreement is writing: the one writer allowed to change an agreement's
# status (guard_agreement_status), set by nothing else (consent_census holds it).
_WRITING: ContextVar[str | None] = ContextVar("agreement_write", default=None)
MAX_EMAIL_PAYLOAD_BYTES = 2 * 1024 * 1024
EMAIL_ATTEMPT_RETENTION = timedelta(days=2)
RECOVERY_RETRY_AGE = timedelta(hours=12)


def _payload_json(message: dict) -> str:
    """The stored form of an email a send gives the relay, and the text its digest is taken
    of: every character past ASCII written as itself, so the size limit counts it once."""
    return json.dumps(message, sort_keys=True, ensure_ascii=False)


def now() -> datetime:
    """Wall-clock time (not the business day): what a record is stamped with and read at."""
    return datetime.now(timezone.utc)


def now_stamp() -> str:
    """Wall-clock time of a record, read back with stamp_time."""
    return now().isoformat()


def stamp_time(at) -> datetime | None:
    """A record's stored time in UTC (one with no zone is read as UTC); None when unreadable,
    including a time at the edge of the calendar that has no UTC equivalent."""
    try:
        when = datetime.fromisoformat(str(at))
        return when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


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
    ``scheduled`` (every operation that reads the schedule; pause, cancel and Activate do not),
    an agreement its stored terms build no schedule from is refused (refuse_unscheduled)."""
    # Postgres stores no NUL, so an id holding one names no agreement.
    proj = (await lock_projections(session, company_id, [entity_id])).get(entity_id) if "\x00" not in entity_id else None
    if not proj or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
    if scheduled:
        refuse_unscheduled(proj.state, entity_id)
    return proj


def refuse_unscheduled(state: dict, entity_id: str) -> None:
    """A 409 naming why, unless the agreement's stored terms build a schedule: a template not
    yet moved onto one (refuse_not_moved), an agreement with no start of its schedule yet (a
    draft, until Activate gives it one), or terms that describe none (the schedule's reason)."""
    refuse_not_moved(state, entity_id)
    if not state.get("anchor_date"):
        raise HTTPException(status_code=409, detail=t(
            "subscriptions.err_no_periods", ref=state.get("ref_id") or entity_id))
    if (exc := unscheduled(state)) is not None:
        raise schedule_refusal(409, exc) from exc


def unscheduled(state: dict) -> ScheduleError | None:
    """Why the agreement's stored terms build no schedule, or None when they build one or, with
    no start of its schedule yet, every term holds what TERMS declares as Activate reads it
    (migration.legacy_terms). The one test every writer and every reader of the stored terms
    asks; anything else that fails reading them (a period key that is not a number, a period
    that is not a record) reads as unreadable, never as a server error."""
    from celerp_subscriptions import migration

    try:
        if state.get("anchor_date"):
            Schedule.from_state(state)
        else:
            check_terms({**state, **migration.legacy_terms(state)})
    except ScheduleError as exc:
        return exc
    except Exception:
        return ScheduleError("subscriptions.err_schedule_unreadable")
    return None


def on_schedule(state: dict) -> bool:
    """The agreement has a start of its schedule and its stored terms build one."""
    return bool(state.get("anchor_date")) and unscheduled(state) is None


def refuse_unbuildable_terms(before: dict, after: dict) -> None:
    """A 422 naming the schedule's reason when a write leaves an agreement's stored terms
    building no schedule where they built one before (or nothing was stored): no writer stores
    such terms. Every term the write sets is checked as sent, so a value only an earlier
    release's reading accepts (an interval of 0, which it billed as 30 days) is refused when
    sent now, and no write takes an agreement off its schedule. A write to terms an earlier
    release left unbuildable is not refused, so voiding or repairing the agreement still works."""
    written = [f for f in TERMS if after.get(f) not in (None, "") and after.get(f) != before.get(f)]
    if before.get("anchor_date") and not after.get("anchor_date"):
        exc = ScheduleError("subscriptions.err_schedule_removed")
    else:
        exc = next((e for f in written if (e := term_error(f, after[f]))), None)
    if exc is None and unscheduled(before) is None:
        exc = unscheduled(after)
    if exc is not None:
        raise schedule_refusal(422, exc) from exc


def schedule_refusal(status_code: int, exc: ScheduleError) -> HTTPException:
    """The refusal saying why stored terms build no schedule, keyed so it reads in the
    reader's language."""
    return HTTPException(status_code=status_code, detail=catalog_refusal(exc.key, **exc.params))


def refuse_not_moved(state: dict, entity_id: str) -> None:
    """A 409 naming the template and why, while it is not yet moved onto a schedule."""
    if (reason := not_moved(state)) is not None:
        raise not_moved_refusal(state, entity_id, reason)


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

    It is also where consent to an unattended mode is given, whatever the status: a write that
    chooses a mode (configured_modes) or changes one of its consent terms while it is chosen
    needs both its permissions from the actor's role (403 naming the first missing) and records
    the actor's consent; the consents kept are exactly the modes chosen after the change, so
    turning a mode off drops its consent and a status change keeps it. A write that changes the
    status may not move a consent (ValueError): status and terms are written apart. Whatever
    it changes, a write that brings a mode into force (entering_modes, as Resume and Activate
    do) also needs that mode's permissions from the actor, and moves no consent for it. A void
    agreement leaves void only through Documents unvoid, so a status write here on one is refused
    (409 naming unvoid) and the void is never rewritten as another status. An ENDED agreement
    bills no period again, so a person's write that changes what its periods claim is refused
    (ended_refusal), whichever action makes it, a placement's withdrawal included; its terms, a
    period's send record and its history review (Set aside, and Return of a set-aside) still change.
    A change that ends the agreement's billing failure (ends_failure) drops it in the same event;
    one that only stops it holding (a pause, billing by hand, a new schedule) leaves it to run_due's
    next pass, as does a void, which Documents writes without passing here."""
    before = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id == entity_id))).scalar_one_or_none() or {}
    changes = {k: v for k, v in changes.items() if shown(k, before.get(k)) != shown(k, v)}
    if before.get("status") == "void" and "status" in changes:
        raise HTTPException(status_code=409, detail=t(
            "subscriptions.err_status_void", ref=before.get("ref_id") or entity_id))
    if ends_failure(before, changes):
        drop = (*drop, "billing_failure")
    drop = tuple(k for k in drop if k in before and k not in changes)
    if actor_id is not None and (refusal := ended_refusal(
            before, {**{k: v for k, v in before.items() if k not in drop}, **changes})):
        raise ended_answer(refusal, before, entity_id)
    if not changes and all(shown(k, before[k]) == shown(k, None) for k in drop):
        drop = ()
    merged = {**{k: v for k, v in before.items() if k not in drop}, **changes}
    chosen, choosing = configured_modes(before), configured_modes(merged)
    needed = [m for m in choosing if m not in chosen or any(f in changes for f in CONSENT_TERMS[m])]
    await require_mode_permissions(session, company_id, actor_id,
                                   [*needed, *(m for m in entering_modes(before, merged) if m not in needed)])
    if "collection" in needed:
        from celerp_subscriptions.collect import require_collectable
        await require_collectable(session, company_id, merged)
    kept = before.get("consents") or {}
    consents = {**{m: kept[m] for m in choosing if m in kept},
                **{m: {"by": str(actor_id), "at": now_stamp()} for m in needed}}
    if consents != kept and "status" in changes:
        raise ValueError(f"a status change of {entity_id} would move its consents")
    if consents != kept:
        changes, drop = ({**changes, "consents": consents}, drop) if consents else (changes, (*drop, "consents"))
    if not changes and not drop:
        return
    after = {**{k: v for k, v in before.items() if k not in drop}, **changes}
    fields = {**{k: {"old": before.get(k), "new": v} for k, v in changes.items()},
              **{k: {"old": before.get(k), "drop": True} for k in drop}}
    # Periods an earlier release left as something other than a mapping hold no period to name.
    old_periods, new_periods = (s["periods"] if isinstance(s.get("periods"), dict) else {} for s in (before, after))
    touched = {k for k in {*old_periods, *new_periods} if old_periods.get(k) != new_periods.get(k)}
    if (failed := (changes.get("billing_failure") or {}).get("period_index")) is not None:
        touched.add(str(failed))
    if touched and on_schedule(after):
        # A key removed from bookkeeping an earlier release left unreadable is not a number.
        schedule = Schedule.from_state(after)
        dates = {k: [schedule.start(int(k)).isoformat(), schedule.service_end(int(k)).isoformat()]
                 for k in touched if k.lstrip("-").isdigit()}
        for k in ("periods", "billing_failure"):
            if dates and k in changes:
                fields[k]["dates"] = dates
    if "periods" in changes:
        # The document each touched period was billed on, by its number then, so History names it.
        claimed = {k: (new_periods.get(k) or {}).get("doc_id") or (old_periods.get(k) or {}).get("doc_id")
                   for k in touched}
        if refs := {k: await doc_ref(session, company_id, d) for k, d in claimed.items() if d}:
            fields["periods"]["refs"] = refs
    if "beneficiary_contact_id" in changes:
        # The member before and after by the name each contact had then, so History names them.
        members = {fields["beneficiary_contact_id"]["old"], changes["beneficiary_contact_id"]} - {None}
        if names := {c: n for c in members if (n := ((await doc_state(session, company_id, c)) or {}).get("name"))}:
            fields["beneficiary_contact_id"]["names"] = names
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


# The statuses an agreement holds. Only write_agreement (its own page) moves it between draft,
# active, paused and cancelled; Documents may also void it, unvoid it (the only way out of void) or
# revert it to draft.
AGREEMENT_STATUSES = frozenset({"draft", "active", "paused", "cancelled", "void"})


async def _refuse_unknown_member(session, company_id, before: dict, after: dict) -> None:
    """422 (subscriptions.err_beneficiary) when a write sets the member to anything but a contact
    of the company; a member left as it was is not read again."""
    member = after.get("beneficiary_contact_id")
    if member is None or member == before.get("beneficiary_contact_id"):
        return
    row = await session.get(Projection, (company_id, member))
    if row is None or row.entity_type != "contact":
        raise HTTPException(status_code=422, detail=catalog_refusal("subscriptions.err_beneficiary"))


async def guard_agreement_status(*, session, entry, transition) -> None:
    """doc_status_guard: no write of a subscription document, by any writer, leaves its terms
    building no schedule (refuse_unbuildable_terms). The status of an agreement that runs an
    unattended mode, before or after the change, changes only in write_agreement (activate, pause, resume, cancel on its
    own page). Every other writer (void, finalize, revert, an import) is refused (409), except Unvoid,
    the only way out of void (write_agreement writes no status on a void agreement); so is any
    that would leave an agreement a status outside AGREEMENT_STATUSES. A draft starts only
    through Activate, which writes its schedule, so a draft leaves draft elsewhere only by void;
    that refusal names Activate. An agreement without a schedule bills nothing (billable). A
    company that turned the module off runs no mode, so Documents can stop its agreements. A
    change that brings a mode into force (an unvoid, or one made for when the module is turned on
    again) needs that mode's permissions from whoever makes it, as in write_agreement."""
    from celerp.modules.registry import uses_module

    if transition.after.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
        return
    refuse_unbuildable_terms(transition.before or {}, transition.after)
    await _refuse_unknown_member(session, entry.company_id, transition.before or {}, transition.after)
    if _WRITING.get() == entry.entity_id or (transition.before or {}).get("status") == transition.after.get("status"):
        return
    status, was = transition.after.get("status"), (transition.before or {}).get("status")
    company = await session.get(Company, entry.company_id)
    in_force = modes_in_force(transition.before or {}) or modes_in_force(transition.after)
    if was != "void" and in_force and company is not None and uses_module(company.settings, MODULE):
        raise HTTPException(status_code=409, detail=t("subscriptions.err_agreement_status_elsewhere"))
    if was == "draft" and status not in ("draft", "void"):
        raise HTTPException(status_code=409, detail=t("subscriptions.err_agreement_activate"))
    if status not in AGREEMENT_STATUSES:
        raise HTTPException(status_code=409, detail=t("subscriptions.err_agreement_status_lifecycle"))
    await require_mode_permissions(session, entry.company_id, entry.actor_id,
                                   entering_modes(transition.before or {}, transition.after))


def billable(state: dict) -> bool:
    """The one rule for whether an agreement's periods can bill: it is active and on a
    schedule its stored terms build (on_schedule). Every other status, named or not, bills
    nothing."""
    return state.get("status") == "active" and on_schedule(state)


# Why a status does not bill, for the ones an agreement holds; any other reads the last.
_UNBILLABLE = {"draft": "subscriptions.err_bill_draft", "paused": "subscriptions.err_bill_paused",
               "cancelled": "subscriptions.err_generate_cancelled", "void": "subscriptions.err_bill_void"}


# The statuses no action on the agreement's page leaves (cancelled is final, void leaves only
# through Documents unvoid), each with why a person's change to what its periods claim is
# refused. Such an agreement bills no period again, so its page shows no bill date and its
# unclaimed periods read "not_billing".
ENDED = {"cancelled": "subscriptions.err_ended_cancelled", "void": "subscriptions.err_ended_void"}
# Why placing an earlier document in a period of one is refused, naming Set aside: the way its
# history review still finishes.
ENDED_PLACE = {"cancelled": "subscriptions.err_ended_place_cancelled", "void": "subscriptions.err_ended_place_void"}


def failure_of(state: dict) -> dict:
    """The agreement's billing failure as run_due writes it: a mapping of its period_index, at and
    reason. A stored value that is not a mapping holds nothing to read; the next pass drops it."""
    failure = state.get("billing_failure")
    return failure if isinstance(failure, dict) else {}


def ends_failure(before: dict, changes: dict) -> bool:
    """Whether a change written through write_agreement ends the agreement's billing failure in
    that event: its period is billed or skipped, or the agreement ends (ENDED, as Cancel does).
    The failure is one current fact, which run_due writes: anything else that stops it holding (a
    pause, billing by hand, a new schedule, or a void, which Documents writes without passing
    here) leaves it stored until run_due's next pass drops it (still_failing)."""
    failure = failure_of(before)
    if not failure:
        return False
    entry = (changes.get("periods") or {}).get(str(failure.get("period_index")))
    return changes.get("status") in ENDED or (isinstance(entry, dict) and entry.get("state") in (BILLED, SKIPPED))


def claims(state: dict) -> tuple:
    """What an agreement's periods claim: each period holding a bill or a skip (CLAIMED_STATES),
    without its send record, and the bills periods let go. A period waiting for history review
    or open again claims nothing, nor do the review's own lists."""
    periods = state.get("periods")
    claimed = {k: {f: v for f, v in e.items() if f != "send"} for k, e in (periods.items() if isinstance(periods, dict) else ())
               if isinstance(e, dict) and e.get("state") in CLAIMED_STATES}
    return claimed, state.get("released_bills")


def ended_key(state: dict, *, placing: bool = False) -> str | None:
    """Why an ENDED agreement refuses a change to what its periods claim, or None when it runs:
    placing an earlier document names Set aside, the way its history review finishes."""
    return (ENDED_PLACE if placing else ENDED).get(state.get("status"))


def ended_answer(key: str, state: dict, entity_id: str) -> HTTPException:
    return HTTPException(status_code=409, detail=catalog_refusal(key, ref=state.get("ref_id") or entity_id))


def refuse_ended(state: dict, entity_id: str, *, placing: bool = False) -> None:
    """The 409 of ended_key for a door that changes what a period claims, answered before any
    other refusal of the period, so every door a refusal names is open there and can be undone."""
    if (key := ended_key(state, placing=placing)) is not None:
        raise ended_answer(key, state, entity_id)


def ended_refusal(before: dict, after: dict) -> str | None:
    """Why a write turning ``before`` into ``after`` is refused on an ENDED agreement, or None.
    What its periods claim no longer changes, placements included (they are final like bills),
    so every claim is as the agreement ended; only adding history placements names Set aside."""
    (was, released), (now, released_after) = claims(before), claims(after)
    if (was, released) == (now, released_after):
        return None
    placing = released == released_after and all(k in now for k in was) and all(
        e.get("history") for k, e in now.items() if was.get(k) != e)
    return ended_key(before, placing=placing)


def refuse_unbillable(state: dict) -> None:
    """A 409 naming why, unless the agreement is billable: its status, or why its stored
    terms build no schedule."""
    if state.get("status") == "active" and (exc := unscheduled(state)) is not None:
        raise schedule_refusal(409, exc) from exc
    if not billable(state):
        raise HTTPException(status_code=409, detail=t(
            _UNBILLABLE.get(state.get("status"), "subscriptions.err_bill_not_active")))


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


async def billing_doc(session, company_id, doc_id: str) -> None:
    """Refused (409) when the document a period claims does not bill the period: back in draft
    (finalize it, or Reopen), or void or gone (Reopen)."""
    doc = await doc_state(session, company_id, doc_id)
    if not bills(doc):
        key = "subscriptions.err_period_draft" if (doc or {}).get("status") == "draft" else "subscriptions.err_period_void"
        raise HTTPException(status_code=409, detail=t(key, ref=(doc or {}).get("ref_id") or doc_id))


async def guard_bill_status(*, session, entry, transition) -> None:
    """doc_status_guard: a document an agreement period let go (Reopen records it in
    ``released_bills``) that comes back into billing, whichever writer issues it (unvoid,
    finalize, send), claims its period again, whatever the agreement's status: Unvoid and
    Finalize undo Reopen. A newer document holding the period that bills nothing is let go as
    Reopen lets it go. Refused (409) only when a newer bill already bills the period. The claim
    is written under the agreement's company lock, as Reopen and Generate due take it, so a
    concurrent bill and the claim never both hold the period."""
    if bills(transition.before) or not bills(transition.after):
        return
    doc_id, company_id = entry.entity_id, entry.company_id
    ids = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(SUBSCRIPTION_DOC_TYPES),
        Projection.state["released_bills"][doc_id].as_string().is_not(None),
    ).order_by(Projection.entity_id))).scalars().all()
    for agreement_id in ids:
        agreement = await locked_agreement(session, company_id, agreement_id, scheduled=False)
        released = dict(agreement.state.get("released_bills") or {})
        if doc_id not in released:
            continue
        k = int(released.pop(doc_id))
        ref, label = transition.before.get("ref_id") or doc_id, template_label(agreement.state, agreement_id)
        periods = periods_of(agreement.state)
        held = periods.get(str(k)) or {}
        newer = held.get("doc_id") if held.get("state") == BILLED else None
        newer_doc = await doc_state(session, company_id, newer) if newer else None
        if bills(newer_doc):
            # The period's dates as the newer bill states them: they need no schedule.
            raise HTTPException(status_code=409, detail=t(
                "subscriptions.err_bill_rebilled", ref=ref, agreement=label, newer=newer_doc.get("ref_id") or newer,
                start=newer_doc.get("service_start") or "--", end=newer_doc.get("service_end") or "--"))
        if newer:
            released[newer] = k
        periods[str(k)] = {"state": BILLED, "doc_id": doc_id, "release_count": held.get("release_count", 0),
                           "at": now_stamp(), "by": str(entry.actor_id) if entry.actor_id else None, "auto": False}
        # Not a person's change to an ended agreement's claims (ended_refusal): the undo of Reopen.
        await write_agreement(session, company_id, agreement_id, {"periods": periods, "released_bills": released}, None)
        if newer:
            await discard_email_attempts(session, company_id, newer)


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


def amendments_of(state: dict, *, settled_outside: int | None = None) -> list[dict]:
    """The agreement's amendments, oldest effective date first (one made later on the same day
    after the earlier). With ``settled_outside``, those whose change period ``k`` already billed
    through their own credit and debit documents are left out: billing that period again bills
    the terms it was first billed on, and those documents keep the change."""
    stored = state.get("amendments")
    return [a for a in (stored if isinstance(stored, list) else [])
            if isinstance(a, dict) and (settled_outside is None or a.get("period_index") != settled_outside)]


def lines_on(state: dict, day: date, amendments: list[dict] | None = None) -> list[dict]:
    """The agreement's lines in force on ``day``: those of the latest amendment effective by
    then, else its own. Only mappings are lines."""
    in_force = [a for a in (amendments_of(state) if amendments is None else amendments)
                if str(a.get("effective")) <= day.isoformat()]
    stored = in_force[-1].get("line_items") if in_force else state.get("line_items")
    return [li for li in copy.deepcopy(stored if isinstance(stored, list) else []) if isinstance(li, dict)]


def prorated(lines: list[dict], first: date, last: date, period_days: int, currency: str) -> list[dict]:
    """``lines`` (each priced for a whole period of ``period_days``) for the days ``first`` to
    ``last``: each line keeps its quantity and unit price, its total is its share of the days,
    and it names those days as its service dates. A line whose amount cannot be read keeps it,
    for the bill's own checks to refuse."""
    days = (last - first).days + 1
    service = {"service_start": first.isoformat(), "service_end": last.isoformat()}
    out = []
    for li in lines:
        amount = document_line_amount(li, currency)
        share = {} if amount is None else {"line_total": to_stored_float(round_money(amount * days / period_days, currency))}
        out.append({**li, **share, **service})
    return out


def period_lines(state: dict, schedule: Schedule, k: int, currency: str) -> list[dict]:
    """The lines period ``k`` bills. Amendments effective inside it split it into segments, each
    billing the lines in force from its first day, prorated by its days of the period; with
    none, the lines in force on its first day for the whole period."""
    start, end = schedule.start(k), schedule.service_end(k)
    amendments = amendments_of(state, settled_outside=k)
    cuts = sorted({d for a in amendments if start < (d := date.fromisoformat(str(a["effective"]))) <= end})
    if not cuts:
        return [{**li, "service_start": start.isoformat(), "service_end": end.isoformat()}
                for li in lines_on(state, start, amendments)]
    starts = [start, *cuts]
    ends = [*(d - timedelta(days=1) for d in cuts), end]
    days = (end - start).days + 1
    return [li for first, last in zip(starts, ends) for li in prorated(lines_on(state, first, amendments), first, last, days, currency)]


def agreement_currency(state: dict, company: Company) -> str:
    return state.get("currency") or (company.settings or {}).get("currency", "USD")


async def agreement_document(session, company: Company, tid: str, state: dict, doc_type: str, lines: list[dict],
                             service: dict) -> dict:
    """A document the agreement issues of ``doc_type`` with ``lines`` (each already naming its
    service dates), for its contact, in its currency and money terms, issued today, linked to
    the agreement and period by ``service``. The document is a record of its own, so its lines
    get ids of their own."""
    today = business_today((company.settings or {}).get("timezone"))
    contact_id = state.get("contact_id")
    contact = (await session.get(Projection, {"company_id": company.id, "entity_id": contact_id})) if contact_id else None
    contact_state = (contact.state or {}) if contact is not None and contact.entity_type == "contact" else {}
    linked = {"source_agreement": service["source_agreement"], "period_index": service["period_index"]}
    lines = [{**li, **linked} for li in strip_line_ids(lines)]
    auto_note = f"Auto-created from subscription template {template_label(state, tid)}"
    notes = f"{state['notes']}\n{auto_note}".strip() if state.get("notes") else auto_note
    money_inputs = {f: state[f] for f in HEADER_MONEY_FIELDS if f in state}
    currency = money_inputs["currency"] = agreement_currency(state, company)
    money = document_money(money_inputs, lines, currency, keep_unrated_tax=True)
    doc = {
        "doc_type": doc_type, "contact_id": contact_id,
        "contact_name": contact_state.get("name") or "", "contact_company_name": contact_state.get("company_name") or "",
        "line_items": lines, "payment_terms": state.get("payment_terms"), **money_inputs, **money, "amount_outstanding": money["total"], "issue_date": today.isoformat(),
        "notes": notes, **service,
    }
    if due := compute_due_date(today, state.get("payment_terms"), company):
        doc["due_date"] = due
    return doc


async def bill_document(session, company: Company, tid: str, state: dict, schedule: Schedule, k: int) -> dict:
    """The document period ``k`` of the agreement bills, as _bill creates it before its reference."""
    service = {"source_agreement": tid, "period_index": k,
               "service_start": schedule.start(k).isoformat(), "service_end": schedule.service_end(k).isoformat()}
    lines = period_lines(state, schedule, k, agreement_currency(state, company))
    return await agreement_document(session, company, tid, state, doc_kind(state), lines, service)


async def issue_document(session, company: Company, tid: str, state: dict, doc: dict, actor, key: str) -> str:
    """Create ``doc`` for the agreement under the idempotency ``key``, numbered and finalized.
    Caller holds the locks and commits."""
    from celerp_docs.routes import finalize_document
    from celerp_docs.sequences import claim_number, draft_counter

    kind = doc["doc_type"]
    # The template may predate a precision change; its units are checked on the document it makes.
    await require_moving_fits_units(session, company.id, tid, "doc", doc, template=template_label(state, tid))
    ref = await claim_number(session, company, draft_counter(kind))
    doc_id = f"doc:{ref}"
    doc = {**doc, "ref_id": ref, **resolve_document_terms(state, company.settings or {}, kind)}
    if "customer_note" in state:
        doc["customer_note"] = state.get("customer_note") or ""
    await emit_event(session, fund_grant=granted_from_stored(doc["line_items"]),
                     company_id=company.id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
                     data=doc, actor_id=actor.id, location_id=None, source="subscription", idempotency_key=key)
    await session.flush()
    await finalize_document(doc_id, company.id, actor, session, commit=False)
    await session.flush()
    return doc_id


class BillRefused(HTTPException):
    """A refusal the bill path met (409): the agreement as the subscriptions list names it, the
    reason inside, then the one way out its status leaves. A draft's lines still change on its
    page, so a draft is told to change them there and Activate; nothing on an activated agreement
    changes, so it is told to cancel it and create a new subscription that bills."""

    def __init__(self, state: dict, tid: str, exc: Exception):
        from celerp.error_body import as_refusal

        key = "subscriptions.err_bill_refused_draft" if state.get("status") == "draft" else "subscriptions.err_bill_refused"
        super().__init__(status_code=409, detail=catalog_refusal(
            key, ref=template_label(state, tid), reason=as_refusal(exc.detail)))


class _Unkept(Exception):
    """Ends bill_refusal's savepoint after a bill the path accepted, so nothing it wrote is kept."""


async def bill_refusal(session, company: Company, tid: str, held: dict, changes: dict,
                       actor) -> BillRefused | None:
    """Why the agreement's next bill would be refused once ``changes`` are written over ``held``,
    or None: the bill path itself (_bill) run on the result for its oldest open period inside a
    savepoint that is rolled back, so its refusals are the path's own and nothing it wrote is kept.
    The refusal names the way out ``held``'s status leaves. The doors that make an agreement bill
    (Activate, Resume, Bill automatically) ask it, holding the company lock first. Objects the
    path changed are read again afterwards, so the caller reads what it held before asking."""
    state = {**held, **changes}
    k = Schedule.from_state(state).oldest_open(floor=review_floor(state))
    if k is None:
        return None
    refused = None
    try:
        async with savepoint(session):
            await _bill(session, company, company.id, tid, state, Schedule.from_state(state), k, actor,
                        auto=False, send=False)
            raise _Unkept
    except _Unkept:
        pass
    except BillRefused as exc:
        refused = BillRefused(held, tid, exc.__cause__)
    await session.refresh(company)
    return refused


async def _bill(session, company, company_id, tid: str, state: dict, schedule: Schedule, k: int, actor,
                *, auto: bool, send: bool, request: dict | None = None, key: str | None = None) -> str:
    """Create, finalize and claim period ``k``'s document. Caller holds the locks and commits.
    Every refusal on the way to the finalized document (its units, its content, finalize's own
    checks) is a BillRefused. A billing ``request`` under its idempotency ``key`` keeps its
    answer on the claim's own event, for replay_result to repeat."""
    from celerp.accounting_roles import KeyedRefusal

    kind = doc_kind(state)
    entry = (state.get("periods") or {}).get(str(k)) or {}
    release = entry.get("release_count", 0)
    claim_key = f"sub:{tid}:p{k}:{kind}:r{release}"
    if (earlier := await find_event_by_idempotency(session, company_id, claim_key)) is not None:
        return earlier.entity_id
    try:
        doc = await bill_document(session, company, tid, state, schedule, k)
        doc_id = await issue_document(session, company, tid, state, doc, actor, claim_key)
    except (HTTPException, KeyedRefusal) as exc:
        raise BillRefused(state, tid, exc) from exc
    if state.get("generation_mode") == COLLECT_MODE:
        from celerp.services.collection import open_attempt
        await open_attempt(session, company.id, (await lock_projections(session, company.id, [doc_id]))[doc_id], 1)
    periods = periods_of(state)
    periods[str(k)] = {"state": BILLED, "doc_id": doc_id, "release_count": release, "at": now_stamp(),
                       "by": str(actor.id) if actor.id else None, "auto": auto}
    if send:
        record_send(periods, k, None, state="pending", n=0)
    metadata = None
    if key:
        claimed = replace(schedule, periods={int(i): p for i, p in periods.items()})
        metadata = {"request": request, "result": await _result(
            session, company_id, doc_id, k, claimed, state, already_billed=False)}
    await write_agreement(session, company_id, tid, {"periods": periods}, actor.id, key=key, metadata=metadata)
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
    refuse_unbillable(state)
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


def amend_refusal(status_code: int, key: str, **params) -> HTTPException:
    return HTTPException(status_code=status_code, detail=catalog_refusal(f"subscriptions.{key}", **params))


def amended_lines(current: list[dict], quantities: dict[str, float], currency: str) -> list[dict]:
    """``current`` with each line named in ``quantities`` at its new quantity and the same unit
    charge, so its total follows; a line whose unit charge cannot be read keeps none, for the
    bill's own checks to refuse."""
    out = []
    for li in current:
        if (line_id := li.get("line_id")) not in quantities:
            out.append(li)
            continue
        qty = quantities[line_id]
        unit = document_line_unit(li, currency)
        changed = {k: v for k, v in li.items() if k != "line_total"}
        out.append({**changed, "quantity": qty,
                    **({} if unit is None else {"line_total": to_stored_float(round_money(unit * Decimal(str(qty)), currency))})})
    return out


async def amend(session: AsyncSession, company_id, tid: str, *, effective: date, quantities: dict[str, float],
                request_key: str, actor) -> dict:
    """Change line quantities from ``effective`` on. The agreement keeps each amendment as the
    full set of lines in force from its date (lines_on), so every period from then bills them
    (period_lines). A date inside the latest billed period settles the days from it to the
    period's end (or the next amendment) at once: a credit note for the old terms against the
    document that billed them, which it settles, and a document for the new terms, both linked
    to the agreement and the period. A date before that period's start is refused, as is a
    quantity below 0, a line the agreement does not have and a change that changes nothing. The
    same ``request_key`` repeats the first answer; with a different request it is refused.
    Caller commits."""
    company = await locked_company(session, company_id)
    key = f"amend:{tid}:{request_key}"
    request = {"effective": effective.isoformat(), "quantities": quantities}
    if (prior := await find_event_by_idempotency(session, company_id, key)) is not None:
        if (prior.metadata_ or {}).get("request") != request:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_request_key_reused"))
        return prior.metadata_["result"]
    proj = await locked_agreement(session, company_id, tid)
    state = proj.state
    refuse_ended(state, tid)
    if state.get("status") == "draft":
        raise amend_refusal(409, "err_amend_draft")
    schedule = Schedule.from_state(state)
    billed = max((k for k, p in schedule.periods.items() if p.get("state") == BILLED), default=None)
    if billed is not None and effective < schedule.start(billed):
        raise amend_refusal(409, "err_amend_before_billed", day=schedule.start(billed).isoformat())
    if any(q < 0 for q in quantities.values()):
        raise amend_refusal(422, "err_amend_quantity")
    current = lines_on(state, effective)
    if set(quantities) - {li.get("line_id") for li in current}:
        raise amend_refusal(422, "err_amend_line")
    if all(li.get("quantity") == quantities[li.get("line_id")] for li in current if li.get("line_id") in quantities):
        raise amend_refusal(422, "err_amend_unchanged")
    currency = agreement_currency(state, company)
    new = amended_lines(current, quantities, currency)
    day = effective.isoformat()
    entry = {"effective": day, "line_items": new, "at": now_stamp(), "by": str(actor.id) if actor.id else None}
    result = {"effective": day, "credit_note_id": None, "invoice_id": None}
    if billed is not None and effective <= schedule.service_end(billed):
        settled = await settle_amendment(session, company, tid, state, schedule, billed, effective, current, new,
                                         actor, key)
        result.update(settled)
        entry.update(period_index=billed, **settled)
    amendments = sorted([*amendments_of(state), entry], key=lambda a: str(a.get("effective")))
    await write_agreement(session, company_id, tid, {"amendments": amendments}, actor.id, key=key,
                          metadata={"request": request, "result": result})
    return result


async def settle_amendment(session, company: Company, tid: str, state: dict, schedule: Schedule, k: int,
                           effective: date, old: list[dict], new: list[dict], actor, key: str) -> dict:
    """Settle an amendment effective inside billed period ``k`` (see amend): its days run to the
    period's service end or the day before the next amendment. The credit note credits the
    document that billed those days on the old terms (the period's own, or an earlier
    amendment's within the period), each line naming the line it credits."""
    from celerp.accounting_roles import KeyedRefusal

    day = effective.isoformat()
    period_end = schedule.service_end(k)
    later = [date.fromisoformat(str(a["effective"])) for a in amendments_of(state) if str(a.get("effective")) > day]
    last = min([period_end, *(d - timedelta(days=1) for d in later)])
    in_force = [a for a in amendments_of(state) if str(a.get("effective")) <= day]
    settled_by = in_force[-1] if in_force and in_force[-1].get("period_index") == k else None
    covering_id = settled_by["invoice_id"] if settled_by else schedule.periods[k]["doc_id"]
    covering = await doc_state(session, company.id, covering_id) or {}
    covered = [li for li in covering.get("line_items") or [] if isinstance(li, dict) and
               str(li.get("service_start") or covering.get("service_start")) <= day
               <= str(li.get("service_end") or covering.get("service_end"))]
    days = (period_end - schedule.start(k)).days + 1
    currency = agreement_currency(state, company)
    service = {"source_agreement": tid, "period_index": k, "service_start": day, "service_end": last.isoformat()}
    credits = [{**li, **({"source_line_id": covered[i].get("line_id")} if i < len(covered) else {})}
               for i, li in enumerate(prorated(old, effective, last, days, currency))]
    kind = doc_kind(state)
    note_type = "credit_note" if kind == "invoice" else "debit_note"
    try:
        note = await agreement_document(session, company, tid, state, note_type, credits, service)
        note_id = await issue_document(session, company, tid, state, {**note, "original_doc_id": covering_id},
                                       actor, f"{key}:cn")
        doc = await agreement_document(session, company, tid, state, kind, prorated(new, effective, last, days, currency),
                                       service)
        doc_id = await issue_document(session, company, tid, state, doc, actor, f"{key}:dn")
    except (HTTPException, KeyedRefusal) as exc:
        raise BillRefused(state, tid, exc) from exc
    return {"credit_note_id": note_id, "invoice_id": doc_id}


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
        # One whose stored schedule can't be read holds no claim a reader can trust; its page
        # and the list name it (unscheduled).
        if unscheduled(agreement.state) is None and any(
                entry.get("state") == BILLED and entry.get("doc_id") == doc_id
                for entry in (agreement.state.get("periods") or {}).values()):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))


def period_row(schedule: Schedule, k: int) -> dict:
    return {"period_index": k, "service_start": schedule.start(k).isoformat(),
            "service_end": schedule.service_end(k).isoformat(), "bill_date": schedule.bill_date(k).isoformat()}


# What the Periods table says of a period: its claim, else where its bill date stands today.
PERIOD_STATUSES = ("billed", "draft", "voided", "skipped", "unresolved", "not_billing", "overdue", "due", "upcoming")


def period_status(state: str | None, doc: dict | None, bill_date: date, today: date, *, ended: bool = False) -> str:
    """One of PERIOD_STATUSES. A billed period reads "billed" only while its document bills
    it: back in draft it reads "draft", void or gone "voided"; an open or reopened one reads
    "not_billing" when its agreement has ENDED, else by its bill date."""
    if state == BILLED:
        return "billed" if bills(doc) else "draft" if (doc or {}).get("status") == "draft" else "voided"
    if state in (SKIPPED, UNRESOLVED):
        return state
    if ended:
        return "not_billing"
    return "overdue" if bill_date < today else "due" if bill_date == today else "upcoming"


async def replayable_operations(session, company_id) -> set[str]:
    """The email attempts whose durable payload is still inside the transport's retry window."""
    attempts = (await session.execute(select(
        SubscriptionEmailAttempt.operation_id, SubscriptionEmailAttempt.created_at).where(
            SubscriptionEmailAttempt.company_id == company_id))).all()
    return {str(operation_id) for operation_id, created_at in attempts if retryable_attempt_time(created_at)}


def sends_again(status: str | None, send: dict | None, replayable: set[str]) -> bool:
    """A cut-off send (send_status "unconfirmed") is sent again as a new message, behind a confirm
    that it may already have arrived, unless the relay replays the same message (it cannot arrive
    twice)."""
    send = send or {}
    return status == "unconfirmed" and not (
        send.get("deduplicated") and email.relay_send_is_idempotent() and send.get("operation_id") in replayable)


async def _listed_row(session, company_id, state: dict, schedule: Schedule, k: int, today: date,
                      replayable: set[str]) -> dict:
    """Period ``k`` as the Periods table lists it: dates, claim, status, document, the earlier
    documents it let go, send state, and whether sending it again is a new message."""
    entry = schedule.periods.get(k) or {}
    doc_id = entry.get("doc_id")
    doc = (await doc_state(session, company_id, doc_id)) if doc_id else None
    send, _since = send_status(state, k, today, doc)
    released = [{"doc_id": d, "ref_id": await doc_ref(session, company_id, d)}
                for d, held in sorted((state.get("released_bills") or {}).items()) if int(held) == k]
    return {**period_row(schedule, k), "state": entry.get("state"), "reopened": entry.get("state") == RELEASED and bool(entry.get("reopened")),
            "status": period_status(entry.get("state"), doc, schedule.bill_date(k), today,
                                    ended=state.get("status") in ENDED),
            "doc_id": doc_id, "ref_id": ((doc or {}).get("ref_id") or doc_id) if doc_id else None,
            "reason": entry.get("reason"), "history": bool(entry.get("history")), "released": released,
            "send": send, "attempt": int((entry.get("send") or {}).get("n") or 0),
            "again": sends_again(send, entry.get("send"), replayable)}


async def period_list(session: AsyncSession, company_id, state: dict, today: date, *,
                      limit: int = 24, before: int | None = None) -> dict:
    """An agreement's periods from its lowest to the one after today (and any claimed later),
    newest first, at most ``limit``, with each claim's state, status, document and email send state; its next bill date;
    the earlier documents waiting for history review, each with its candidate periods; and the
    ones set aside as billing no period of their own. An ENDED agreement has no next bill date
    and no oldest due row, and ``ended`` says why it bills nothing. A template not yet on a
    schedule has none of these, and ``not_moved`` says why; an agreement whose stored terms
    build no schedule is refused (409) with the schedule's reason."""
    ended = t(_UNBILLABLE[state["status"]]) if state.get("status") in ENDED else None
    if not state.get("anchor_date"):
        return {"next_bill_date": None, "rows": [], "review": [], "set_aside": [], "ended": ended,
                "older_before": None, "oldest_due_row": None, "not_moved": not_moved(state)}
    if (exc := unscheduled(state)) is not None:
        raise schedule_refusal(409, exc) from exc
    schedule = Schedule.from_state(state)
    nxt = None if ended else schedule.next_bill_date(floor=review_floor(state))
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
    if (before is None and not ended and oldest_open is not None and oldest_open not in
            {r["period_index"] for r in rows} and schedule.bill_date(oldest_open) <= today):
        oldest_due_row = await _listed_row(session, company_id, state, schedule, oldest_open, today, replayable)
    review = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id")),
               "candidates": [period_row(schedule, c) for c in r.get("candidates") or []]}
              for r in state.get("history_review") or []]
    set_aside = [{"doc_id": r.get("doc_id"), "ref_id": await doc_ref(session, company_id, r.get("doc_id"))}
                 for r in state.get("history_set_aside") or []]
    return {"next_bill_date": nxt.isoformat() if nxt else None, "ended": ended,
            "oldest_open_index": oldest_open, "oldest_due_row": oldest_due_row,
            "older_before": older_before, "rows": rows, "review": review, "set_aside": set_aside}


def due_periods(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and its due open periods, for an active agreement with a schedule."""
    if not billable(state):
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
    at = now()
    waiting = sorted(int(k) for k, entry in (state.get("periods") or {}).items()
                     if send_state(entry.get("send"), at)[0] == "not_sent")
    if not waiting or not on_schedule(state) or not sends_itself(state):
        return []
    schedule = Schedule.from_state(state)
    return [k for k in waiting if not outside_window(state, schedule, [k], today)]


def unattended_due(state: dict, today: date) -> tuple[Schedule, list[int]] | None:
    """The schedule and due periods of an agreement that bills itself, with no earlier
    document waiting for review; None when it is not one or nothing is due."""
    if not billable(state) or state.get("generation_mode") not in AUTO_MODES or state.get("history_review"):
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


async def require_mode_permissions(session, company_id, actor_id, modes: list[str]) -> None:
    """403 naming the first permission running any of ``modes`` unattended needs that the
    actor's role lacks (no member lacks them all)."""
    if not modes:
        return
    company = await session.get(Company, company_id)
    role = await _member_role(session, company_id, actor_id)
    for mode in modes:
        if (missing := missing_mode_permission((company.settings if company else None) or {}, role, mode)):
            raise missing_permission(missing)


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


def send_state(send: dict | None, now: datetime, *, emailed: bool = False,
               waits: bool = False) -> tuple[str | None, str | None]:
    """How a send record reads at ``now``, and since when: the one reading of a stored send.
    The state is "sent" once the record or the document says it went (``emailed``: the
    document's own page can send it), "waiting" while the next pass sends it (``waits``),
    "not_sent" when nothing will send it unless someone does, "failed", "sending" while it may
    still be with the mail server (recorded as sending less than SEND_TIMEOUT ago), or
    "unconfirmed" (it may not have arrived). Since is when that state began: the time the
    record was written, except that a send cut off by age reads "unconfirmed" from the moment
    it could no longer be with the mail server. A readable time is given in UTC (stamp_time), so
    one reading has one since, which the summary row's key holds. (None, None) when there is no record."""
    send = send or {}
    recorded, at = send.get("state"), send.get("at")
    if recorded is None:
        return None, None
    if (began := stamp_time(at)) is not None:
        at = began.isoformat()
    if recorded == "sent" or emailed:
        return "sent", at
    if recorded == "pending":
        return ("waiting" if waits else "not_sent"), at
    if recorded == "sending":
        if began is None:
            return "unconfirmed", at
        cut_off = began + SEND_TIMEOUT
        return ("sending", at) if cut_off > now else ("unconfirmed", cut_off.isoformat())
    return recorded, at


def send_written(send: dict | None) -> str | None:
    """How a send record read when it was written, as History describes the event that wrote
    it: send_state at no time after the record (a send recorded as sending was then in flight)."""
    return send_state(send, datetime.min.replace(tzinfo=timezone.utc))[0]


def send_status(state: dict, k, today: date, doc: dict | None = None) -> tuple[str | None, str | None]:
    """Period ``k``'s email as send_state reads it now, for the Periods table, the summary, the
    send answer and Send again alike: whether its document was emailed and whether the next
    pass sends it (waiting_sends) come from the agreement and the document."""
    send = (periods_of(state).get(str(k)) or {}).get("send")
    return send_state(send, now(), emailed=bool(emailed(doc)),
                      waits=int(k) in waiting_sends(state, today))


def emailed(doc: dict | None) -> str | None:
    """How the document went out when it was emailed (the doc.sent projection's ``sent_via``,
    recorded for every send with a recipient, whoever sent it), else None."""
    return (doc or {}).get("sent_via")


def record_send(periods: dict, k, by, **record) -> dict:
    """Write period ``k``'s send record: ``record`` stamped now, with ``by`` the member who
    pressed Send (absent when it went on its own). The one writer of a send record."""
    send = periods[str(k)]["send"] = {**record, "at": now_stamp(), **({"by": str(by)} if by else {})}
    return send


def configured_modes(state: dict) -> list[str]:
    """The unattended modes the agreement's terms choose, by their consent key, whatever its
    status: what a consent is given to. Sending runs only on what automatic billing makes, so
    Send automatically on a manual agreement is not one of them."""
    if state.get("generation_mode") not in AUTO_MODES:
        return []
    return ["generation", *(["sending"] if state.get("auto_send") else []),
            *(["collection"] if state.get("generation_mode") == COLLECT_MODE else [])]


def modes_in_force(state: dict) -> list[str]:
    """The chosen modes that run now: only while the agreement can bill (billable)."""
    return configured_modes(state) if billable(state) else []


def entering_modes(before: dict, after: dict) -> list[str]:
    """The modes a change brings into force: in force after it and not before, whatever moved
    them (the status, the start of the schedule, a term)."""
    running = modes_in_force(before)
    return [m for m in modes_in_force(after) if m not in running]


def failure_reason(exc: Exception) -> dict:
    """Why an unattended bill failed, stored with its billing_failure: ``reason``, the refusal
    Generate due gives for the same state as its catalog key and params, so each reader reads it
    in their language; nothing for any other failure (one with no key reads as failed, and
    Generate due gives its refusal as it is)."""
    from celerp.error_body import as_refusal, keyed
    detail = as_refusal(exc.detail) if isinstance(exc, HTTPException) else None
    return {"reason": detail} if keyed(detail) else {}


def still_failing(state: dict, found: tuple[Schedule, list[int]] | None) -> bool:
    """Whether the agreement's billing failure still holds: its period is the one the agreement
    would bill on its own next (``found``, unattended_due as stored). One that no longer holds
    (paused, billing by hand, rescheduled, or void through Documents) stays stored until
    run_due's next pass drops it."""
    return found is not None and found[1][0] == failure_of(state).get("period_index")


async def _drop_failure(session: AsyncSession, company_id, entity_id: str, today: date) -> None:
    """Drop a billing failure that no longer holds (still_failing, decided again as locked):
    the agreement was paused, set to bill by hand, rescheduled or voided through Documents since
    the pass that failed."""
    try:
        async with savepoint(session):
            proj = await locked_agreement(session, company_id, entity_id)
            if proj.state.get("billing_failure") and not still_failing(proj.state, unattended_due(proj.state, today)):
                await write_agreement(session, company_id, entity_id, {}, None, drop=("billing_failure",))
    except Exception:
        log.exception("Dropping the billing failure of %s failed", entity_id)


async def run_due(*, session: AsyncSession, company_id, today: date, budget: int) -> OperationReport:
    """The dispatcher's billing pass for one company (the ``periodic_operation`` slot). It
    stores an agreement's billing failure when its unattended bill is refused, one current fact,
    and drops one that no longer holds (still_failing) before billing it; a bill or skip of the
    failed period, or the agreement ending, drops it sooner, in that write (ends_failure)."""
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
        if state.get("billing_failure") and not still_failing(state, found):
            await _drop_failure(session, company_id, row.entity_id, today)
        if found is None:
            continue
        if outside_window(state, *found, today):
            held.append(len(found[1]))
            continue
        while report.done < budget:
            failed_period = None
            try:
                async with savepoint(session):
                    # Decided again from the agreement as locked: the read above may be stale.
                    company = await locked_company(session, company_id)
                    if (company is None or not company.is_active
                            or not uses_module(company.settings, MODULE)):
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
            except Exception as exc:
                log.exception("Unattended billing of %s failed", row.entity_id)
                if failed_period is not None:
                    try:
                        proj = await locked_agreement(session, company_id, row.entity_id)
                        prior = failure_of(proj.state)
                        why = failure_reason(exc)
                        if (prior.get("period_index"), prior.get("reason")) != (failed_period, why.get("reason")):
                            await write_agreement(session, company_id, row.entity_id, {
                                "billing_failure": {"period_index": failed_period, "at": now_stamp(), **why},
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
    from celerp_subscriptions.collect import pass_for
    report.after_commit.append(pass_for(company_id))
    return report


def _expire_attempts_for(company_id):
    async def _run(factory) -> None:
        async with factory() as session:
            await locked_company(session, company_id)
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


async def deliver(factory, company_id, template_id: str, k: int, *, retry_by=None, attempt: int | None = None,
                  again: bool = False) -> dict:
    """Send a period's document using one immutable message per transport attempt. A send
    that was cut off is replayed as the same attempt, or with ``again`` (the user confirmed
    it may already have arrived) sent as a new one. A person's send (``retry_by``) names the
    ``attempt`` they saw; once the stored attempt moved on, it is answered as stored and sends
    nothing. A document already emailed (emailed) settles the period's email instead of
    sending it twice."""
    from celerp.modules.registry import uses_module
    from celerp_docs.doc_constants import NO_SEND_DOC_TYPES
    from celerp_docs.routes import DocSendBody, doc_email, mark_doc_sent

    relay_idempotent = email.relay_send_is_idempotent()
    async with factory() as session:
        proj = await locked_agreement(session, company_id, template_id)
        company = await session.get(Company, company_id)
        if retry_by is None:
            if (company is None or not company.is_active
                    or not uses_module(company.settings, MODULE)
                    or not sends_itself(proj.state)
                    or await authorizer(session, company_id, company.settings or {}, proj.state, "sending") is None):
                return {"state": "pending"}
        periods = periods_of(proj.state)
        entry = periods.get(str(k)) or {}
        send = dict(entry.get("send") or {})
        if entry.get("state") != BILLED:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_not_billed"))
        if retry_by is not None and int(send.get("n") or 0) != attempt:
            return send
        reads, _since = send_state(send, now())
        if retry_by is not None and reads == "sending":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_in_progress"))
        if reads not in (("not_sent", "failed", "unconfirmed") if retry_by else ("not_sent",)):
            return send

        uncertain = reads == "unconfirmed" and not again
        if uncertain and not (relay_idempotent and send.get("deduplicated")):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_send_unverified"))
        doc_id = entry["doc_id"]
        doc = await session.get(Projection, {"company_id": company_id, "entity_id": doc_id})
        via = emailed(doc.state if doc is not None else None)
        if via and not again:
            send = record_send(periods, k, retry_by, state="sent", n=int(send.get("n") or 0), via=via)
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
            digest = hashlib.sha256(_payload_json(message).encode()).hexdigest()
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
                send = record_send(periods, k, retry_by, state="failed", n=n,
                                   error_key="subscriptions.err_send_no_email" if doc is not None and not to
                                   else "subscriptions.err_send_void")
                await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
                await session.commit()
                return send
            try:
                async with savepoint(session):
                    message = await doc_email(session, company_id, doc_id, doc.state, to, actor)
                    html, body = email._with_footer(message["body_html"], message["body_text"])
                    message = {**message, "body_html": html, "body_text": body, "branded": False}
                    payload_json = _payload_json(message)
                    if len(payload_json.encode()) > MAX_EMAIL_PAYLOAD_BYTES:
                        raise ValueError("email too large")
            except Exception as exc:
                log.warning("Preparing subscription email failed (%s)", type(exc).__name__)
                send = record_send(periods, k, retry_by, state="failed", n=n,
                                   error_key="subscriptions.err_send_preparation")
                await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
                await session.commit()
                return send

            operation_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"subscription-email:{company_id}:{doc_id}:{n}"))
            digest = hashlib.sha256(payload_json.encode()).hexdigest()
            record_send(periods, k, retry_by, state="sending", n=n, operation_id=operation_id,
                        deduplicated=relay_idempotent, payload_digest=digest)
            await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
            session.add(SubscriptionEmailAttempt(
                company_id=company_id, operation_id=uuid.UUID(operation_id), doc_id=doc_id,
                attempt_number=n, payload_json=payload_json))
            await session.commit()

    try:
        result = await asyncio.wait_for(email.send_email(**message, operation_id=operation_id,
                                                         fail_closed_on_uncertain=True),
                                        TRANSPORT_TIMEOUT.total_seconds())
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
                or sending.get("n") != n
                or send_state(sending, now())[0] not in ("sending", "unconfirmed")
                or sending.get("operation_id") != operation_id):
            await session.execute(delete(SubscriptionEmailAttempt).where(
                SubscriptionEmailAttempt.company_id == company_id,
                SubscriptionEmailAttempt.operation_id == operation_uuid))
            await session.commit()
            return sending

        outcome = "sent" if result.ok else ("unconfirmed" if uncertain or result.unconfirmed else "failed")
        send = record_send(periods, k, retry_by, state=outcome, n=n, operation_id=operation_id,
                           deduplicated=relay_idempotent, payload_digest=digest,
                           **({"error_key": result.reason} if outcome == "failed" else {}))
        await write_agreement(session, company_id, template_id, {"periods": periods}, actor.id)
        if result.ok:
            try:
                async with savepoint(session):
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
async def savepoint(session: AsyncSession):
    """A savepoint whose failure the caller catches and carries on after. Every row written
    inside it is held until it ends, so its rollback takes each of them out of the session: a
    row the session no longer held would come back on a later read as one the rollback cannot
    see, and stay in the session after its row is gone."""
    written = []

    def hold(_session, instance) -> None:
        written.append(instance)

    event.listen(session.sync_session, "pending_to_persistent", hold)
    try:
        async with session.begin_nested():
            yield
    finally:
        event.remove(session.sync_session, "pending_to_persistent", hold)


@asynccontextmanager
async def same_session(session: AsyncSession):
    """A session factory that hands out the caller's own session."""
    yield session
