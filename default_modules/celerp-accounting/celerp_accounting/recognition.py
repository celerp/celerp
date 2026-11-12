# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Revenue recognition: the monthly runs that move each revenue schedule's deferred revenue
to its revenue account, posted by hand or by the dispatcher once authorized, and the
deferred revenue roll-forward.

A schedule (``rev_schedule``, made at finalize by celerp.services.recognition, which also
holds the run arithmetic and reads the run entries) holds segments of {start, end, amount}.
Only months that have ended are planned, a segment's last run is exactly what its other runs
leave (so a run posted before an amendment closed its segment never leaves a cent behind),
and nothing is planned for a schedule whose source document is void or whose source entry
no longer stands. When Change fund moves a posted run onto a new fund (``dependents``), the
run stands as the copy it is replaced by.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import catalog_refusal
from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.business_time import company_today
from celerp.services.company_lock import locked_company
from celerp.services.journal_accounts import funds_enabled
from celerp.services.money import to_decimal, to_stored_float
from celerp.services.peer_lines import LINE_KEYS, UNRESTRICTED, copy_posted_lines
from celerp.services.permissions import read_authority, require_permission, role_has_permission
from celerp.schemas.numbers import FiniteFloat
from celerp.services.recognition import (
    PREFIX, REVERSAL, SCHEDULE, Ledger, amended_segments, amendment, month_end, parse_run, posted_amount,
    record_amendment, run_ledger, segment_runs,
)

router = APIRouter(dependencies=[Depends(get_current_user)])

MODULE = "celerp-accounting"
CONSENT = "recognition"
LAPSED = "recognition_lapsed"
CATEGORY = "recognition"
PAGE = "/accounting/recognition"
# Title (stored in English, as every notice is) and message key of the lapse notice.
NOTICE_LAPSED = ("Automatic recognition stopped", "recognition.notice_lapsed")


# ── planning ─────────────────────────────────────────────────────────────────────────────

async def _states(session, company_id, entity_type: str, ids=None) -> dict[str, dict]:
    query = select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == entity_type)
    if ids is not None:
        ids = sorted({str(i) for i in ids if i})
        if not ids:
            return {}
        query = query.where(Projection.entity_id.in_(ids))
    return {entity_id: state or {} for entity_id, state in (await session.execute(query)).all()}


async def _schedules(session, company_id) -> dict[str, dict]:
    """Every schedule, its ``source_je`` the entry standing for its source entry now (the copy
    Change fund replaced it by, when it did), each with ``stopped`` set when its source
    document is void or that entry does not stand."""
    from celerp.services.auto_je import live_entries

    schedules = await _states(session, company_id, SCHEDULE)
    live = await live_entries(session, company_id, [str(s.get("source_je")) for s in schedules.values()])
    for schedule in schedules.values():
        schedule["source_je"] = live[str(schedule.get("source_je"))]
    docs = await _states(session, company_id, "doc", {s.get("doc_id") for s in schedules.values()})
    entries = await _states(session, company_id, "journal_entry", {s.get("source_je") for s in schedules.values()})
    for schedule in schedules.values():
        schedule["stopped"] = ((docs.get(str(schedule.get("doc_id"))) or {}).get("status") == "void"
                               or (entries.get(str(schedule.get("source_je"))) or {}).get("status") != "posted")
    return schedules


def _run(schedule_id: str, segment: int, month: str, amount, status: str, je_id: str | None) -> dict:
    return {"schedule_id": schedule_id, "segment": segment, "month": month, "amount": to_stored_float(amount),
            "status": status, "je_id": je_id}


def plan(schedules: dict[str, dict], ledger: Ledger, today: date) -> list[dict]:
    """Every run: each planned run of an ended month (posted or outstanding), and every run
    standing posted that is planned no longer (its schedule stopped or replanned), so the
    accountant can reverse it. Newest month first."""
    runs, seen = [], set()
    for sid, schedule in schedules.items():
        if schedule["stopped"]:
            continue
        for index, segment in enumerate(schedule.get("segments") or ()):
            own = []
            for month, _, amount in segment_runs(segment, schedule["currency"], today):
                key = (sid, index, month)
                seen.add(key)
                live = ledger.live(key)
                own.append(_run(sid, index, month, posted_amount(live, schedule["deferred_account"]) if live else amount,
                                "posted" if live else "outstanding", live["id"] if live else None))
            end = date.fromisoformat(segment["end"])
            if own and own[-1]["status"] == "outstanding" and month_end(end.year, end.month) < today:
                own[-1]["amount"] = to_stored_float(to_decimal(segment["amount"])
                                                    - sum((to_decimal(r["amount"]) for r in own[:-1]), Decimal(0)))
            runs += own
    for key in ledger.keys():
        if key not in seen and key[0] in schedules and (live := ledger.live(key)):
            runs.append(_run(*key, posted_amount(live, schedules[key[0]]["deferred_account"]), "posted", live["id"]))
    return sorted(runs, key=lambda r: (r["month"], r["schedule_id"], r["segment"]), reverse=True)


