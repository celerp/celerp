# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Depreciation runs: one journal per month, exceptions held per asset, exactly once.

A run for month M depreciates every month from the one after the latest posted run
through M. Each open month gets its own journal (``run:<yyyy-mm>:<cycle>``) dated at its
month end; anything owed for a locked or earlier month is caught up in M's journal,
on movements dated at the month it belongs to. Every month has a run record
(``fa:run:<yyyy-mm>``) saying what was prepared or posted.

What an asset owes is its schedule (calc.engine) less the depreciation already on it,
so a month posted, linked from a manual journal or reversed is never charged twice and
never lost. An asset that cannot be charged (an account that no longer takes postings,
a voided source, a missing reading, a month skipped with a reason) is held from the
first month it cannot be charged: it posts nothing from then on, the rest of the run
posts, and the asset carries the exception until a later run catches it up.

The monthly loop prepares last month's run once the run day has come, or posts it as
the user who chose automatic posting while that user may still post journals.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import uuid
from datetime import date
from decimal import Decimal

from celerp.models.projections import Projection
from celerp.services.business_time import business_today, company_today
from celerp.services.money import books_currency, round_money
from celerp_fixed_assets import notices, operations, setup, sources
from celerp_fixed_assets.calc import rules
from celerp_fixed_assets.calc.engine import (
    Acquire,
    Dispose,
    EstimateChange,
    Impair,
    Improve,
    Opening,
    Policy,
    Revalue,
    schedule,
)
from celerp_fixed_assets.calc.periods import month_index, parse_period, period_label
from celerp_fixed_assets.operations import MODULE, _dec, _move, conflict
from celerp_fixed_assets.projections import REMEASUREMENTS, live, money
from celerp_fixed_assets.store import _emit, _entities, _state, movements_by_asset
from fastapi import HTTPException
from sqlalchemy import func, select

log = logging.getLogger(__name__)

_JOURNAL = f"je:mod:{MODULE}:run:"
_POSTED = ("posted", "posted_with_exceptions")
_CHECK_INTERVAL_SECONDS = 3600


def _end(index: int) -> date:
    year, month = index // 12, index % 12 + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _long(index: int) -> str:
    return f"{calendar.month_name[index % 12 + 1]} {index // 12}"


def _month(value, field: str = "The month") -> int:
    text = str(value or "")
    refusal = f"Enter {field[0].lower()}{field[1:]} as YYYY-MM, such as 2026-09."
    try:
        index = parse_period(text)
    except ValueError:
        raise setup.unprocessable(refusal) from None
    if period_label(index) != text:
        raise setup.unprocessable(refusal)
    return index


# ── What the books hold ──────────────────────────────────────────────────────


class Books:
    """Everything a run is planned from, read under the company lock."""

    def __init__(self, settings: dict, records: dict, journals: dict):
        self.settings = settings
        self.currency = books_currency(settings)
        self.fa = setup.settings_of(settings)
        self.records = records  # month index -> run record
        self.journals = journals  # month index -> {cycle: journal state}
        lock = settings.get("lock_date")
        self.lock = date.fromisoformat(lock) if lock else None
        self.first = month_index(date.fromisoformat(self.fa["first_managed_period"]))
        posted = [k for k, r in records.items() if r.get("status") in _POSTED]
        posted += [k for k, cycles in journals.items() if self.latest_posted(k)]
        self.last = max(posted, default=None)
        # The first month the next run posts.
        self.next = self.last + 1 if self.last is not None else self.first

    def locked(self, index: int) -> bool:
        return self.lock is not None and self.lock >= _end(index)

    def latest_posted(self, index: int) -> tuple[int, dict] | None:
        cycles = self.journals.get(index) or {}
        if not cycles:
            return None
        cycle = max(cycles)
        return (cycle, cycles[cycle]) if cycles[cycle].get("status") == "posted" else None

    def next_cycle(self, index: int) -> int:
        return max(int((self.records.get(index) or {}).get("cycle") or 0), *(self.journals.get(index) or {0: None})) + 1


async def _books(session, company_id) -> Books:
    settings = await setup.company_settings(session, company_id)
    records = {parse_period(s["month"]): s for _, s in await _entities(session, company_id, "fa_run")}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(_JOURNAL, autoescape=True)))).all()
    journals: dict[int, dict[int, dict]] = {}
    for entity_id, state in rows:
        month, cycle = entity_id.removeprefix(_JOURNAL).split(":")
        journals.setdefault(parse_period(month), {})[int(cycle)] = {"id": entity_id, **state}
    return Books(settings, records, journals)


