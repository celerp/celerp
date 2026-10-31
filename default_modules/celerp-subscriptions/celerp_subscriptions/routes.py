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

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.business_time import company_today
from celerp.services.company_lock import locked_company
from celerp.services.permissions import missing_permission, require_permission, role_has_permission
from celerp_subscriptions import billing, summary
from celerp_subscriptions.periods import BILLED, CLAIMED_STATES, FREQUENCIES, RELEASED, SKIPPED, TIMINGS, Schedule, UNRESOLVED
from celerp_subscriptions.search import SUBSCRIPTION_DOC_TYPES, search_subscription_templates
from ui.i18n import t

BATCH_CAP = 50
GENERATION_MODES = frozenset({"manual_due"}) | billing.AUTO_MODES
# Schedule terms that place the periods: fixed once any period is claimed.
PLACING_FIELDS = ("anchor_date", "frequency", "custom_interval_days", "billing_timing")


def next_bill_date(state: dict) -> str | None:
    """The derived next bill date of an active or paused agreement."""
    if state.get("status") not in ("active", "paused") or not state.get("anchor_date"):
        return None
    nxt = Schedule.from_state(state).next_bill_date(floor=billing.review_floor(state))
    return nxt.isoformat() if nxt else None


class RequestBody(BaseModel):
    request_key: str | None = Field(None, min_length=1, max_length=200)


class EarlyBody(BaseModel):
    request_key: str = Field(..., min_length=1, max_length=200)
    period_index: int | None = None


class BulkBody(BaseModel):
    ids: list[str] = Field(..., max_length=BATCH_CAP)