# ── posting ──────────────────────────────────────────────────────────────────────────────

def _check_month(month: str) -> date:
    try:
        year, number = (int(p) for p in month.split("-"))
        if len(month) != 7:
            raise ValueError(month)
        return month_end(year, number)
    except ValueError:
        raise HTTPException(status_code=422, detail=catalog_refusal("recognition.err_month", month=month)) from None


def _dated(settings: dict, month_end: date, chosen: str | None) -> tuple[str, bool]:
    """The day a run of the month ending ``month_end`` posts on: its month's end while that
    month is open; in a locked month only the first open day, sent as the recorded choice
    ``chosen`` (409 naming the lock and that day when nothing is sent, 422 for another day).
    Answers the day and whether it is the recorded choice of a locked month."""
    lock = settings.get("lock_date")
    lock_day = date.fromisoformat(lock) if lock else None
    if lock_day is None or lock_day < month_end:
        if chosen and chosen != month_end.isoformat():
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "recognition.err_post_on", first_open=month_end.isoformat()))
        return month_end.isoformat(), False
    first_open = (lock_day + timedelta(days=1)).isoformat()
    if not chosen:
        raise HTTPException(status_code=409, detail=catalog_refusal(
            "recognition.err_month_locked", lock_date=lock, first_open=first_open))
    if chosen != first_open:
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "recognition.err_post_on", first_open=first_open))
    return first_open, True


def _leg_keys(schedule: dict, doc: dict) -> dict:
    """The fund keys each leg of a run carries: the schedule's fund, and the origin and
    choice the deferred document line posted with."""
    line = next((li for li in doc.get("line_items") or () if isinstance(li, dict)
                 and li.get("line_id") == schedule.get("line_id")), {})
    keys = {key: line[key] for key in LINE_KEYS if line.get(key)}
    keys.pop("fund_id", None)
    return {**keys, **({"fund_id": schedule["fund_id"]} if schedule.get("fund_id") else {})}


async def run_recognition(session, company_id, user_id, month: str, *, post_on: str | None = None,
                    today: date | None = None, unattended: bool = False, limit: int | None = None) -> dict:
    """Post every outstanding run of ``month`` (an ended month), or the first ``limit`` of them
    by schedule and segment: Dr the deferred account, Cr the schedule's revenue account, on the
    schedule's fund. A run already posted is answered with its entry, so a repeat or a race
    posts nothing twice. ``today`` is the business day (the dispatcher's pass day), else the
    company's. Answers every run of the month and how many this call posted. The caller commits."""
    from celerp.services.auto_je import _emit_auto_posted_je

    month_end = _check_month(month)
    company = await locked_company(session, company_id)
    settings = company.settings or {}
    today = today or await company_today(session, company_id)
    schedules = await _schedules(session, company_id)
    ledger = await run_ledger(session, company_id)
    runs = [r for r in plan(schedules, ledger, today) if r["month"] == month and not schedules[r["schedule_id"]]["stopped"]]
    pending = sorted((r for r in runs if r["status"] == "outstanding"),
                     key=lambda r: (r["schedule_id"], r["segment"]))[:limit]
    if pending:
        day, chosen = _dated(settings, month_end, post_on)
        docs = await _states(session, company_id, "doc", {schedules[r["schedule_id"]]["doc_id"] for r in pending})
    for run in pending:
        schedule = schedules[run["schedule_id"]]
        key = (run["schedule_id"], run["segment"], month)
        n = ledger.next_n(key)
        run_key = f"{run['schedule_id']}:{run['segment']}:{month}:{n}"
        keys = _leg_keys(schedule, docs.get(str(schedule["doc_id"])) or {})
        await _emit_auto_posted_je(
            session, company_id=company_id, user_id=user_id, je_id=f"{PREFIX}{run_key}",
            record_id=schedule["doc_id"], idem_create=f"{run_key}:c", idem_posted=f"{run_key}:p",
            memo=f"Revenue recognition {month}", currency=schedule["currency"], ts=day,
            entries=[{"account": schedule["deferred_account"], "debit": run["amount"], "credit": 0, **keys},
                     {"account": schedule["account"], "debit": 0, "credit": run["amount"], **keys}],
            metadata_={"source": "recognition", "schedule_id": run["schedule_id"], "segment": run["segment"],
                       "month": month, "unattended": unattended,
                       **({"post_on_choice": day} if chosen else {})})
        run.update(status="posted", je_id=f"{PREFIX}{run_key}")
    return {"runs": runs, "posted": len(pending)}


