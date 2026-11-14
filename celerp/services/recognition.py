# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Deferred revenue at finalize: which sale lines defer, over what period, and the schedule
each one makes.

A line defers when it is not an asset line, its item's category is not a gift category
(``gift_categories``, a company setting of category keys) and its item, or the agreement the
document bills, recognizes over the service period (``RECOGNIZE_FIELD``). Its share of the
sale credits the deferred revenue account instead of its revenue account, and a schedule
``rev:{doc}:{line_id}`` (a ``rev_schedule`` projection) records what is to be recognized:
the source line, the revenue account it would have credited, the deferred account, the
fund, the currencies and rate, the source entry and segments of {start, end, amount}. The
monthly runs that recognize it are planned from the schedule and the ledger (below;
posted by celerp_accounting.recognition); nothing recognized is stored on the schedule.
Amendments and credit notes close a schedule at a day and plan what is left from it.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import RECOGNIZE_FIELD, catalog_refusal
from celerp.models.projections import Projection
from celerp.services.document_lines import line_item_id
from celerp.services.money import round_money, to_decimal, to_stored_float

GIFT_CATEGORIES = "gift_categories"
SCHEDULE = "rev_schedule"
BASIS = "daily"


def schedule_id(doc_id: str, line_id: str) -> str:
    return f"rev:{doc_id}:{line_id}"


def gift_categories(settings: Mapping) -> frozenset[str]:
    return frozenset(settings.get(GIFT_CATEGORIES) or ())


def deferred_line(settings: Mapping, line: Mapping, item: Mapping | None, agreement: Mapping | None) -> bool:
    """Whether ``line`` defers its revenue: not an asset line, its item outside the gift
    categories, and the item's or the source agreement's flag on."""
    if line.get("asset_id"):
        return False
    item = item or {}
    if item.get("category") and str(item["category"]) in gift_categories(settings):
        return False
    return bool(item.get(RECOGNIZE_FIELD) or (agreement or {}).get(RECOGNIZE_FIELD))


def _iso_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def service_period(doc: Mapping, line: Mapping) -> tuple[date, date] | None:
    """The line's own service period, else the document's; None when there is none or it
    ends before it starts."""
    start, end = ((line.get("service_start"), line.get("service_end"))
                  if line.get("service_start") or line.get("service_end")
                  else (doc.get("service_start"), doc.get("service_end")))
    start, end = _iso_day(start), _iso_day(end)
    return (start, end) if start and end and end >= start else None


async def _states(session, company_id, entity_type: str, ids: set) -> dict[str, dict]:
    ids = {str(i) for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == entity_type,
        Projection.entity_id.in_(sorted(ids))))).scalars().all()
    return {row.entity_id: row.state or {} for row in rows}


async def deferred_lines(session, company_id, doc: Mapping, settings: Mapping) -> dict[str, tuple[date, date]]:
    """The service period of each line of a sale that defers (deferred_line), keyed by the
    line's index as a string. Refuses (422, naming the line) a deferred line with no period
    or one ending before it starts."""
    lines = [li if isinstance(li, dict) else {} for li in doc.get("line_items") or []]
    items = await _states(session, company_id, "item", {line_item_id(li) for li in lines})
    agreements = await _states(session, company_id, "doc",
                               {li.get("source_agreement") or doc.get("source_agreement") for li in lines})
    periods: dict[str, tuple[date, date]] = {}
    for index, li in enumerate(lines):
        agreement = agreements.get(str(li.get("source_agreement") or doc.get("source_agreement") or ""))
        if not deferred_line(settings, li, items.get(str(line_item_id(li))), agreement):
            continue
        period = service_period(doc, li)
        if period is None:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "recognition.err_service_period", line=li.get("name") or str(index + 1)))
        periods[str(index)] = period
    return periods


def schedule_records(doc_id: str, doc: Mapping, *, periods: Mapping[str, tuple[date, date]],
                     allocations: Mapping[str, dict], revenue_accounts: Mapping[str, str], deferred: str,
                     funds: Mapping[int, str | None], books_currency: str, rate, source_je: str) -> list[dict]:
    """The schedule each deferred line makes, as the ``rev_schedule.created`` data: its line's
    share in the books' currency (``allocations``) recognized over its period in one segment."""
    lines = doc.get("line_items") or []
    records = []
    for index, (start, end) in periods.items():
        line = lines[int(index)]
        records.append({
            "doc_id": doc_id, "line_id": line.get("line_id"), "line": int(index) + 1,
            "account": revenue_accounts[index], "deferred_account": deferred,
            "fund_id": funds.get(int(index) + 1), "currency": books_currency,
            "doc_currency": doc.get("currency") or books_currency, "rate": float(rate),
            "basis": BASIS, "source_je": source_je,
            "segments": [{"start": start.isoformat(), "end": end.isoformat(),
                          "amount": allocations[index]["base"]}],
            "amendments": [],
        })
    return records


