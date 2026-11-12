# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Revenue metrics of the sales agreements: MRR, ARR, churn and cohorts by month.

History is read, never stored. Each sales agreement is replayed from its ledger events with the
projection reducer, each event placed on the business day of its timestamp, and measured at
the end of each month of the range (the last clipped to ``to``). An agreement counts on a day
when it is active, has started (its schedule anchor, else its start date, on or before the day)
and its term is not over. Its MRR there is the subtotal less discount of its lines in force,
scaled to a month; tax and shipping are left out.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import Refused
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine
from celerp.services.business_time import business_date_of
from celerp.services.currencies import CURRENCY_CODES
from celerp.services.money import doc_rate, round_money, to_stored_float
from celerp_docs.doc_money import HEADER_MONEY_FIELDS, document_money
from celerp_subscriptions import billing
from celerp_subscriptions.periods import _DAYS, _MONTHS, add_months

MAX_RANGE_MONTHS = 120
DEFAULT_MONTHS = 12
_SALES = "subscription_invoice"


def default_range(today: date) -> tuple[date, date]:
    """Today and the first day of the 12th month back, counting this one."""
    return add_months(today.replace(day=1), 1 - DEFAULT_MONTHS), today


def range_refusal(start: date, end: date, currency: str | None) -> tuple[str, str] | None:
    """The refusal key and the parameter it names, or None for a range and currency to measure."""
    if start > end:
        return "err_metrics_order", "from"
    if end > add_months(start, MAX_RANGE_MONTHS):
        return "err_metrics_range", "to"
    if currency is not None and currency.upper() not in CURRENCY_CODES:
        return "err_metrics_currency", "currency"
    return None


def measure_days(start: date, end: date) -> list[date]:
    """The last day of each month from ``start``'s to ``end``'s, the last clipped to ``end``."""
    days, month = [], start.replace(day=1)
    while month <= end:
        days.append(min(month.replace(day=calendar.monthrange(month.year, month.month)[1]), end))
        month = add_months(month, 1)
    return days


def _day(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def is_active(state: dict, day: date) -> bool:
    """Active, started on or before ``day`` and before its term end."""
    start = _day(state.get("anchor_date") or state.get("start_date"))
    end = _day(state.get("term_end"))
    return state.get("status") == "active" and start is not None and start <= day and (end is None or day < end)


def monthly_factor(state: dict) -> Decimal | None:
    """What one period's amount is multiplied by to give a month's, or None for no usable period."""
    frequency = state.get("frequency") or "monthly"
    if frequency in _MONTHS:
        return Decimal(1) / _MONTHS[frequency]
    days = _DAYS.get(frequency) or (state.get("custom_interval_days") if frequency == "custom" else None)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        return None
    return Decimal(365) / 12 / days


def own_currency(state: dict, books: str) -> str:
    return str(state.get("currency") or books).upper()


def agreement_mrr(state: dict, day: date, books: str, currency: str | None) -> Decimal | None:
    """The agreement's MRR on ``day`` in ``currency`` (its own, no conversion), or with no
    ``currency`` in the books' at its stored rate. None when it cannot be converted."""
    own = own_currency(state, books)
    factor = monthly_factor(state)
    if factor is None:
        return None
    inputs = {f: state[f] for f in HEADER_MONEY_FIELDS if f in state}
    inputs["currency"] = own
    money = document_money(inputs, billing.lines_on(state, day), own, keep_unrated_tax=True)
    amount = (Decimal(str(money["subtotal"])) - Decimal(str(money["discount_amount"]))) * factor
    if currency is None:
        try:
            rate = doc_rate({**state, "currency": own}, books)
        except Refused:
            return None
        if rate is None:
            return None
        amount *= rate
    return amount


@dataclass
class _Point:
    day: date
    mrr: dict[str, Decimal]
    not_converted: list[str]


def _ratio(part: Decimal | int, whole: Decimal | int) -> float | None:
    return round(float(Decimal(part) / Decimal(whole)), 4) if whole else None


def _money(value: Decimal, currency: str) -> float:
    return to_stored_float(round_money(value, currency))


async def _histories(session: AsyncSession, company_id) -> dict[str, list[LedgerEntry]]:
    """Each sales agreement's ledger events, oldest first."""
    ids = [p.entity_id for p in await billing.agreements(session, company_id) if p.state.get("doc_type") == _SALES]
    if not ids:
        return {}
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "doc",
        LedgerEntry.entity_id.in_(ids)).order_by(LedgerEntry.id))).scalars().all()
    out: dict[str, list[LedgerEntry]] = {i: [] for i in ids}
    for row in rows:
        out[row.entity_id].append(row)
    return out