class SkipBody(BaseModel):
    reason: str = Field(..., max_length=500)

    @field_validator("reason")
    @classmethod
    def _stated(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A reason is required")
        return v.strip()


class ConfirmBody(BaseModel):
    period_index: int


class CatchUpItem(BaseModel):
    entity_id: str
    period_index: int


class CatchUpBody(BaseModel):
    items: list[CatchUpItem]


class ScheduleBody(BaseModel):
    anchor_date: date | None = None
    frequency: str | None = None
    custom_interval_days: int | None = Field(None, gt=0)
    billing_timing: str | None = None
    term_start: date | None = None
    term_end: date | None = None
    cancel_at_period_end: bool | None = None
    catchup_periods: int | None = Field(None, ge=0, le=billing.CATCHUP_CAP)
    generation_mode: str | None = None
    auto_send: bool | None = None

    @field_validator("frequency")
    @classmethod
    def _frequency(cls, v):
        if v is not None and v not in FREQUENCIES:
            raise ValueError(f"Unknown frequency {v!r}")
        return v

    @field_validator("billing_timing")
    @classmethod
    def _timing(cls, v):
        if v is not None and v not in TIMINGS:
            raise ValueError(f"Unknown billing timing {v!r}")
        return v

    @field_validator("generation_mode")
    @classmethod
    def _mode(cls, v):
        if v is not None and v not in GENERATION_MODES:
            raise ValueError(f"Unknown generation mode {v!r}")
        return v

    @field_validator("anchor_date", "generation_mode", "frequency", "billing_timing", "catchup_periods", mode="before")
    @classmethod
    def _not_null(cls, v):
        if v is None:
            raise ValueError("A value is required")
        return v


def _periods(state: dict) -> dict[str, dict]:
    return billing.periods_of(state)


async def _refuse_claimed(session, company_id, entry: dict) -> None:
    """409 naming the document when the period is billed."""
    if entry.get("state") == BILLED:
        ref = await billing.doc_ref(session, company_id, entry["doc_id"])
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


def _build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("")
    async def list_subscription_templates(
        direction: str | None = None,
        status: str | None = None,
        q: str | None = None,
        review: bool = False,
        limit: int = 50,
        offset: int = 0,
        company_id: uuid.UUID = Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """List agreements, each with its derived next bill date; ``review`` keeps those
        with earlier documents waiting for history review."""
        items, total = await search_subscription_templates(
            session, company_id, direction=direction, status=status, q=q, review=review, limit=limit, offset=offset
        )
        return {"items": [{**i, "next_bill_date": next_bill_date(i)} for i in items], "total": total}

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
        refusal never stops the rest."""
        results = []
        for eid in body.ids:
            try:
                async with session.begin_nested():
                    found = await billing.claim_period(session, company_id, eid, actor=user)
                results.append({"entity_id": eid, "ok": True, **found})
            except HTTPException as exc:
                detail = "Not found" if exc.status_code == 404 else str(exc.detail)
                results.append({"entity_id": eid, "ok": False, "error": detail})
        await session.commit()
        billed = sum(1 for r in results if r["ok"])
        return {"results": results, "billed": billed, "refused": len(results) - billed}

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
        only when the preview lists it and it is its agreement's oldest open period."""
        today = await company_today(session, company_id)
        listed = {(i["entity_id"], p["period_index"])
                  for i in await billing.catch_up_preview(session, company_id, today) for p in i["periods"]}
        results = []
        for n, item in enumerate(body.items):
            row = {"entity_id": item.entity_id, "period_index": item.period_index}
            if n >= BATCH_CAP or (item.entity_id, item.period_index) not in listed:
                results.append({**row, "ok": False, "error": t("subscriptions.err_catchup_not_listed")})
                continue
            try:
                async with session.begin_nested():
                    proj = await billing.locked_agreement(session, company_id, item.entity_id)
                    oldest = Schedule.from_state(proj.state).oldest_open(floor=billing.review_floor(proj.state))
                    if oldest != item.period_index:
                        raise HTTPException(status_code=409, detail=t("subscriptions.err_early_not_next"))
                    found = await billing.claim_period(session, company_id, item.entity_id, actor=user)
                results.append({**row, "ok": True, **found})
            except HTTPException as exc:
                results.append({**row, "ok": False, "error": str(exc.detail)})
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
        """What unattended billing did since this user's acknowledgment (``all``: everything)."""
        return await summary.report(session, company_id, user.id, everything=all)

    @router.post("/summary/ack")
    async def ack_summary(
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Mark the summary read up to now (the server's clock, never the client's)."""
        at = await summary.acknowledge(session, company_id, user.id)
        await session.commit()
        return {"ok": True, "at": at}

    @router.post("/{entity_id}/generate-due")
    async def generate_due(
        entity_id: str,
        body: RequestBody | None = None,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("finalize_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Bill the agreement's oldest due period."""
        result = await billing.claim_period(session, company_id, entity_id, actor=user,
                                            request_key=body.request_key if body else None)
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
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("view_subscriptions"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """The agreement's periods, newest first, and its next bill date."""
        proj = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
        if proj is None or proj.state.get("doc_type") not in SUBSCRIPTION_DOC_TYPES:
            raise HTTPException(status_code=404, detail=t("subscriptions.err_not_found"))
        return await billing.period_list(session, company_id, proj.state, await company_today(session, company_id))

    @router.post("/{entity_id}/periods/{k}/skip")
    async def skip_period(
        entity_id: str,
        k: int,
        body: SkipBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Record that period ``k`` is not billed, and why."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        periods = _periods(proj.state)
        entry = periods.get(str(k)) or {}
        await _refuse_claimed(session, company_id, entry)
        if entry.get("state") == SKIPPED:
            return {"ok": True}
        if entry.get("state") == UNRESOLVED:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_in_review"))
        if not Schedule.from_state(proj.state).exists(k):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_missing"))
        periods[str(k)] = {"state": SKIPPED, "reason": body.reason, "by": str(user.id), "at": billing.now_stamp(),
                           "release_count": entry.get("release_count", 0)}
        await _write(session, company_id, entity_id, {"periods": periods}, user)
        return {"ok": True}

    async def _hand_back(entity_id: str, k: int, company_id, user, session, *, after_void: bool) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id)
        periods = _periods(proj.state)
        entry = periods.get(str(k)) or {}
        release = entry.get("release_count", 0)
        if entry.get("state") == BILLED:
            doc = await billing.doc_state(session, company_id, entry["doc_id"])
            if not after_void or (doc is not None and doc.get("status") != "void"):
                await _refuse_claimed(session, company_id, entry)
            release += 1  # the next document for this period takes a new claim key
        elif entry.get("state") != SKIPPED:
            return {"ok": True}
        periods[str(k)] = {"state": RELEASED, "release_count": release, "by": str(user.id), "at": billing.now_stamp()}
        await _write(session, company_id, entity_id, {"periods": periods}, user)
        return {"ok": True}

    @router.post("/{entity_id}/periods/{k}/unskip")
    async def unskip_period(
        entity_id: str,
        k: int,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
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
        """Open a billed period again once its document is voided, so it can be billed once more."""
        return await _hand_back(entity_id, k, company_id, user, session, after_void=True)

    @router.post("/{entity_id}/periods/{k}/send")
    async def send_period(
        entity_id: str,
        k: int,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Send period ``k``'s document again after a failed send; once."""
        return await billing.deliver(lambda: billing.same_session(session), company_id, entity_id, k, retry=True)

    @router.patch("/{entity_id}/schedule")
    async def change_schedule(
        entity_id: str,
        body: ScheduleBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        role: str = Depends(get_current_role),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Change the agreement's schedule terms and unattended modes. Turning a mode on
        needs its permission and records the consent of the person who did."""
        sent = body.model_dump(include=body.model_fields_set)
        company = await locked_company(session, company_id)
        settings = company.settings or {}
        proj = await billing.locked_agreement(session, company_id, entity_id)
        state = proj.state
        needed = []
        if sent.get("generation_mode") in billing.AUTO_MODES:
            needed.append("generation")
        if sent.get("generation_mode") == "auto_invoice_collect":
            needed.append("collection")
        if sent.get("auto_send"):
            needed.append("sending")
        for mode in needed:
            if not role_has_permission(settings, role, billing.MODE_PERMISSION[mode]):
                raise missing_permission(billing.MODE_PERMISSION[mode])
        if "collection" in needed:
            # Collection runs through the Connect service; until it offers it, no agreement collects.
            raise HTTPException(status_code=422, detail=t("subscriptions.err_collect_unavailable"))
        changes = {f: (v.isoformat() if isinstance(v, date) else v) for f, v in sent.items()}
        claimed = [int(k) for k, e in (state.get("periods") or {}).items() if e.get("state") in CLAIMED_STATES]
        if claimed and any(f in changes and changes[f] != state.get(f) for f in PLACING_FIELDS):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_schedule_claimed"))
        if "cancel_at_period_end" in changes:
            # Stored as the last period kept, so the end is fixed when it is chosen and undone by clearing it.
            changes["cancel_at_period_end"] = (
                Schedule.from_state(state).current(await company_today(session, company_id))
                if changes["cancel_at_period_end"] else None)
        merged = {**state, **changes}
        try:
            schedule = Schedule.from_state(merged) if merged.get("anchor_date") else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if merged.get("term_start") and merged.get("term_end") and merged["term_start"] >= merged["term_end"]:
            raise HTTPException(status_code=422, detail=t("subscriptions.err_term_order"))
        if schedule is not None and any(not schedule.exists(k) for k in claimed):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_term_cuts_claimed"))
        if needed:
            stamp = {"by": str(user.id), "at": billing.now_stamp()}
            changes["consents"] = {**(state.get("consents") or {}), **{m: stamp for m in needed}}
        await _write(session, company_id, entity_id, changes, user)
        return {"ok": True, "next_bill_date": next_bill_date(merged)}

    @router.post("/{entity_id}/history/{doc_id}/confirm")
    async def confirm_history(
        entity_id: str,
        doc_id: str,
        body: ConfirmBody,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
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
        review = list(proj.state.get("history_review") or [])
        row = next((r for r in review if r.get("doc_id") == doc_id), None)
        if row is None or body.period_index not in (row.get("candidates") or []):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_candidate"))
        if entry.get("state") in CLAIMED_STATES:
            await _refuse_claimed(session, company_id, entry)
            raise HTTPException(status_code=409, detail=t("subscriptions.err_period_skipped"))
        periods[k] = {"state": BILLED, "doc_id": doc_id, "release_count": entry.get("release_count", 0),
                      "candidates": row.get("candidates") or [], "history": True,
                      "by": str(user.id), "at": billing.now_stamp()}
        review = [r for r in review if r is not row]
        _settle(periods, review)
        await _write(session, company_id, entity_id, {"periods": periods, **_review_change(review)}, user,
                     drop=() if review else ("history_review",))
        return {"ok": True}

    @router.post("/{entity_id}/history/{doc_id}/withdraw")
    async def withdraw_history(
        entity_id: str,
        doc_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """Undo a confirmation while no later period is billed: the document waits for review again."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        periods = _periods(proj.state)
        k = next((int(i) for i, e in periods.items() if e.get("history") and e.get("doc_id") == doc_id
                  and e.get("state") == BILLED), None)
        if k is None:
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_not_confirmed"))
        if any(int(i) > k and e.get("state") == BILLED and not e.get("history") for i, e in periods.items()):
            raise HTTPException(status_code=409, detail=t("subscriptions.err_history_later_billed"))
        candidates = periods[str(k)].get("candidates") or [k]
        for c in candidates:
            entry = periods.get(str(c)) or {}
            if c == k or entry.get("state") in (None, RELEASED):
                periods[str(c)] = {"state": UNRESOLVED, "release_count": entry.get("release_count", 0)}
        review = [*(proj.state.get("history_review") or []), {"doc_id": doc_id, "candidates": candidates}]
        await _write(session, company_id, entity_id, {"periods": periods, "history_review": review}, user)
        return {"ok": True}

    @router.post("/{entity_id}/pause")
    async def pause_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id)
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
        """Resume billing. Periods missed while paused stay due: none is skipped."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        if proj.state.get("status") != "paused":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_resume_not_paused"))
        await _write(session, company_id, entity_id, {"status": "active"}, user)
        return {"ok": True, "next_bill_date": next_bill_date({**proj.state, "status": "active"})}

    @router.post("/{entity_id}/cancel")
    async def cancel_subscription(
        entity_id: str,
        company_id: uuid.UUID = Depends(get_current_company_id),
        _: None = require_permission("edit_documents"),
        user=Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        proj = await billing.locked_agreement(session, company_id, entity_id)
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
        which is also its first bill date when billed in advance."""
        proj = await billing.locked_agreement(session, company_id, entity_id)
        if proj.state.get("status") != "draft":
            raise HTTPException(status_code=409, detail=t("subscriptions.err_activate_not_draft"))
        start = proj.state.get("start_date") or (await company_today(session, company_id)).isoformat()
        changes = {"status": "active", "start_date": start,
                   "anchor_date": proj.state.get("anchor_date") or start}
        merged = {**proj.state, **changes}
        try:
            Schedule.from_state(merged)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=t("subscriptions.err_frequency_required")) from exc
        await _write(session, company_id, entity_id, changes, user)
        return {"ok": True, "next_bill_date": next_bill_date(merged)}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/subscriptions", tags=["subscriptions"])