def amendment(kind: str, effective: str, reason: str | None, user_id, **credit) -> dict:
    """An entry of a schedule's amendment history; a credit carries its note_id and amount."""
    return {"kind": kind, "effective": effective, "reason": reason, "by": str(user_id) if user_id else None,
            "at": datetime.now(timezone.utc).isoformat(), **credit}


# ── runs and the ledger ──────────────────────────────────────────────────────────────────
#
# Each segment recognizes over its days: through the end of month m it has recognized
# ``cum(m) = round(amount * days through m / days)``, and the run of month m is
# ``cum(m) - cum(m - 1)``, so a missed run stays in its own month. What has been recognized
# is read from the ledger, never stored: a run's entry is
# ``je:auto:{schedule}:{segment}:{yyyy-mm}:{n}``, n counting its posts, and its reversal is
# that id with ``:reversal``. A run is posted while its latest entry stands unreversed. When
# Change fund moves a posted run onto a new fund, the run's entry is voided naming its copy
# ``{entry}:fund:{n}``, and the run stands as that copy.

PREFIX = "je:auto:"
REVERSAL = ":reversal"


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months(start: date, end: date):
    """(yyyy-mm, month end) of every month from ``start``'s through ``end``'s."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield f"{year:04d}-{month:02d}", month_end(year, month)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _period(segment: Mapping) -> tuple[date, date]:
    return date.fromisoformat(segment["start"]), date.fromisoformat(segment["end"])


def _month_runs(segment: Mapping, currency: str):
    """(month, month end, run amount) of every month of ``segment``, a run of 0 included."""
    start, end = _period(segment)
    amount, days = to_decimal(segment["amount"]), (end - start).days + 1
    before = Decimal(0)
    for month, last in _months(start, end):
        cum = round_money(amount * ((min(end, last) - start).days + 1) / days, currency)
        yield month, last, cum - before
        before = cum


def segment_runs(segment: Mapping, currency: str, today: date) -> list[tuple[str, date, Decimal]]:
    """(month, month end, amount) of each month of ``segment`` ended before ``today`` whose
    run moves money."""
    return [(month, last, amount) for month, last, amount in _month_runs(segment, currency)
            if last < today and amount]


def parse_run(entry_id: str) -> tuple[tuple[str, int, str], int, bool] | None:
    """((schedule, segment, month), n, is reversal) of a run entry id; a copy Change fund
    posted in a run entry's place reads as that entry."""
    root = entry_id.split(":fund:", 1)[0]
    reversal = root.endswith(REVERSAL)
    body = root.removeprefix(PREFIX).removesuffix(REVERSAL)
    try:
        schedule, segment, month, n = body.rsplit(":", 3)
        return (schedule, int(segment), month), int(n), reversal
    except ValueError:
        return None


class Ledger:
    """The run entries of the company: per run key, each post n with the entry standing for
    it (``root`` its own id, ``id`` the copy Change fund replaced it by, when it did), and
    which posts are reversed."""

    def __init__(self, rows: list[tuple[str, dict]]):
        self.posts: dict[tuple, dict[int, dict]] = defaultdict(dict)
        self.reversals: dict[tuple, dict[int, dict]] = defaultdict(dict)
        states = {entity_id: state or {} for entity_id, state in rows}
        for entity_id, state in states.items():
            parsed = None if ":fund:" in entity_id else parse_run(entity_id)
            if parsed is None:
                continue
            live, seen = entity_id, {entity_id}
            while state.get("status") == "void" and state.get("replaced_by") in states.keys() - seen:
                live = state["replaced_by"]
                seen.add(live)
                state = states[live]
            key, n, reversal = parsed
            (self.reversals if reversal else self.posts)[key][n] = {**state, "id": live, "root": entity_id}

    def next_n(self, key) -> int:
        return max(self.posts.get(key) or {0: None}) + 1

    def latest(self, key) -> tuple[int, dict, dict | None] | None:
        """The latest post of ``key`` and its reversal, when it was posted at all."""
        posts = self.posts.get(key) or {}
        if not posts:
            return None
        n = max(posts)
        return n, posts[n], (self.reversals.get(key) or {}).get(n)

    def live(self, key) -> dict | None:
        """The entry the run stands as: its latest post, when that stands unreversed."""
        latest = self.latest(key)
        if latest is None:
            return None
        _, entry, reversal = latest
        if entry.get("status") != "posted" or (reversal and reversal.get("status") == "posted"):
            return None
        return entry

    def keys(self):
        return self.posts.keys()


async def run_ledger(session, company_id) -> Ledger:
    """Every run entry and reversal of the company, the copies Change fund posted included."""
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(f"{PREFIX}rev:", autoescape=True)))).all()
    return Ledger(rows)


def posted_amount(entry: Mapping, account: str) -> float:
    """What a run entry moved off the deferred account (its debits there less its credits)."""
    total = sum((to_decimal(e.get("debit") or 0) - to_decimal(e.get("credit") or 0)
                 for e in entry.get("entries") or () if e.get("account") == account), Decimal(0))
    return to_stored_float(total)


