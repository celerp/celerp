# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-subscriptions API routes.

Agreements are stored as docs with doc_type "subscription_invoice" or "subscription_po".
Each period is billed once (``billing.claim_period``); these routes list agreements,
bill what is due, skip or reopen a period, change the schedule, resolve history from
the previous release, and report what unattended billing did.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.business_time import company_today
from celerp.services.company_lock import locked_company
from celerp.services.permissions import require_permission
from celerp_docs.doc_constants import NO_SEND_DOC_TYPES
from celerp_subscriptions import billing, migration, summary
from celerp_subscriptions.periods import (
    AUTO_MODES, BILLED, CLAIMED_STATES, RELEASED, SKIPPED, Schedule, ScheduleError, UNRESOLVED, shown,
)
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES, search_subscription_templates
from ui.i18n import t

BATCH_CAP = 50
# Schedule terms that place the periods: fixed once any period is claimed.
PLACING_FIELDS = ("anchor_date", "frequency", "custom_interval_days", "billing_timing")
# What a template not yet on a schedule may change: the terms that let it move onto one.
_MOVING_FIELDS = {"frequency", "custom_interval_days"}


def next_bill_date(state: dict) -> str | None:
    """The derived next bill date of an active or paused agreement on a schedule."""
    if state.get("status") not in ("active", "paused") or not billing.on_schedule(state):
        return None
    nxt = Schedule.from_state(state).next_bill_date(floor=billing.review_floor(state))
    return nxt.isoformat() if nxt else None


def list_row(state: dict) -> dict:
    """An agreement as the list shows it: its derived next bill date, and why its stored terms
    build no schedule when they build none (that row only; the rest of the list reads)."""
    exc = billing.unscheduled(state)
    return {**state, "next_bill_date": next_bill_date(state),
            "schedule_error": t(exc.key, **exc.params) if exc is not None else None}


async def refused_period(session, company_id, entity_id: str, k: int | None) -> dict:
    """The dates of the period a refused item names, so each refused row says which period it
    was; nothing when the agreement or its schedule cannot tell."""
    state = await billing.doc_state(session, company_id, entity_id) or {}
    if k is None or state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES or not billing.on_schedule(state):
        return {}
    schedule = Schedule.from_state(state)
    return {"service_start": schedule.start(k).isoformat(), "service_end": schedule.service_end(k).isoformat()}


class AckBody(BaseModel):
    keys: list[str]  # the ``key`` of each row the page marked read showed (GET /summary)


class RequestBody(BaseModel):
    request_key: str = Field(..., min_length=1, max_length=200)
    period_index: int | None = None


class EarlyBody(BaseModel):
    request_key: str = Field(..., min_length=1, max_length=200)
    period_index: int | None = None


class CatchUpItem(BaseModel):
    entity_id: str
    period_index: int
    request_key: str = Field(..., min_length=1, max_length=200)


class BulkBody(BaseModel):
    items: list[CatchUpItem] = Field(..., max_length=BATCH_CAP)


class SkipBody(BaseModel):
    reason: str = Field("", max_length=500)


class ConfirmBody(BaseModel):
    period_index: int


class SendBody(BaseModel):
    attempt: int = Field(..., ge=0)  # the stored attempt the user saw: the send's operation key
    again: bool = False


class CatchUpBody(BaseModel):
    items: list[CatchUpItem] = Field(..., max_length=1000)


class ScheduleBody(BaseModel):
    anchor_date: date | None = None
    frequency: str | None = None
    custom_interval_days: int | None = None
    billing_timing: str | None = None
    term_start: date | None = None
    term_end: date | None = None
    cancel_at_period_end: bool | None = None
    catchup_periods: int | None = None
    generation_mode: str | None = None
    auto_send: bool | None = None

    @field_validator("anchor_date", "generation_mode", "frequency", "billing_timing", "catchup_periods", mode="before")
    @classmethod
    def _not_null(cls, v):
        if v is None:
            raise ValueError("A value is required")
        return v