async def dependents(session, company_id, user_id, doc_id: str, relabel) -> None:
    """Change fund on a document a schedule recognizes (the doc_posting_dependents slot): each
    schedule of ``doc_id`` takes the fund its line now posts on, and each of its runs standing
    posted is voided and posted again with the legs a run posts now (``_leg_keys``) at its own
    date (module_journals.move_journal_funds); a run posted and reversed is left as it is, and
    runs still outstanding plan on the new fund. ``relabel`` rewrites the document's own legs,
    so it is not read here."""
    from celerp.events.engine import emit_event
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import move_journal_funds
    from celerp.services.peer_lines import leg_funds

    found = sorted(sid for sid, s in (await _states(session, company_id, SCHEDULE)).items() if s.get("doc_id") == doc_id)
    if not found:
        return
    doc = (await _states(session, company_id, "doc", [doc_id])).get(doc_id) or {}
    funds = leg_funds(doc)[0]
    rows = await lock_projections(session, company_id, found)
    ledger = await run_ledger(session, company_id)
    for sid in found:
        schedule = dict(rows[sid].state or {})
        fund = funds.get(int(schedule["line"]))
        if fund != schedule.get("fund_id"):
            await emit_event(session, company_id=company_id, entity_id=sid, entity_type=SCHEDULE,
                             event_type="rev_schedule.fund_changed", data={"fund_id": fund}, actor_id=user_id,
                             location_id=None, source="fund_change",
                             idempotency_key=f"{sid}:fund_changed:{rows[sid].version}", metadata_={})
        keys = _leg_keys({**schedule, "fund_id": fund}, doc)
        for key in sorted(k for k in ledger.keys() if k[0] == sid):
            if (live := ledger.live(key)) is not None:
                await move_journal_funds(
                    session, company_id=company_id, user_id=user_id, module=MODULE, je_id=live["root"],
                    reason="Fund changed on the document this revenue is recognized from",
                    relabel=lambda lines, keys=keys: [{k: v for k, v in ln.items() if k not in LINE_KEYS} | keys
                                                      for ln in lines])


async def reverse_recognition(session, company_id, user_id, month: str, *, reverse_on: str | None = None) -> dict:
    """Reverse every run of ``month`` standing posted, so each is outstanding again: its lines
    posted again swapped (copy_posted_lines). Answers with each run's reversal; a repeat finds
    them reversed and answers the same. 409 when no run of the month was posted."""
    from celerp.services.auto_je import _emit_auto_posted_je

    month_end = _check_month(month)
    company = await locked_company(session, company_id)
    settings = company.settings or {}
    schedules = await _schedules(session, company_id)
    ledger = await run_ledger(session, company_id)
    latest = {key: ledger.latest(key) for key in ledger.keys() if key[2] == month and key[0] in schedules}
    latest = {key: found for key, found in latest.items() if found and found[1].get("status") == "posted"}
    if not latest:
        raise HTTPException(status_code=409, detail=catalog_refusal("recognition.err_run_not_posted", month=month))
    standing = [key for key, (_, _, reversal) in latest.items() if not (reversal and reversal.get("status") == "posted")]
    if standing:
        day, chosen = _dated(settings, month_end, reverse_on)
    runs = []
    for key in sorted(latest):
        n, entry, reversal = latest[key]
        reversal_id = f"{entry['root']}{REVERSAL}"
        if key in standing:
            run_key = f"{key[0]}:{key[1]}:{month}:{n}{REVERSAL}"
            await _emit_auto_posted_je(
                session, company_id=company_id, user_id=user_id, je_id=reversal_id,
                record_id=schedules[key[0]]["doc_id"], idem_create=f"{run_key}:c", idem_posted=f"{run_key}:p",
                memo=f"Revenue recognition {month} reversed", currency=schedules[key[0]]["currency"], ts=day,
                entries=copy_posted_lines(entry["id"], entry.get("entries") or [], reverse=True),
                metadata_={"source": "recognition", "schedule_id": key[0], "segment": key[1], "month": month,
                           "reverses": entry["id"], **({"reverse_on_choice": day} if chosen else {})})
        runs.append(_run(*key, posted_amount(entry, schedules[key[0]]["deferred_account"]), "outstanding", reversal_id))
    return {"runs": runs}