async def _ledger_basis(session, company_id, books: Books) -> list:
    """What the plan read that its preview does not show: every journal's version (a
    posting or void anywhere moves it) and the period lock."""
    count, versions = (await session.execute(select(func.count(), func.coalesce(func.sum(Projection.version), 0)).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).one()
    return [int(count), int(versions), books.settings.get("lock_date")]


# ── One asset ────────────────────────────────────────────────────────────────


# The operations that bring an asset in at the cutover, and the correction of those values.
OPENING_OPERATIONS = ("opening", "opening_values")
# Operations whose cost is not an acquisition: the asset leaving the books in whole or part,
# a remeasurement, an improvement (a layer of its own), and construction placed in service.
_NOT_ACQUIRED = (*OPENING_OPERATIONS, *REMEASUREMENTS, "disposal", "improvement", "construction_out", "reclassify")


def _first(movements: list[dict]) -> int | None:
    """The month index the asset entered service (the month after the cutover for an opening asset)."""
    starts = [month_index(date.fromisoformat(m["posting_date"])) + 1 for m in movements
              if m.get("operation") == "opening"]
    # Net cost per day, so a correction moving the available date leaves no start behind.
    by_day: dict[str, Decimal] = {}
    for m in movements:
        if m.get("operation") not in _NOT_ACQUIRED:
            day = m["effective_date"]
            by_day[day] = by_day.get(day, Decimal(0)) + _dec((m.get("amounts") or {}).get("cost"))
    starts += [month_index(date.fromisoformat(day)) for day, cost in by_day.items() if cost]
    return min(starts, default=None)


def estimates(movements: list[dict]) -> list[dict]:
    """The live changes in estimate, by date: each carries the policy from its date on and
    the one it replaced. A reclassification brings in the new class's policy as one."""
    return sorted((m for m in live(movements) if m.get("operation") == "estimate"
                   or (m.get("operation") == "reclassify" and m.get("policy"))), key=lambda m: m["effective_date"])


def base_policy(asset: dict, movements: list[dict]) -> dict:
    """The policy the asset started under: the one its first change in estimate replaced,
    or its policy when it has none."""
    changes = estimates(movements)
    return changes[0]["previous"] if changes else asset["policy"]


def engine_movements(asset: dict, movements: list[dict], currency: str) -> list:
    """The engine's view of an asset's movements: what it was acquired at (by effective
    date) or brought in at on the cutover, and each change in estimate."""
    movements = live(movements)
    policy = base_policy(asset, movements)
    out = []
    opening = [m for m in movements if m.get("operation") in OPENING_OPERATIONS]
    first = next((m for m in opening if m.get("operation") == "opening"), None)
    if first is not None:
        totals = {k: sum((_dec((m.get("amounts") or {}).get(k)) for m in opening), Decimal(0))
                  for k in ("cost", "accumulated_depreciation", "accumulated_impairment")}
        # The life left at the cutover: the life less the months used through it.
        cutover = date.fromisoformat(first["posting_date"])
        used = month_index(cutover) - month_index(date.fromisoformat(first["effective_date"])) + 1
        out.append(Opening(cutover, totals["cost"], totals["accumulated_depreciation"]
                           + totals["accumulated_impairment"], max(int(policy.get("life_months") or 0) - used, 0)))
    by_day: dict[str, Decimal] = {}
    for m in movements:
        cost = _dec((m.get("amounts") or {}).get("cost"))
        if m.get("operation") not in _NOT_ACQUIRED and cost:
            by_day[m["effective_date"]] = by_day.get(m["effective_date"], Decimal(0)) + cost
    out += [Acquire(date.fromisoformat(day), cost) for day, cost in sorted(by_day.items()) if cost]
    for m in movements:
        on = date.fromisoformat(m["effective_date"])
        if m.get("operation") == "improvement":
            out.append(Improve(on, _dec(m["amounts"].get("cost")), m.get("life_months")))
        elif m.get("operation") == "revaluation":
            out.append(Revalue(on, _dec(m["carrying_amount"])))
        elif m.get("operation") in REMEASUREMENTS:
            out.append(Impair(on, _dec(m["carrying_amount"])))
    disposals = sorted((m for m in movements if m.get("operation") == "disposal"), key=lambda m: m["effective_date"])
    out += [Dispose(date.fromisoformat(m["effective_date"]), _dec(m.get("share") or 1)) for m in disposals]
    start = _first(movements)
    readings = asset.get("readings") or {}
    for m in estimates(movements):
        on, new = date.fromisoformat(m["effective_date"]), m["policy"]
        method = rules.method(new["method"])
        if method.uses_readings:
            used = sum((_dec(u) for p, u in readings.items() if parse_period(p) < month_index(on)), Decimal(0))
            left = _dec(new["params"].get("total_units")) - used
        else:
            left = Decimal(int(new.get("life_months") or 0) - max(month_index(on) - (start or month_index(on)), 0))
        cost = sum(by_day.get(d, Decimal(0)) for d in by_day if d <= m["effective_date"])
        cost += sum(_dec(d["amounts"].get("cost")) for d in disposals if d["effective_date"] <= m["effective_date"])
        if first is not None:
            cost += totals["cost"]
        out.append(EstimateChange(on, max(left, Decimal(0)), round_money(cost * _dec(new["residual_percent"]) / 100,
                                                                          currency),
                                  method=new["method"], params=new.get("params") or {},
                                  convention=new.get("convention")))
    return out


def unavailable_rule(asset: dict, movements: list[dict], settings: dict) -> str | None:
    """Why the asset cannot be depreciated for this company: a method or convention it
    uses comes from a module the company has turned off, or that is no longer installed."""
    policies = [base_policy(asset, movements), *(m["policy"] for m in estimates(movements))]
    for policy in policies:
        for kind, key in (("method", policy.get("method")), ("convention", policy.get("convention") or "full_month")):
            if not rules.is_available(kind, key, settings):
                return rules.unavailable_reason(kind, key)
    return None


def asset_schedule(asset: dict, movements: list[dict], settings: dict, through: int) -> list:
    """The asset's schedule through month ``through`` (calc.engine PeriodAmount rows)."""
    return full_schedule(asset, movements, settings, through).periods


def full_schedule(asset: dict, movements: list[dict], settings: dict, through: int):
    """The asset's calc.engine Schedule through month ``through``."""
    currency = books_currency(settings)
    policy = base_policy(asset, movements)
    # The residual is a share of what the asset was acquired or brought in at.
    cost = sum((_dec((m.get("amounts") or {}).get("cost")) for m in live(movements)
                if m.get("operation") not in (*REMEASUREMENTS, "disposal", "improvement", "construction_out")),
               Decimal(0))
    engine_policy = Policy(policy["method"], int(policy.get("life_months") or 0),
                           round_money(cost * _dec(policy.get("residual_percent")) / 100, currency),
                           policy.get("convention") or "full_month",
                           int(str(settings.get("fiscal_year_start") or "01-01")[:2]), policy.get("params") or {})
    readings = {p: _dec(u) for p, u in (asset.get("readings") or {}).items()}
    return schedule(engine_movements(asset, movements, currency), engine_policy, through=period_label(through),
                    currency=currency, readings=readings)


def charged_by_month(movements: list[dict]) -> dict[int, Decimal]:
    """Depreciation already on the asset, by the month it belongs to. Opening values are
    not charges; a correction or remeasurement says which months it charged for."""
    out: dict[int, Decimal] = {}
    for m in live(movements):
        if m.get("operation") in OPENING_OPERATIONS:
            continue
        if m.get("periods") is not None:
            for p, amount in m["periods"].items():
                out[parse_period(p)] = out.get(parse_period(p), Decimal(0)) + _dec(amount)
            continue
        amount = _dec((m.get("amounts") or {}).get("accumulated_depreciation"))
        if amount:
            k = month_index(date.fromisoformat(m["effective_date"]))
            out[k] = out.get(k, Decimal(0)) + amount
    return out


class Charge:
    """What one asset owes month by month through the run month, and why it is held."""

    def __init__(self, asset_id: str, asset: dict, cls: dict, movements: list[dict], books: Books, through: int,
                 start: int):
        self.asset_id, self.asset, self.cls, self.movements = asset_id, asset, cls, movements
        self.expense = asset.get("depreciation_account") or cls["depreciation_account"]
        self.accumulated = cls["accumulated_depreciation_account"]
        self.reason = unavailable_rule(asset, movements, books.settings)
        periods = [] if self.reason else asset_schedule(asset, movements, books.settings, through)
        charged = sum(charged_by_month(movements).values(), Decimal(0))
        # Owed per month: the schedule to date beyond what is on the asset already.
        self.owed: dict[int, Decimal] = {}
        self.missing: set[int] = set()
        before = Decimal(0)
        for p in periods:
            index = parse_period(p.period)
            total = before + p.amount
            self.owed[index] = max(Decimal(0), total - max(charged, before))
            before = total
            if p.exception is not None:
                self.missing.add(index)
        self.start = start
        self.held_from: int | None = None
        self.action: str | None = None

    async def hold(self, session, company_id, books: Books, through: int, booked: dict[str, list[int]]) -> None:
        """The first month this asset cannot be charged, and what clears it. ``booked`` holds,
        per accumulated depreciation account, the months of depreciation journals entered
        by hand and not yet allocated to assets or kept: the run would charge those months
        twice, so every asset in service then on that account waits for them."""
        from celerp.services.journal_accounts import require_destinations

        unposted = [k for k, owed in self.owed.items() if owed or k >= self.start]
        first = min([*unposted, self.start])
        found: list[tuple[int, str]] = []
        if self.reason:
            found.append((first, "method_unavailable"))
        try:
            await require_destinations(session, company_id, {self.expense, self.accumulated})
        except HTTPException:
            found.append((first, "fix_account"))
        if await self._void_source(session, company_id):
            found.append((first, "review_source"))
        open_from = parse_period(self.asset["exception"]["first_month"]) if self.asset.get("exception") else self.start
        found += [(k, "enter_reading") for k in sorted(self.missing) if open_from <= k <= through][:1]
        found += [(k, "skip") for k in sorted(parse_period(m) for m in self.asset.get("skips") or {})
                  if self.start <= k <= through and not books.locked(k)][:1]
        in_service = _first(live(self.movements))
        found += [(max(k, self.start), "disposition_journal") for k in sorted(booked.get(self.accumulated, []))
                  if in_service is not None and in_service <= k <= through][:1]
        if found:
            self.held_from, self.action = min(found)

    async def _void_source(self, session, company_id) -> bool:
        """Whether the asset still holds part of a voided journal's line. A source replaced
        by Correct nets to nothing and no longer holds it."""
        net: dict[tuple, Decimal] = {}
        for m in self.movements:
            for c in m.get("contributions") or []:
                if not str(c.get("journal_id")).startswith(_JOURNAL):
                    key = (c["journal_id"], c.get("line_index"))
                    net[key] = net.get(key, Decimal(0)) + _dec(c.get("amount"))
        journals = {je for (je, _), amount in net.items() if amount}
        for je in journals:
            state = await _state(session, company_id, je)
            if state is not None and state.get("status") == "void":
                return True
        return False

    def postable(self) -> list[tuple[int, Decimal]]:
        return [(k, a) for k, a in sorted(self.owed.items()) if a and (self.held_from is None or k < self.held_from)]

    def held(self, through: int) -> list[int]:
        return [] if self.held_from is None else [k for k in range(self.held_from, through + 1)]

    def held_amount(self, months) -> Decimal | None:
        if any(k in self.missing for k in months):
            return None
        return sum((self.owed.get(k, Decimal(0)) for k in months), Decimal(0))

    def exception(self, through: int, user_id) -> dict | None:
        months = self.held(through)
        if not months:
            return None
        amount = self.held_amount(months)
        return {"first_month": period_label(months[0]), "months": [period_label(k) for k in months],
                "amount": None if amount is None else str(amount), "action": self.action,
                "reason": self.reason if self.action == "method_unavailable" else None,
                "responsible_user_id": str(user_id) if user_id else None}


def depreciates(asset: dict, cls: dict | None) -> bool:
    """Whether runs charge the asset: in use, in a class, on a depreciation method."""
    return asset.get("status") == "active" and cls is not None and asset["policy"]["method"] != "none"


async def next_charge(session, company_id, asset_id: str, asset: dict) -> dict | None:
    """What the first run that charges the asset charges it: the run month (the asset's own
    first month when it starts after the next run), the amount, and the earlier months that
    amount catches up; or held when something holds the asset then. None when no run charges it."""
    cls = await _state(session, company_id, str(asset.get("class_id") or ""))
    if not depreciates(asset, cls):
        return None
    books = await _books(session, company_id)
    movements = await operations.asset_movements(session, company_id, asset_id)
    month = max(books.next, _first(live(movements)) or books.next)
    charge = Charge(asset_id, asset, cls, movements, books, month, books.next)
    await charge.hold(session, company_id, books, month, {})
    if charge.held(month):
        return {"month": period_label(month), "held": True, "amount": None, "catches_up": []}
    postable = charge.postable()
    return {"month": period_label(month), "held": False,
            "amount": money(sum((a for _, a in postable), Decimal(0)), books.currency),
            "catches_up": [period_label(k) for k, _ in postable if k < month]}


async def _charges(session, company_id, books: Books, through: int, start: int) -> list[Charge]:
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    movements = await movements_by_asset(session, company_id)
    booked: dict[str, list[int]] = {}
    for line in await sources.intake(session, company_id):
        if line["kind"] == "depreciation":
            booked.setdefault(line["account"], []).append(month_index(date.fromisoformat(line["date"])))
    out = []
    for asset_id, asset in sorted(await _entities(session, company_id, "fa_asset"), key=lambda r: r[1].get("number") or ""):
        cls = classes.get(asset.get("class_id"))
        if not depreciates(asset, cls):
            continue
        charge = Charge(asset_id, asset, cls, movements.get(asset_id, []), books, through, start)
        await charge.hold(session, company_id, books, through, booked)
        out.append(charge)
    return out


# ── Planning a run ───────────────────────────────────────────────────────────


def _lines(items: list[dict]):
    """A month's journal: one debit per expense account, one credit per accumulated
    depreciation account; each item learns the index of the credit line it is part of."""
    from celerp.services.module_journals import JournalLine

    debits: dict[str, Decimal] = {}
    credits: dict[str, Decimal] = {}
    for it in items:
        debits[it["expense"]] = debits.get(it["expense"], Decimal(0)) + it["amount"]
        credits[it["accumulated"]] = credits.get(it["accumulated"], Decimal(0)) + it["amount"]
    lines = [JournalLine(account=a, debit=v) for a, v in sorted(debits.items())]
    index = {a: len(lines) + i for i, a in enumerate(sorted(credits))}
    lines += [JournalLine(account=a, credit=v) for a, v in sorted(credits.items())]
    for it in items:
        it["line_index"] = index[it["accumulated"]]
    return lines


async def plan_run(session, company_id, month: int, today: date, user_id) -> tuple[dict, dict]:
    """What a run through ``month`` posts, as the preview shows it, and what posting it needs."""
    books = await _books(session, company_id)
    currency = books.currency
    label = period_label(month)
    if books.last is not None and month <= books.last:
        raise setup.refused(409, f"Depreciation for {label} is already posted. Open its run to see it or reverse it.",
                            href=f"/fixed-assets/runs/{label}", action="fixed_assets.action.open_run")
    if _end(month) >= today:
        last = period_label(month_index(today) - 1)
        raise setup.refused(422, f"{label} has not ended yet. Depreciation runs for a month once it has ended, so "
                                 f"the latest you can run through is {last}.",
                            fill={"field": "month", "value": last, "action": "fixed_assets.action.review_through"})
    if month < books.first:
        raise setup.unprocessable(f"{label} is before the first managed period.")
    if books.locked(month):
        raise conflict(f"{label} is locked. Run a later month; what it owes is caught up there.")
    start = books.next
    charges = await _charges(session, company_id, books, month, start)
    open_months = [k for k in range(start, month + 1) if not books.locked(k)]
    target = {k: k for k in open_months}
    items: dict[int, list[dict]] = {k: [] for k in open_months}
    held: dict[int, dict[str, dict]] = {k: {} for k in open_months}
    exceptions = {}
    for c in charges:
        for k, amount in c.postable():
            t = target.get(k, month)
            items[t].append({"asset_id": c.asset_id, "number": c.asset["number"], "name": c.asset.get("name"),
                             "month": k, "amount": amount, "expense": c.expense, "accumulated": c.accumulated, "catch_up": t != k})
        by_month: dict[int, list[int]] = {}
        for k in c.held(month):
            by_month.setdefault(target.get(k, month), []).append(k)
        for t, months in by_month.items():
            amount = c.held_amount(months)
            held[t][c.asset_id] = {"asset_id": c.asset_id, "number": c.asset["number"], "name": c.asset.get("name"),
                                   "amount": None if amount is None else money(amount, currency), "action": c.action,
                                   "reason": c.reason if c.action == "method_unavailable" else None}
        exceptions[c.asset_id] = (c.asset.get("exception"), c.exception(month, user_id))
    months, total = [], Decimal(0)
    for k in open_months if charges else []:
        lines = _lines(items[k])
        month_total = sum((it["amount"] for it in items[k]), Decimal(0))
        total += month_total
        months.append({"month": period_label(k), "total": money(month_total, currency),
                       "lines": operations._journal_view(lines, currency)["lines"], "assets": _by_asset(items[k], currency),
                       "exceptions": list(held[k].values())})
    attention = sum(1 for _, new in exceptions.values() if new)
    shown = {"empty": not charges, "months": months, "total": money(total, currency),
             "ready": len(charges) - attention, "attention": attention}
    context = {"books": books, "month": month, "months": open_months, "items": items, "held": held,
               "exceptions": exceptions, "basis": await _ledger_basis(session, company_id, books)}
    return shown, context


# ── Posting and preparing ────────────────────────────────────────────────────


def _by_asset(items: list[dict], currency: str) -> list[dict]:
    """A month's posting asset by asset, with every month it covers, caught-up months included."""
    rows: dict[str, dict] = {}
    for it in items:
        row = rows.setdefault(it["asset_id"], {"asset_id": it["asset_id"], "number": it["number"], "name": it["name"],
                                               "months": [], "amount": Decimal(0)})
        row["months"].append(period_label(it["month"]))
        row["amount"] += it["amount"]
    return [{**row, "months": sorted(row["months"]), "amount": money(row["amount"], currency)} for row in rows.values()]


def _record(k: int, status: str, items: list[dict], held: dict, currency: str, **extra) -> dict:
    total = sum((it["amount"] for it in items), Decimal(0))
    return {"month": period_label(k), "status": status, "total": money(total, currency),
            "assets": _by_asset(items, currency), "exceptions": list(held.values()), **extra}


async def _post(session, company_id, user_id, context: dict, op_key: str, *, manual: bool) -> dict:
    books: Books = context["books"]
    currency = books.currency
    if not context["items"] or not context["exceptions"]:
        raise setup.unprocessable("Nothing to depreciate yet. Add or register an asset first.")
    runs = []
    for k in context["months"]:
        label, items, held = period_label(k), context["items"][k], context["held"][k]
        cycle = books.next_cycle(k)
        key = f"run:{label}:{cycle}"
        lines = _lines(items)
        je_id = f"je:mod:{MODULE}:{key}" if lines else None
        moved = []
        for it in items:
            moved.append(await _move(
                session, company_id, user_id, it["asset_id"], kind="posted", operation="run",
                effective=_end(it["month"]).isoformat(), posting=_end(k).isoformat(),
                amounts={"accumulated_depreciation": it["amount"]}, op_key=op_key,
                contributions=[{"journal_id": je_id, "line_index": it["line_index"], "amount": str(it["amount"])}],
                catch_up=it["catch_up"], run=label))
        record_id = f"fa:run:{label}"
        record = _record(k, "posted_with_exceptions" if held else "posted", items, held, currency, cycle=cycle,
                         journal_ids=[je_id] if je_id else [], movements=moved, op_key=op_key)
        await _emit(session, company_id, user_id, record_id, "fa_run", "fa.run.posted", record,
                    key=f"{record_id}:{cycle}:posted")
        if lines:
            await operations._post(session, company_id, user_id, key=key, operation="run", day=_end(k).isoformat(),
                                   memo=f"Depreciation {label}", lines=lines, source_id=record_id,
                                   href=f"/fixed-assets/runs/{label}", label=f"Depreciation {label}",
                                   op_key=op_key, intent={"run": label, "cycle": cycle})
        runs.append({"month": label, "journal_id": je_id, "total": record["total"]})
    for asset_id, (before, after) in context["exceptions"].items():
        if before != after:
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.exception", {"exception": after})
    await notices.clear(session, company_id, notices.READY)
    if manual and books.fa.get("auto_post_paused"):
        await _set(session, company_id, auto_post_paused=False)
        await notices.clear(session, company_id, notices.PAUSED)
    await _tell_attention(session, company_id)
    return {"runs": runs}


async def _prepare(session, company_id, context: dict) -> None:
    books: Books = context["books"]
    for k in context["months"]:
        record = _record(k, "prepared", context["items"][k], context["held"][k], books.currency)
        current = books.records.get(k)
        if current is None or any(current.get(f) != record[f] for f in ("status", "total", "assets", "exceptions")):
            await _emit(session, company_id, None, f"fa:run:{record['month']}", "fa_run", "fa.run.prepared", record)
    await notices.tell(session, company_id, notices.READY,
                       f"Depreciation for {_long(context['month'])} is ready to post.",
                       month=period_label(context["month"]))


async def _tell_attention(session, company_id) -> None:
    held = [s for _, s in await _entities(session, company_id, "fa_asset") if s.get("exception")
            and s.get("status") == "active"]
    if not held:
        await notices.clear(session, company_id, notices.ATTENTION)
        return
    await notices.tell(session, company_id, notices.ATTENTION,
                       f"{len(held)} assets are held out of depreciation until what holds them is cleared.",
                       count=len(held))


async def _set(session, company_id, **changes) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(session, company_id)
    fa = {**setup.settings_of(company.settings), **changes}
    company.settings = {**(company.settings or {}), setup.SETTINGS_KEY: fa}
    await session.flush()


async def run(session, company_id, user_id, body: dict, *, today: date | None = None) -> dict:
    """Preview or post the depreciation run through ``body["month"]``."""
    month = _month(body.get("month"))

    async def plan(fa):
        return await plan_run(session, company_id, month, today or await company_today(session, company_id), user_id)

    async def apply(context, op_key):
        return await _post(session, company_id, user_id, context, op_key, manual=True), operations._effects()

    return await operations._run(session, company_id, user_id, body, op_type="run", keys=("manage_accounting",),
                                 plan=plan, apply=apply)


# ── Reversing the latest run ─────────────────────────────────────────────────


async def reverse(session, company_id, user_id, label: str, body: dict) -> dict:
    """Reverse the latest posted run while its month is open and nothing changed since."""
    from celerp.services.module_journals import reverse_journal

    month = _month(label)

    async def plan(fa):
        books = await _books(session, company_id)
        record = books.records.get(month)
        if books.last != month or record is None or record.get("status") not in _POSTED:
            raise conflict(f"Only the latest posted run can be reversed. Use Correct to change {label}.")
        if books.locked(month):
            raise conflict(f"{label} is locked. Use Correct to change its depreciation.")
        last: dict[str, int] = {}
        for movement_id in record.get("movements") or []:
            mv = await _state(session, company_id, movement_id)
            last[mv["asset_id"]] = max(last.get(mv["asset_id"], 0), int(movement_id.rsplit(":", 1)[1]))
        changed = [(asset_id, asset, index) for asset_id, index in last.items()
                   if int((asset := await _state(session, company_id, asset_id)).get("movement_count") or 0) > index]
        if changed:
            raise await operations.changed_since(session, company_id, changed, ("since this run",) * 2,
                                                 "to change its depreciation",
                                                 lead=f"The {label} run can no longer be reversed.")
        journals = [await _state(session, company_id, j) for j in record.get("journal_ids") or []]
        shown = {"month": label, "journals": [operations._reversed_lines(je, books.currency) for je in journals if je]}
        return shown, {"record": record}

    async def apply(context, op_key):
        record = context["record"]
        for je_id in record.get("journal_ids") or []:
            await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                                  key=je_id.removeprefix(f"je:mod:{MODULE}:"), operation="run",
                                  reason=f"Depreciation {label} reversed")
        for movement_id in record.get("movements") or []:
            mv = await _state(session, company_id, movement_id)
            await _move(session, company_id, user_id, mv["asset_id"], kind="reversal", operation="reverse_run",
                        effective=mv["effective_date"], posting=mv["posting_date"],
                        amounts={k: -_dec(v) for k, v in (mv.get("amounts") or {}).items()}, op_key=op_key,
                        contributions=[{**c, "amount": str(-_dec(c.get("amount")))}
                                       for c in mv.get("contributions") or []], reverses=movement_id)
        await _emit(session, company_id, user_id, f"fa:run:{label}", "fa_run", "fa.run.reversed", {"op_key": op_key})
        return {"month": label, "status": "reversed"}, operations._effects()

    return await operations._run(session, company_id, user_id, body, op_type="reverse_run",
                                 keys=("manage_accounting",), plan=plan, apply=apply)