# ── amendments and credits ───────────────────────────────────────────────────────────────
#
# Both close the schedule at a day and plan what is left from it in one new segment. The
# segments before that day stay as they are (their runs keep their keys); the one it falls in
# is closed the day before, holding its share through then plus whatever its posted runs moved
# beyond what was planned for them, so its own runs still sum to what was posted.

async def doc_schedules(session, company_id, doc_id: str, *, lock: bool = False) -> dict[str, dict]:
    """The schedules of document ``doc_id``; with ``lock`` their rows locked for a write."""
    from celerp.services.company_lock import lock_projections

    ids = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id, Projection.entity_type == SCHEDULE,
        Projection.entity_id.startswith(f"rev:{doc_id}:", autoescape=True)))).scalars().all()
    if lock and ids:
        return {sid: dict(row.state or {}) for sid, row in (await lock_projections(session, company_id, ids)).items()}
    return await _states(session, company_id, SCHEDULE, set(ids))


def _total(segments) -> Decimal:
    return sum((to_decimal(s["amount"]) for s in segments), Decimal(0))


def _covered(sid: str, schedule: Mapping, ledger: Ledger) -> list[date]:
    """The last day each run of the schedule standing posted covers."""
    return [min(end, last) for index, segment in enumerate(schedule.get("segments") or ())
            for (start, end) in [_period(segment)] for month, last in _months(start, end)
            if ledger.live((sid, index, month)) is not None]


def _closed(sid: str, schedule: Mapping, ledger: Ledger, at: date, currency: str) -> list[dict]:
    """The schedule's segments closed the day before ``at``."""
    cut, kept = at - timedelta(days=1), []
    for index, segment in enumerate(schedule.get("segments") or ()):
        start, end = _period(segment)
        if start > cut:
            break
        if end <= cut:
            kept.append(dict(segment))
            continue
        amount = round_money(to_decimal(segment["amount"]) * ((cut - start).days + 1) / ((end - start).days + 1), currency)
        for month, _, planned in _month_runs(segment, currency):
            if (live := ledger.live((sid, index, month))) is not None:
                amount += to_decimal(posted_amount(live, schedule["deferred_account"])) - planned
        kept.append({"start": segment["start"], "end": cut.isoformat(), "amount": to_stored_float(amount)})
    return kept


def amended_segments(sid: str, schedule: Mapping, ledger: Ledger, *, effective: date, service_end: date,
                     amount=None) -> list[dict]:
    """The segments of an amendment taking effect on ``effective``: the schedule closed the day
    before, and a segment from ``effective`` through ``service_end`` holding the rest, or
    ``amount`` (the schedule's new total) less what the closed segments hold. Refused while a
    posted run covers a day from ``effective`` on (reverse those runs first), and when the
    period ends before it starts or the closed segments hold more than ``amount``."""
    currency = schedule["currency"]
    if any(day >= effective for day in _covered(sid, schedule, ledger)):
        raise HTTPException(status_code=409, detail=catalog_refusal(
            "recognition.err_amend_posted", effective=effective.isoformat()))
    kept = _closed(sid, schedule, ledger, effective, currency)
    rest = to_decimal(_total(schedule.get("segments") or ()) if amount is None else amount) - _total(kept)
    if service_end < effective or rest < 0:
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "recognition.err_amend_invalid", held=to_stored_float(_total(kept))))
    return [*kept, {"start": effective.isoformat(), "end": service_end.isoformat(), "amount": to_stored_float(rest)}]


def credited_segments(sid: str, schedule: Mapping, ledger: Ledger, amount, day: date) -> tuple[Decimal, list[dict]]:
    """A credit of ``amount`` on the schedule from a note dated ``day``: what it takes off
    deferred revenue (at most what is planned from the first day no posted run covers, or the
    first of the note's month when that is later) and the segments that leave: the schedule
    closed the day before that day and the rest planned from it to the service end. The
    caller puts the remainder of ``amount`` on the revenue account."""
    currency, segments = schedule["currency"], schedule.get("segments") or ()
    covered = _covered(sid, schedule, ledger)
    first_open = max(covered) + timedelta(days=1) if covered else _period(segments[0])[0]
    at = max(first_open, day.replace(day=1))
    kept = _closed(sid, schedule, ledger, at, currency)
    planned = _total(segments) - _total(kept)
    deferred = min(to_decimal(amount), max(planned, Decimal(0)))
    rest = planned - deferred
    end = max(_period(s)[1] for s in segments)
    return deferred, [*kept, *([{"start": at.isoformat(), "end": end.isoformat(), "amount": to_stored_float(rest)}]
                               if rest and at <= end else [])]


async def record_amendment(session, company_id, user_id, sid: str, segments: list[dict], entry: dict, *,
                           key: str, source: str) -> None:
    """Write the schedule's new segments and the amendment that made them, once per ``key``."""
    from celerp.events.engine import emit_event

    await emit_event(session, company_id=company_id, entity_id=sid, entity_type=SCHEDULE,
                     event_type="rev_schedule.amended", data={"segments": segments, "amendment": entry},
                     actor_id=user_id, location_id=None, source=source, idempotency_key=key, metadata_={})