async def amend_schedule(session, company_id, user_id, schedule_id: str, payload: AmendIn) -> dict:
    """Amend a schedule from ``payload.effective`` (amended_segments); an amendment that
    leaves the segments as they are records nothing, so a repeat is harmless. The caller commits."""
    from celerp.services.company_lock import lock_projections

    row = (await lock_projections(session, company_id, [schedule_id])).get(schedule_id)
    if row is None or row.entity_type != SCHEDULE:
        raise HTTPException(status_code=404, detail=catalog_refusal("recognition.err_schedule_not_found"))
    schedule = dict(row.state or {})
    segments = amended_segments(schedule_id, schedule, await run_ledger(session, company_id),
                                effective=payload.effective, service_end=payload.service_end, amount=payload.amount)
    if segments == schedule.get("segments"):
        return {"schedule_id": schedule_id, "segments": segments, "amendment": None}
    entry = amendment("amend", payload.effective.isoformat(), payload.reason, user_id)
    await record_amendment(session, company_id, user_id, schedule_id, segments, entry,
                           key=f"{schedule_id}:amend:{len(schedule.get('amendments') or ())}", source="recognition.amend")
    return {"schedule_id": schedule_id, "segments": segments, "amendment": entry}


# ── authorization and the dispatcher ─────────────────────────────────────────────────────

async def _may_post(session, company_id, user_id) -> bool:
    try:
        role, settings = await read_authority(session, company_id, uuid.UUID(str(user_id)))
    except (TypeError, ValueError):
        return False
    return role_has_permission(settings, role, "manage_accounting")


async def run_due(*, session, company_id, today: date, budget: int):
    """The dispatcher's recognition pass for one company (the ``periodic_operation`` slot):
    while automatic posting is authorized and the authorizer still manages accounting and is
    active, post the runs of the latest ended month, if that month is open, at most ``budget``
    a pass (the next pass posts the rest). Older outstanding
    runs wait for a manual post. When the authorization lapses it posts nothing, records the
    lapse once and tells the company."""
    from celerp.notifications import service
    from celerp_docs.periodic import OperationReport

    company = await locked_company(session, company_id)
    settings = company.settings or {}
    consent = (settings.get("consents") or {}).get(CONSENT)
    if not consent:
        return OperationReport()
    if not await _may_post(session, company_id, consent.get("by")):
        if not settings.get(LAPSED):
            company.settings = {**settings, LAPSED: {"by": consent.get("by"), "at": datetime.now(timezone.utc).isoformat()}}
            title, key = NOTICE_LAPSED
            await service.notify_standing(session, company_id, CATEGORY, title,
                                          "Automatic recognition stopped: authorization lapsed",
                                          action_url=PAGE, i18n={"title": f"{key}.title", "body": f"{key}.body", "params": {}})
        return OperationReport()
    if settings.get(LAPSED):
        company.settings = {k: v for k, v in settings.items() if k != LAPSED}
        await service.clear_standing(session, company_id, CATEGORY, NOTICE_LAPSED[0])
    last = today.replace(day=1) - timedelta(days=1)
    lock = settings.get("lock_date")
    if lock and date.fromisoformat(lock) >= last:
        return OperationReport()
    answer = await run_recognition(session, company_id, uuid.UUID(str(consent["by"])), f"{last:%Y-%m}", today=today,
                                   unattended=True, limit=budget)
    return OperationReport(done=answer["posted"])