def _points(histories: dict[str, list[LedgerEntry]], days: list[date], tz: str | None, books: str,
            currency: str | None) -> tuple[list[_Point], dict[str, date]]:
    """The MRR of every counted agreement at each day, and each agreement's start."""
    points = [_Point(d, {}, []) for d in days]
    starts: dict[str, date] = {}
    for eid, entries in histories.items():
        state: dict = {}
        at = 0
        on = [date.fromisoformat(business_date_of(e.ts.isoformat(), tz)) for e in entries]
        for point in points:
            # Events in ledger order up to the end of this day; one dated earlier than an event
            # before it waits for that event, as the state cannot skip one.
            while at < len(entries) and on[at] <= point.day:
                state = ProjectionEngine.replay(state, entries[at])
                at += 1
            if not is_active(state, point.day):
                continue
            if currency is not None and own_currency(state, books) != currency:
                continue
            mrr = agreement_mrr(state, point.day, books, currency)
            if mrr is None:
                point.not_converted.append(eid)
                continue
            point.mrr[eid] = mrr
            starts.setdefault(eid, _day(state.get("anchor_date") or state.get("start_date")))
    return points, starts


def _month(prev: _Point, cur: _Point, cur_code: str) -> dict:
    was, now = prev.mrr, cur.mrr
    kept = was.keys() & now.keys()
    churned = [e for e in was if e not in now]
    new = sum((now[e] for e in now if e not in was), Decimal(0))
    expansion = sum((now[e] - was[e] for e in kept if now[e] > was[e]), Decimal(0))
    contraction = sum((was[e] - now[e] for e in kept if was[e] > now[e]), Decimal(0))
    lost = sum((was[e] for e in churned), Decimal(0))
    mrr = sum(now.values(), Decimal(0))
    return {
        "month": cur.day.strftime("%Y-%m"), "day": cur.day.isoformat(),
        "mrr": _money(mrr, cur_code), "arr": _money(mrr * 12, cur_code), "active": len(now),
        "new_mrr": _money(new, cur_code), "expansion_mrr": _money(expansion, cur_code),
        "contraction_mrr": _money(contraction, cur_code), "churned_mrr": _money(lost, cur_code),
        "logo_churn": _ratio(len(churned), len(was)),
        "revenue_churn": _ratio(lost + contraction, sum(was.values(), Decimal(0))),
        "not_converted": sorted(cur.not_converted),
    }


def _cohorts(points: list[_Point], starts: dict[str, date], cur_code: str) -> list[dict]:
    """Agreements by start month in the range, with the count and MRR still active at each
    month end from then on."""
    months = [p.day.strftime("%Y-%m") for p in points]
    out = []
    for i, month in enumerate(months):
        members = {e for e, s in starts.items() if s is not None and s.strftime("%Y-%m") == month}
        if not members:
            continue
        later = points[i:]
        out.append({
            "month": month, "size": len(members),
            "active": [len(members & p.mrr.keys()) for p in later],
            "mrr": [_money(sum((p.mrr[e] for e in members & p.mrr.keys()), Decimal(0)), cur_code) for p in later],
        })
    return out


async def revenue_metrics(session: AsyncSession, company_id, start: date, end: date, currency: str | None) -> dict:
    """The answer of GET /subscriptions/metrics for a checked range (range_refusal)."""
    settings = (await session.execute(select(Company.settings).where(Company.id == company_id))).scalar_one_or_none() or {}
    books = str(settings.get("currency") or "USD").upper()
    code = currency.upper() if currency else None
    days = measure_days(start, end)
    before = start.replace(day=1) - timedelta(days=1)
    points, starts = _points(await _histories(session, company_id), [before, *days], settings.get("timezone"),
                             books, code)
    shown = code or books
    range_starts = {e: s for e, s in starts.items() if s is not None and before < s <= end}
    return {
        "currency": shown,
        "months": [_month(prev, cur, shown) for prev, cur in zip(points, points[1:])],
        "cohorts": _cohorts(points[1:], range_starts, shown),
    }