# ── Readings and skipped months ──────────────────────────────────────────────


async def _open_asset(session, company_id, user_id, asset_id: str, keys: tuple[str, ...]) -> tuple[dict, Books]:
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, keys)
    await setup.require_managed(session, company_id)
    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    if row.state.get("status") != "active":
        raise conflict("Only an asset in use takes readings or skipped months.")
    return row.state, await _books(session, company_id)


def _still_open(books: Books, asset: dict, month: int) -> None:
    held = (asset.get("exception") or {}).get("months") or []
    if books.last is not None and month <= books.last and period_label(month) not in held:
        raise conflict(f"Depreciation for {period_label(month)} is already posted. Use Correct to change it.")


async def record_reading(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """The units an asset produced in a month, for a class that depreciates by units."""
    asset, books = await _open_asset(session, company_id, user_id, asset_id, ("manage_fixed_assets",))
    cls = await setup.get_class(session, company_id, asset.get("class_id"))
    if not rules.method(cls["method"]).uses_readings:
        raise setup.unprocessable("This asset's class does not depreciate by units, so it takes no readings.")
    month = _month(body.get("period"), "The period")
    units = operations._amount(body.get("units"), "Units")
    _still_open(books, asset, month)
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.reading.recorded",
                {"period": period_label(month), "units": str(units)})
    return {"asset_id": asset_id, "period": period_label(month), "units": str(units)}