async def set_authorization(session, company_id, user_id, enabled: bool) -> dict:
    """Turn automatic posting on (recording who and when) or off (clearing that and any lapse)."""
    from celerp.notifications import service

    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    consents = {k: v for k, v in (settings.get("consents") or {}).items() if k != CONSENT}
    if enabled:
        consents[CONSENT] = {"by": str(user_id), "at": datetime.now(timezone.utc).isoformat()}
    settings.pop(LAPSED, None)
    if consents:
        settings["consents"] = consents
    else:
        settings.pop("consents", None)
    company.settings = settings
    await service.clear_standing(session, company_id, CATEGORY, NOTICE_LAPSED[0])
    return {"enabled": enabled}


def summary(settings: dict, runs: list[dict]) -> dict:
    """The outstanding runs by month and whether automatic posting lapsed."""
    from ui.i18n import t

    outstanding = [r for r in runs if r["status"] == "outstanding"]
    lapsed = settings.get(LAPSED) if (settings.get("consents") or {}).get(CONSENT) else None
    return {"outstanding": len(outstanding), "months": sorted({r["month"] for r in outstanding}),
            "lapsed": lapsed, "message": t("recognition.summary_lapsed") if lapsed else None}


# ── roll-forward ─────────────────────────────────────────────────────────────────────────

def _day(entry: dict) -> str:
    return str(entry.get("ts") or "")[:10]


def roll_forward(schedules: dict[str, dict], journals: dict[str, dict], runs: list[dict],
                 date_from: str, date_to: str, by_fund: bool) -> dict:
    """The deferred revenue accounts from ``date_from`` through ``date_to``: the opening
    balance, what was deferred (the schedules' source entries), recognized (run entries less
    their reversals) and adjusted (every other entry on those accounts), and the closing
    balance the schedules still hold unrecognized beside the accounts' balance it reconciles
    to, by fund when funds are on. Lists the outstanding runs of the period and the schedules
    whose source document is void with their posted runs."""
    accounts = {s["deferred_account"] for s in schedules.values()}
    sources = {s.get("source_je") for s in schedules.values()}
    zero = lambda: {"opening": Decimal(0), "deferred": Decimal(0), "recognized": Decimal(0),  # noqa: E731
                    "adjusted": Decimal(0), "closing": Decimal(0), "liability": Decimal(0)}
    totals: dict[str, dict] = defaultdict(zero)
    for entry_id, entry in journals.items():
        if entry.get("status") != "posted" or not (day := _day(entry)) or day > date_to:
            continue
        flow = ("deferred" if entry_id in sources else "recognized" if entry_id.startswith(f"{PREFIX}rev:")
                else "adjusted")
        for line in entry.get("entries") or ():
            if line.get("account") not in accounts:
                continue
            held = to_decimal(line.get("credit") or 0) - to_decimal(line.get("debit") or 0)
            for fund in {"all", line.get("fund_id") or UNRESTRICTED}:
                bucket = totals[fund]
                bucket["liability"] += held
                if day < date_from:
                    bucket["opening"] += held
                else:
                    bucket[flow] += held if flow == "deferred" else -held
    recognized: dict[str, Decimal] = defaultdict(Decimal)
    for entry_id, entry in journals.items():
        parsed = parse_run(entry_id)
        if parsed and entry.get("status") == "posted" and _day(entry) <= date_to and parsed[0][0] in schedules:
            recognized[parsed[0][0]] += to_decimal(posted_amount(entry, schedules[parsed[0][0]]["deferred_account"]))
    for sid, schedule in schedules.items():
        source = journals.get(str(schedule.get("source_je"))) or {}
        held = Decimal(0)
        if source.get("status") == "posted" and _day(source) <= date_to:
            held = sum((to_decimal(s["amount"]) for s in schedule.get("segments") or ()), Decimal(0))
            held += sum((to_decimal(a.get("amount") or 0) for a in schedule.get("amendments") or ()
                         if a.get("kind") == "credit" and str(a.get("effective") or "") > date_to), Decimal(0))
        for fund in {"all", schedule.get("fund_id") or UNRESTRICTED}:
            totals[fund]["closing"] += held - recognized[sid]
    view = lambda b: {k: to_stored_float(v) for k, v in b.items()}  # noqa: E731
    out = {**view(totals["all"]),
           "outstanding": [r for r in runs if r["status"] == "outstanding" and date_from[:7] <= r["month"] <= date_to[:7]],
           "void_source": [{"schedule_id": sid, "doc_id": s.get("doc_id"),
                            "posted": [r for r in runs if r["schedule_id"] == sid and r["status"] == "posted"]}
                           for sid, s in sorted(schedules.items()) if s["stopped"]
                           and any(r["schedule_id"] == sid and r["status"] == "posted" for r in runs)]}
    if by_fund:
        out["by_fund"] = {fund: view(bucket) for fund, bucket in sorted(totals.items()) if fund != "all"}
    return out