def _periods(state: dict) -> dict[str, dict]:
    return billing.periods_of(state)


def _period_answer(state: dict, k: int) -> dict:
    """What a period action answers: the period's dates, so the caller can name them."""
    return {"ok": True, "period": billing.period_row(Schedule.from_state(state), k)}


async def _later_bills(session, company_id, periods: dict[str, dict], earliest: int) -> str:
    """The refs of the bills (placements aside) of the periods after ``earliest``, oldest first:
    while any is there a placement from ``earliest`` on cannot be withdrawn."""
    return ", ".join([await billing.doc_ref(session, company_id, e["doc_id"])
                      for i, e in sorted(periods.items(), key=lambda p: int(p[0]))
                      if int(i) > earliest and e.get("state") == BILLED and not e.get("history")])


async def _refuse_claimed(session, company_id, periods: dict[str, dict], k) -> None:
    """409 when period ``k`` is billed, naming the door that frees it: Withdraw placement for a
    history placement (after voiding and reopening the later bills that hold it, named), Reopen
    for a bill that no longer bills it (billing_doc), else Void. Its callers answer
    billing.refuse_ended first, so on an ENDED agreement none of these is named."""
    entry = periods.get(str(k)) or {}
    if entry.get("state") == BILLED:
        ref = await billing.doc_ref(session, company_id, entry["doc_id"])
        if entry.get("history"):
            if later := await _later_bills(session, company_id, periods,
                                           min([*(entry.get("candidates") or []), int(k)])):
                raise HTTPException(status_code=409, detail=t(
                    "subscriptions.err_period_placed_later_billed", ref=ref, later=later))
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_placed", ref=ref))
        await billing.billing_doc(session, company_id, entry["doc_id"])
        raise HTTPException(status_code=409, detail=t("subscriptions.err_period_billed", ref=ref))


def _settle(periods: dict[str, dict], review: list[dict]) -> None:
    """A period no document in review names any more is open again."""
    named = {str(k) for row in review for k in row.get("candidates") or []}
    for key, entry in periods.items():
        if entry.get("state") == UNRESOLVED and key not in named:
            periods[key] = {**entry, "state": RELEASED}


def _review_change(review: list[dict]) -> dict:
    return {"history_review": review} if review else {}


async def _write(session, company_id, entity_id, changes: dict, user, *, drop: tuple[str, ...] = ()) -> None:
    await billing.write_agreement(session, company_id, entity_id, changes, user.id, drop=drop)
    await session.commit()


async def _first_schedule(session, company_id, state: dict) -> dict:
    """The start date and schedule an agreement not yet on one starts with: its start date (or
    today) as its anchor, with its own settings; 422 saying why when they describe none."""
    start = state.get("start_date") or (await company_today(session, company_id)).isoformat()
    try:
        return {"start_date": start, **migration.legacy_schedule({**state, "start_date": start})}
    except ScheduleError as exc:
        raise billing.schedule_refusal(422, exc) from exc


async def _move_with(session, company_id, proj: Projection, sent: dict, user) -> dict:
    """A template not yet on a schedule takes only its frequency and interval, and moves onto
    a schedule with them at once; 409 naming it and why while it still cannot."""
    state, entity_id = proj.state, proj.entity_id  # read before a rolled-back move expires proj
    if set(sent) - _MOVING_FIELDS:
        raise billing.not_moved_refusal(state, entity_id, billing.not_moved(state))
    try:
        async with billing.savepoint(session):
            await billing.write_agreement(session, company_id, entity_id, sent, user.id)
            await migration.move_template(session, company_id, entity_id)
    except ScheduleError as exc:
        raise billing.not_moved_refusal(state, entity_id, t(exc.key, **exc.params)) from exc
    moved = await billing.locked_agreement(session, company_id, entity_id)
    await session.commit()
    return {"ok": True, "next_bill_date": next_bill_date(moved.state)}