async def skip_month(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Hold an asset out of a month's run, with the reason."""
    asset, books = await _open_asset(session, company_id, user_id, asset_id, ("manage_accounting",))
    month = _month(body.get("month"))
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise setup.unprocessable("A reason is required to skip a month.")
    _still_open(books, asset, month)
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.skip.recorded",
                {"month": period_label(month), "reason": reason})
    return {"asset_id": asset_id, "month": period_label(month), "reason": reason}


async def withdraw_skip(session, company_id, user_id, asset_id: str, label: str) -> dict:
    asset, _ = await _open_asset(session, company_id, user_id, asset_id, ("manage_accounting",))
    month = period_label(_month(label))
    if month not in (asset.get("skips") or {}):
        raise HTTPException(status_code=404, detail="This month is not skipped.")
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.skip.withdrawn", {"month": month})
    return {"asset_id": asset_id, "month": month}


# ── Lists ────────────────────────────────────────────────────────────────────


async def records(session, company_id) -> list[dict]:
    rows = [s for _, s in await _entities(session, company_id, "fa_run")]
    return sorted(rows, key=lambda r: r["month"], reverse=True)


async def attention(session, company_id) -> list[dict]:
    rows = await _entities(session, company_id, "fa_asset")
    held = [{"asset_id": eid, "number": s["number"], "name": s.get("name"), "exception": s["exception"]}
            for eid, s in rows if s.get("exception") and s.get("status") == "active"]
    return sorted(held, key=lambda a: a["number"])