# ── routes ───────────────────────────────────────────────────────────────────────────────

class PostIn(BaseModel):
    post_on: str | None = None


class ReverseIn(BaseModel):
    reverse_on: str | None = None


class SettingIn(BaseModel):
    enabled: bool


class AmendIn(BaseModel):
    effective: date
    service_end: date
    amount: FiniteFloat | None = None
    reason: str | None = None


async def _settings(session, company_id) -> dict:
    from celerp.models.company import Company

    return dict((await session.execute(select(Company.settings).where(Company.id == company_id))).scalar_one_or_none() or {})


async def _overview(session, company_id) -> tuple[dict, dict, list[dict]]:
    schedules = await _schedules(session, company_id)
    runs = plan(schedules, await run_ledger(session, company_id), await company_today(session, company_id))
    return await _settings(session, company_id), schedules, runs


@router.get("/recognition")
async def list_recognition(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    """Every run by month (newest first), the number of schedules, the document and line each
    schedule came from, and the summary."""
    settings, schedules, runs = await _overview(session, company_id)
    docs = await _states(session, company_id, "doc", {s.get("doc_id") for s in schedules.values()})
    documents = {}
    for sid, schedule in schedules.items():
        doc = docs.get(str(schedule.get("doc_id"))) or {}
        documents[sid] = {"doc_id": schedule.get("doc_id"), "ref": doc.get("ref_id") or doc.get("doc_number"),
                          "line": schedule.get("line")}
    return {"runs": runs, "schedules": len(schedules), "documents": documents, "summary": summary(settings, runs),
            "enabled": bool((settings.get("consents") or {}).get(CONSENT))}


@router.get("/recognition/summary")
async def recognition_summary(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    settings, _, runs = await _overview(session, company_id)
    return summary(settings, runs)


@router.get("/recognition/roll-forward")
async def recognition_roll_forward(
    date_from: date,
    date_to: date,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    settings, schedules, runs = await _overview(session, company_id)
    accounts = {s["deferred_account"] for s in schedules.values()}
    journals = {entity_id: state for entity_id, state in (await _states(session, company_id, "journal_entry")).items()
                if any(e.get("account") in accounts for e in state.get("entries") or ())}
    return roll_forward(schedules, journals, runs, date_from.isoformat(), date_to.isoformat(), funds_enabled(settings))


@router.get("/recognition/setting")
async def recognition_setting(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
    _: None = require_permission("view_financial_reports"),
) -> dict:
    """Whether recognition posts automatically, who authorized it and when, and its lapse."""
    settings = await _settings(session, company_id)
    consent = (settings.get("consents") or {}).get(CONSENT) or {}
    return {"enabled": bool(consent), "by": consent.get("by"), "at": consent.get("at"),
            "lapsed": settings.get(LAPSED) if consent else None}


@router.put("/recognition/setting")
async def set_recognition_setting(
    payload: SettingIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Post recognition automatically: on records who authorized it and when; off clears it."""
    answer = await set_authorization(session, company_id, user.id, payload.enabled)
    await session.commit()
    return answer


@router.post("/recognition/{month}/post")
async def post_month(
    month: str,
    payload: PostIn | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    answer = await run_recognition(session, company_id, user.id, month, post_on=(payload or PostIn()).post_on)
    await session.commit()
    return answer


@router.post("/recognition/{month}/reverse")
async def reverse_month(
    month: str,
    payload: ReverseIn | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    answer = await reverse_recognition(session, company_id, user.id, month, reverse_on=(payload or ReverseIn()).reverse_on)
    await session.commit()
    return answer


@router.post("/recognition/schedules/{schedule_id}/amend")
async def post_amendment(
    schedule_id: str,
    payload: AmendIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user=Depends(get_current_user),
    _: None = require_permission("manage_accounting"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Close the schedule the day before ``effective`` and plan the rest to ``service_end``."""
    answer = await amend_schedule(session, company_id, user.id, schedule_id, payload)
    await session.commit()
    return answer