def _build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("")
    async def list_subscription_templates(
        direction: str | None = None,
        status: str | None = None,
        q: str | None = None,
        review: bool = False,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """List agreements, each with its derived next bill date (list_row); ``review`` keeps
        those with earlier documents waiting for history review."""
        items, total = await search_subscription_templates(
            session, company_id, direction=direction, status=status, q=q, review=review, limit=limit, offset=offset
        )
        return {"items": [list_row(i) for i in items], "total": total}

    @router.get("/due")
    async def list_due(
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Every active agreement with a period due today: what Generate due would bill."""
        return await billing.due_preview(session, company_id, await company_today(session, company_id))

    @router.post("/generate-due")
    async def generate_all_due(
        body: BulkBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Bill the oldest due period of each listed agreement, each on its own: one
        refusal never stops the rest. A period billed before this request counts as
        already billed, never as billed."""
        results = []
        for item in body.items:
            eid = item.entity_id
            try:
                async with billing.savepoint(session):
                    found = await billing.claim_period(
                        session, company_id, eid, actor=user, period_index=item.period_index,
                        request_key=item.request_key)
                results.append({"entity_id": eid, "period_index": item.period_index, "ok": True, **found})
            except HTTPException as exc:
                detail = t("subscriptions.err_not_found") if exc.status_code == 404 else exc.detail
                results.append({"entity_id": eid, "period_index": item.period_index, "ok": False, "error": detail})
        await session.commit()
        answered = [r for r in results if r["ok"]]
        already = sum(1 for r in answered if r["already_billed"])
        return {"results": results, "billed": len(answered) - already, "already_billed": already,
                "refused": len(results) - len(answered)}

    @router.get("/catch-up")
    async def catch_up(
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Unattended agreements further behind than their catch-up window."""
        today = await company_today(session, company_id)
        return {"items": await billing.catch_up_preview(session, company_id, today)}

    @router.post("/catch-up/run")
    async def catch_up_run(
        body: CatchUpBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Bill the chosen periods from the catch-up preview, in order. An item is billed
        only when the preview lists it and it is its agreement's oldest open period. A refused
        item says why, the agreement's own state first (paused, cancelled, no schedule) before
        that it left the preview, and names its period's dates when the schedule gives them."""
        today = await company_today(session, company_id)
        listed = {(i["entity_id"], p["period_index"])
                  for i in await billing.catch_up_preview(session, company_id, today) for p in i["periods"]}
        results = []
        for n, item in enumerate(body.items):
            row = {"entity_id": item.entity_id, "period_index": item.period_index}
            if n >= BATCH_CAP:
                results.append({**row, "ok": False, "error": t("subscriptions.err_catchup_not_listed"),
                                **await refused_period(session, company_id, item.entity_id, item.period_index)})
                continue
            try:
                # A response lost after commit must replay its receipt, even after
                # that period has left the catch-up preview.
                prior = await billing.replay_result(session, company_id, item.entity_id,
                                                    item.request_key, period_index=item.period_index)
                if prior is not None:
                    results.append({**row, "ok": True, **prior})
                    continue
                async with billing.savepoint(session):
                    proj = await billing.locked_agreement(session, company_id, item.entity_id)
                    billing.refuse_unbillable(proj.state)
                    if (item.entity_id, item.period_index) not in listed:
                        raise HTTPException(status_code=409, detail=t("subscriptions.err_catchup_not_listed"))
                    found = await billing.claim_period(
                        session, company_id, item.entity_id, actor=user, period_index=item.period_index,
                        request_key=item.request_key)
                results.append({**row, "ok": True, **found})
            except HTTPException as exc:
                results.append({**row, "ok": False, "error": exc.detail,
                                **await refused_period(session, company_id, item.entity_id, item.period_index)})
        await session.commit()
        return {"results": results}

    @router.get("/summary")
    async def get_summary(
        all: bool = False,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """What unattended billing did in the last SUMMARY_WINDOW, each row with the ``key`` Mark as
        read posts for it: every row but those this user marked read (``all``: every row)."""
        return await summary.report(session, company_id, user.id, everything=all)

    @router.post("/summary/ack")
    async def ack_summary(
        body: AckBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Mark read the rows the page showed (each row's ``key`` from GET /summary); a key of
        no row there now is ignored."""
        read = await summary.acknowledge(session, company_id, user.id, body.keys)
        await session.commit()
        return {"ok": True, "read": len(read)}

    @router.post("/{entity_id}/generate-due")
    async def generate_due(
        entity_id: str,
        body: RequestBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Bill the agreement's oldest due period."""
        result = await billing.claim_period(session, company_id, entity_id, actor=user,
                                            request_key=body.request_key, period_index=body.period_index)
        await session.commit()
        return result

    @router.post("/{entity_id}/bill-early")
    async def bill_early(
        entity_id: str,
        body: EarlyBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Bill the next period ahead of its bill date; only the oldest open one."""
        result = await billing.claim_period(session, company_id, entity_id, actor=user, early=True,
                                            period_index=body.period_index, request_key=body.request_key)
        await session.commit()
        return result

    @router.get("/{entity_id}/periods")
    async def list_periods(
        entity_id: str,
        before: int | None = Query(None, description="Return periods before this period index."),
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """The agreement's periods, newest first, and its next bill date."""
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if proj is None or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
        return await billing.period_list(session, company_id, proj.state, await company_today(session, company_id),
                                         before=before)

    @router.post("/{entity_id}/periods/{k}/skip")
    async def skip_period(
        entity_id: str,
        k: int,
        body: SkipBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Record that period ``k`` is not billed, and why."""
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=422, detail=t("subscriptions.err_skip_reason"))
        proj = await billing.locked_agreement(session, company_id, entity_id)
        billing.refuse_ended(proj.state, entity_id)
        periods = _periods(proj.state)
        entry = periods.get(str(k)) or {}
        await _refuse_claimed(session, company_id, periods, k)
        if entry.get("state") == SKIPPED:
            return _period_answer(proj.state, k)
        if entry.get("state") == UNRESOLVED:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_in_review"))
        if not Schedule.from_state(proj.state).exists(k):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_missing"))
        periods[str(k)] = {"state": SKIPPED, "reason": reason, "by": str(user.id), "at": billing.now_stamp(),
                           "release_count": entry.get("release_count", 0)}
        await _write(session, company_id, entity_id, {"periods": periods}, user)
        return _period_answer(proj.state, k)

    async def _hand_back(entity_id: str, k: int, company_id, user, session, *, after_void: bool) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id)
        billing.refuse_ended(proj.state, entity_id)
        periods = _periods(proj.state)
        entry = periods.get(str(k)) or {}
        release = entry.get("release_count", 0)
        written = {}
        if entry.get("state") == BILLED:
            doc = await billing.doc_state(session, company_id, entry["doc_id"])
            if not after_void or billing.bills(doc):
                await _refuse_claimed(session, company_id, periods, k)
            release += 1  # the next document for this period takes a new claim key
            # The document let go never bills again: billing.guard_bill_status reads this.
            written["released_bills"] = {**(proj.state.get("released_bills") or {}), entry["doc_id"]: k}
        elif entry.get("state") != SKIPPED:
            return _period_answer(proj.state, k)
        # Reopened: the period's document was let go; an un-skipped period is only open again.
        periods[str(k)] = {"state": RELEASED, "release_count": release, "by": str(user.id), "at": billing.now_stamp(),
                           **({"reopened": True} if written else {})}
        await billing.write_agreement(session, company_id, entity_id, {"periods": periods, **written}, user.id)
        if entry.get("state") == BILLED:
            await billing.discard_email_attempts(session, company_id, entry["doc_id"])
        await session.commit()
        return _period_answer(proj.state, k)

    @router.post("/{entity_id}/periods/{k}/unskip")
    async def unskip_period(
        entity_id: str,
        k: int,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Undo a skip: the period is open again."""
        return await _hand_back(entity_id, k, company_id, user, session, after_void=False)

    @router.post("/{entity_id}/periods/{k}/reopen")
    async def reopen_period(
        entity_id: str,
        k: int,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Open a billed period again once its document no longer bills it (void, or back in
        draft), so it can be billed once more; that document never bills again."""
        return await _hand_back(entity_id, k, company_id, user, session, after_void=True)

    @router.post("/{entity_id}/periods/{k}/send")
    async def send_period(
        entity_id: str,
        k: int,
        body: SendBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Send period ``k``'s document again after a failed or cut-off send (``again``: a new
        message for one that may already have arrived). ``attempt`` is the stored attempt the
        user saw, so the same click arriving twice sends once; answered with how its email reads now."""
        await billing.deliver(lambda: billing.same_session(session), company_id, entity_id, k,
                              retry_by=user.id, attempt=body.attempt, again=body.again)
        return await summary.send_answer(session, company_id, entity_id, k)

    @router.patch("/{entity_id}/schedule")
    async def change_schedule(
        entity_id: str,
        body: ScheduleBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Change the agreement's schedule terms and unattended modes. Bringing a mode into
        force, or changing the terms of one that runs, asks for its consent where every
        agreement write does (billing.write_agreement)."""
        sent = body.model_dump(include=body.model_fields_set)
        changes = {f: (v.isoformat() if isinstance(v, date) else v) for f, v in sent.items()}
        await locked_company(session, company_id)
        proj = await billing.locked_agreement(session, company_id, entity_id, scheduled=False)
        if migration.is_legacy(proj.state):
            return await _move_with(session, company_id, proj, sent, user)
        state = proj.state
        recorded = [int(k) for k in (state.get("periods") or {})]
        claimed = [int(k) for k, e in (state.get("periods") or {}).items() if e.get("state") in CLAIMED_STATES]
        if (recorded or state.get("history_review") or state.get("history_set_aside")) and any(
            f in changes and shown(f, changes[f]) != shown(f, state.get(f)) for f in PLACING_FIELDS
        ):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_schedule_claimed"))
        if sent.get("auto_send") and billing.doc_kind(state) in NO_SEND_DOC_TYPES:
            raise HTTPException(status_code=422, detail=t("subscriptions.err_send_purchase"))
        if changes.get("cancel_at_period_end"):
            # Stored as the last period kept: the current one, or a later one already billed or
            # skipped, so nothing recorded falls outside it. Fixed when first chosen (choosing it
            # again keeps it) and undone by clearing it. A draft has no current period to stop at.
            if state.get("status") == "draft":
                raise HTTPException(status_code=409, detail=t("subscriptions.err_stop_draft"))
            chosen = state.get("cancel_at_period_end")
            if chosen is None:
                today = await company_today(session, company_id)
                chosen = max([Schedule.from_state(state).current(today), *recorded])
            changes["cancel_at_period_end"] = chosen
        elif "cancel_at_period_end" in changes:
            changes["cancel_at_period_end"] = None
        # Sending runs only on what automatic billing makes: choosing it under billing by hand
        # is refused, and going back to billing by hand turns it off (the answer says so).
        terms = {**state, **changes}
        sending_off = bool(terms.get("auto_send")) and terms.get("generation_mode") not in AUTO_MODES
        if sending_off and "auto_send" in changes:
            raise HTTPException(status_code=422, detail=t("subscriptions.err_send_manual"))
        sending_off = sending_off and "generation_mode" in changes
        if sending_off:
            changes["auto_send"] = False
        if billing.configured_modes({**state, **changes}) and not state.get("anchor_date"):
            # Automatic terms are consented whole: an agreement without a schedule takes the one
            # Activate would give it in the same write, so Activate never changes a consent term.
            changes = {**(await _first_schedule(session, company_id, {**state, **changes})), **changes}
        merged = {**state, **changes}
        if (exc := billing.unscheduled(merged)) is not None:
            raise billing.schedule_refusal(422, exc) from exc
        schedule = Schedule.from_state(merged) if merged.get("anchor_date") else None
        if schedule is not None and any(not schedule.exists(k) for k in recorded):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_term_cuts_claimed"))
        if schedule is not None and claimed and state.get("anchor_date"):
            previous = Schedule.from_state(state)
            if any(schedule.service_end(k) != previous.service_end(k) for k in claimed):
                raise HTTPException(status_code=409, detail=t("subscriptions.err_term_cuts_claimed"))
        await _write(session, company_id, entity_id, changes, user)
        return {"ok": True, "next_bill_date": next_bill_date(merged), "sending_off": sending_off}

    @router.post("/{entity_id}/history/{doc_id}/confirm")
    async def confirm_history(
        entity_id: str,
        doc_id: str,
        body: ConfirmBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Place an earlier document in one of its candidate periods."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        periods = _periods(proj.state)
        k = str(body.period_index)
        entry = periods.get(k) or {}
        if entry.get("state") == BILLED and entry.get("doc_id") == doc_id:
            return {"ok": True}
        billing.refuse_ended(proj.state, entity_id, placing=True)
        review = list(proj.state.get("history_review") or [])
        row = next((r for r in review if r.get("doc_id") == doc_id), None)
        if row is None or body.period_index not in (row.get("candidates") or []):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))
        await billing.require_history_document(session, company_id, entity_id, proj.state, doc_id)
        if entry.get("state") in CLAIMED_STATES:
            await _refuse_claimed(session, company_id, periods, k)
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_skipped"))
        periods[k] = {"state": BILLED, "doc_id": doc_id, "release_count": entry.get("release_count", 0),
                      "candidates": row.get("candidates") or [], "history": True,
                      "by": str(user.id), "at": billing.now_stamp()}
        review = [r for r in review if r is not row]
        _settle(periods, review)
        await _write(session, company_id, entity_id, {"periods": periods, **_review_change(review)}, user,
                     drop=() if review else ("history_review",))
        return {"ok": True}

    @router.post("/{entity_id}/history/{doc_id}/set-aside")
    async def set_aside_history(
        entity_id: str,
        doc_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Record that an earlier document billed no period of its own (a duplicate, or one that
        no longer exists), so review can finish without it."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        aside = list(proj.state.get("history_set_aside") or [])
        if any(r.get("doc_id") == doc_id for r in aside):
            return {"ok": True}
        review = list(proj.state.get("history_review") or [])
        row = next((r for r in review if r.get("doc_id") == doc_id), None)
        if row is None:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))
        review = [r for r in review if r is not row]
        periods = _periods(proj.state)
        _settle(periods, review)
        aside.append({"doc_id": doc_id, "candidates": row.get("candidates") or [],
                      "by": str(user.id), "at": billing.now_stamp()})
        await _write(session, company_id, entity_id,
                     {"periods": periods, "history_set_aside": aside, **_review_change(review)}, user,
                     drop=() if review else ("history_review",))
        return {"ok": True}

    @router.post("/{entity_id}/history/{doc_id}/withdraw")
    async def withdraw_history(
        entity_id: str,
        doc_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Undo a placement or a set-aside while no later period is billed: the document waits
        for review again. On an ENDED agreement a placement is final like a bill, and a set-aside
        always goes back."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        periods = _periods(proj.state)
        aside = list(proj.state.get("history_set_aside") or [])
        set_aside = next((r for r in aside if r.get("doc_id") == doc_id), None)
        k = next((int(i) for i, e in periods.items() if e.get("history") and e.get("doc_id") == doc_id
                  and e.get("state") == BILLED), None)
        if set_aside is not None:
            candidates = set_aside.get("candidates") or []
        elif k is not None:
            candidates = periods[str(k)].get("candidates") or [k]
        else:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_confirmed"))
        if set_aside is None:
            billing.refuse_ended(proj.state, entity_id)
        earliest = min([*candidates, *([k] if k is not None else [])], default=None)
        # Nothing bills again on an ENDED agreement, so a set-aside always goes back there: Set
        # aside, the way its review finishes, can be undone.
        if earliest is not None and billing.ended_key(proj.state) is None and (
                later := await _later_bills(session, company_id, periods, earliest)):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_later_billed", later=later))
        for c in candidates:
            entry = periods.get(str(c)) or {}
            if c == k or entry.get("state") in (None, RELEASED):
                periods[str(c)] = {"state": UNRESOLVED, "release_count": entry.get("release_count", 0)}
        review = [*(proj.state.get("history_review") or []), {"doc_id": doc_id, "candidates": candidates}]
        aside = [r for r in aside if r is not set_aside]
        await _write(session, company_id, entity_id,
                     {"periods": periods, "history_review": review, **({"history_set_aside": aside} if aside else {})},
                     user, drop=("history_set_aside",) if set_aside is not None and not aside else ())
        return {"ok": True}

    @router.post("/{entity_id}/pause")
    async def pause_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id, scheduled=False)
        if proj.state.get("status") != "active":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_pause_not_active"))
        await _write(session, company_id, entity_id, {"status": "paused"}, user)
        return {"ok": True}

    @router.post("/{entity_id}/resume")
    async def resume_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Resume billing, the status only: any automatic terms keep the consent of whoever set
        them, and the answer says what now runs. Periods missed while paused stay due: none is skipped, unless the
        template was paused on an earlier release, which restarted it from the resume day. Then
        every open period that started on or before today is skipped with that reason (each
        can be un-skipped), once."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        state = proj.state
        if state.get("status") != "paused":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_resume_not_paused"))
        changes: dict = {"status": "active"}
        skipped = []
        if state.get("resume_restarts"):
            schedule = Schedule.from_state(state)
            today = await company_today(session, company_id)
            periods = _periods(state)
            k = schedule.oldest_open(floor=billing.review_floor(state))
            while k is not None and schedule.exists(k) and schedule.start(k) <= today:
                if schedule.is_open(k):
                    periods[str(k)] = {"state": SKIPPED, "reason": t("subscriptions.skip_reason_paused_before_upgrade"),
                                       "by": str(user.id), "at": billing.now_stamp(),
                                       "release_count": (periods.get(str(k)) or {}).get("release_count", 0)}
                    skipped.append(billing.period_row(schedule, k))
                k += 1
            changes["periods"] = periods
        await _write(session, company_id, entity_id, changes, user,
                     drop=("resume_restarts",) if "resume_restarts" in state else ())
        return {"ok": True, "next_bill_date": next_bill_date({**state, **changes}), "skipped": skipped,
                **await summary.modes_answer(session, company_id, entity_id)}

    @router.post("/{entity_id}/cancel")
    async def cancel_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id, scheduled=False)
        if proj.state.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_already_cancelled"))
        await _write(session, company_id, entity_id, {"status": "cancelled"}, user)
        return {"ok": True}

    @router.post("/{entity_id}/activate")
    async def activate_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Promote a draft agreement to active. Its first period starts on the start date,
        which is also its first bill date when billed in advance. The status changes only: any
        automatic terms keep the consent of whoever set them, and the answer says what now runs.
        Activate gives the draft its schedule, so it asks only that the agreement is not a
        template still waiting to move onto one; whoever presses it needs the permissions of any
        mode it brings into force (billing.write_agreement)."""
        proj = await billing.locked_agreement(session, company_id, entity_id, scheduled=False)
        billing.refuse_not_moved(proj.state, entity_id)
        if proj.state.get("status") != "draft":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_activate_not_draft"))
        changes = {"status": "active", **await _first_schedule(session, company_id, proj.state)}
        merged = {**proj.state, **changes}
        await _write(session, company_id, entity_id, changes, user)
        return {"ok": True, "next_bill_date": next_bill_date(merged),
                **await summary.modes_answer(session, company_id, entity_id)}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/subscriptions", tags=["subscriptions"])