# ── The monthly loop ─────────────────────────────────────────────────────────


async def _restore(session, company_id, books: Books) -> bool:
    """A run journal that committed while its run record was lost: complete the record
    from the journal, so the month reads as posted and is never posted again."""
    restored = False
    for k in sorted(books.journals):
        found = books.latest_posted(k)
        record = books.records.get(k) or {}
        if found is None or (record.get("status") in _POSTED and record.get("cycle") == found[0]):
            continue
        cycle, je = found
        moves = sorted((eid, s) for eid, s in await _entities(session, company_id, "fa_movement")
                       if any(c.get("journal_id") == je["id"] for c in s.get("contributions") or []))
        assets = dict(await _entities(session, company_id, "fa_asset"))
        items = [{"asset_id": s["asset_id"], "number": assets[s["asset_id"]]["number"],
                  "name": assets[s["asset_id"]].get("name"), "month": month_index(date.fromisoformat(s["effective_date"])),
                  "amount": _dec(s["amounts"]["accumulated_depreciation"])} for _, s in moves]
        await _emit(session, company_id, None, f"fa:run:{period_label(k)}", "fa_run", "fa.run.posted", _record(
            k, "posted", items, {}, books.currency, cycle=cycle, journal_ids=[je["id"]],
            movements=[eid for eid, _ in moves]))
        restored = True
    return restored


async def _may_post(session, company_id, settings: dict, user_id) -> bool:
    from celerp.services.permissions import read_authority, role_has_permission

    try:
        role, _ = await read_authority(session, company_id, uuid.UUID(str(user_id)))
    except ValueError:
        return False
    return role_has_permission(settings, role, "manage_accounting")


async def tick(session, company_id, today: date | None = None) -> str | None:
    """Last month's run for one company, once its run day has come in the company's
    timezone (``today`` when given): prepared, or posted as the user who chose automatic
    posting. The caller commits."""
    from celerp.modules.registry import uses_module
    from celerp.services.company_lock import lock_company

    await lock_company(session, company_id)
    books = await _books_if_started(session, company_id)
    if books is None:
        return None
    fa = books.fa
    today = today or business_today(books.settings.get("timezone"))
    if not uses_module(books.settings, MODULE):
        if fa.get("run_mode") == "post" and not fa.get("auto_post_paused"):
            await _set(session, company_id, auto_post_paused=True)
        return "off"
    from celerp_fixed_assets import remeasure, upkeep

    await remeasure.tell_reviews(session, company_id, today)
    await upkeep.tell_reminders(session, company_id, today)
    if fa.get("run_mode", "prepare") == "manual" or today.day < int(fa.get("run_day") or 1):
        return None
    month = month_index(today) - 1
    if await _restore(session, company_id, books):
        books = await _books(session, company_id)
    if (books.last is not None and month <= books.last) or month < books.first or books.locked(month):
        return None
    user = fa.get("auto_post_user")
    may_post = bool(user) and await _may_post(session, company_id, books.settings, user)
    shown, context = await plan_run(session, company_id, month, today, user if may_post else None)
    if shown["empty"]:
        return None
    if fa.get("run_mode") == "post" and not fa.get("auto_post_paused") and may_post:
        await _post(session, company_id, uuid.UUID(user), context, f"auto.{period_label(month)}", manual=False)
        await notices.tell(session, company_id, notices.AUTO_POSTED,
                           f"Depreciation for {_long(month)} was posted automatically.",
                           action_url=f"{notices.RUNS_URL}/{period_label(month)}", month=period_label(month))
        return "posted"
    await _prepare(session, company_id, context)
    if fa.get("run_mode") == "post" and fa.get("auto_post_paused"):
        await notices.tell(session, company_id, notices.PAUSED,
                           "Automatic posting is paused because fixed assets was turned off. "
                           "Post this run by hand to resume it.")
    elif fa.get("run_mode") == "post":
        await notices.tell(session, company_id, notices.NO_USER,
                           "The user who chose automatic posting can no longer post journals. "
                           "Choose another user by setting posting to automatic again.")
    return "prepared"


async def _books_if_started(session, company_id) -> Books | None:
    """The books once fixed assets is started and its first managed period confirmed."""
    fa = setup.settings_of(await setup.company_settings(session, company_id))
    if not fa.get("started") or not fa.get("first_managed_period"):
        return None
    return await _books(session, company_id)


async def loop() -> None:
    """Background loop: every hour, each company's run (tick) for last month."""
    from celerp.db import get_session_ctx
    from celerp.models.company import Company

    while True:
        try:
            async with get_session_ctx() as session:
                companies = (await session.execute(select(Company.id).where(Company.is_active.is_(True)))).scalars().all()
                await session.commit()
                for company_id in companies:
                    try:
                        await tick(session, company_id)
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        log.error("fixed assets: run failed for %s: %s", company_id, exc)
        except Exception as exc:
            log.error("fixed assets: loop error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
