# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Period arithmetic for an agreement schedule.

Period k starts on b(k), the anchor plus k intervals, always counted from the anchor
(a 31 January anchor gives 29 February, then 31 March), and ends the day before
b(k + 1). Advance billing bills period k on b(k); arrears bills it on b(k + 1). A period
exists when k is at or above the lowest billable index (the lowest claimed index, or 0),
it starts on or after the term start and before the term end, and it is not past the
period an end-of-period cancel stopped at. Pure: no database, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

FREQUENCIES = frozenset({"weekly", "biweekly", "monthly", "quarterly", "annually", "custom"})
TIMINGS = frozenset({"advance", "arrears"})
AUTO_MODES = frozenset({"auto_invoice"})
GENERATION_MODES = frozenset({"manual_due"}) | AUTO_MODES
MAX_INTERVAL_DAYS = 36500
# What an agreement is to the business, which picks the page it is listed on: Subscriptions,
# Retainers or Members. Set by the page that makes it; an agreement without one is a subscription.
KINDS = frozenset({"subscription", "retainer", "membership"})
MAX_GRACE_DAYS = 365
CATCHUP_CAP = 12
DUE_LIMIT = 500  # the most open periods one request walks through
_MONTHS = {"monthly": 1, "quarterly": 3, "annually": 12}
_DAYS = {"weekly": 7, "biweekly": 14}

# Period states written by the agreement's own schedule operations. A released period
# was billed or skipped and handed back: it is unclaimed again and keeps its entry, so
# the lowest index and the release count stay.
BILLED, SKIPPED, UNRESOLVED, RELEASED = "billed", "skipped", "unresolved", "released"
CLAIMED_STATES = frozenset({BILLED, SKIPPED})


def _days_in_month(year: int, month: int) -> int:
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def add_months(d: date, months: int) -> date:
    """``d`` moved by ``months``, its day clamped to the target month's last day."""
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, min(d.day, _days_in_month(year, month)))


# The agreement's stored settings a schedule is read from, and nothing else of it but its periods.
SCHEDULE_FIELDS = ("anchor_date", "frequency", "custom_interval_days", "billing_timing", "term_start", "term_end",
                   "cancel_at_period_end")

# What each field an agreement event records holds while the agreement does not store it, as
# its page and History read it: an agreement made through Documents, an import or an earlier
# release stores few of them. Every field a writer sets is named here.
UNSET = {
    "anchor_date": None, "start_date": None, "frequency": "monthly", "custom_interval_days": None,
    "billing_timing": "advance", "term_start": None, "term_end": None, "cancel_at_period_end": None,
    "generation_mode": "manual_due", "auto_send": False, "catchup_periods": 0, "status": None,
    "periods": {}, "consents": {}, "history_review": [], "history_set_aside": [], "billing_failure": None,
    "released_bills": {}, "resume_restarts": False, "recognize_over_service_period": False,
    "kind": "subscription", "beneficiary_contact_id": None, "grace_days": 0,
}


def shown(field: str, value):
    """``value`` of an agreement's ``field`` as the agreement reads it: unset (None or an empty
    text) reads as its UNSET value."""
    return UNSET.get(field) if value is None or value == "" else value


class ScheduleError(ValueError):
    """Why settings describe no schedule, as a catalog key and its params, so whoever refuses
    on it words it in the reader's language."""

    def __init__(self, key: str, **params) -> None:
        super().__init__(key)
        self.key, self.params = key, params


@dataclass(frozen=True)
class Term:
    """What a stored schedule term must hold: a date (ISO text), one of ``choices``, a whole
    number from ``low`` to ``high`` (an open end is unbounded), a text or a flag; ``key`` says
    why when it does not."""
    kind: str
    key: str
    choices: frozenset = frozenset()
    low: int | None = None
    high: int | None = None

    def holds(self, value) -> bool:
        if self.kind == "date":
            try:
                date.fromisoformat(value)
            except (TypeError, ValueError):
                return False
            return True
        if self.kind == "choice":
            return isinstance(value, str) and value in self.choices
        if self.kind == "whole":
            return type(value) is int and (self.low is None or value >= self.low) \
                and (self.high is None or value <= self.high)
        if self.kind == "text":
            return isinstance(value, str) and bool(value.strip())
        return type(value) is bool


# Every term an agreement's schedule and its unattended billing read off its stored state, and
# what each must hold. Unset (None or empty text) reads as its UNSET value. The schedule builder
# checks them all (check_terms), so every writer and reader holds the same rule.
TERMS = {
    "anchor_date": Term("date", "subscriptions.err_schedule_date"),
    "frequency": Term("choice", "subscriptions.err_schedule_frequency", choices=FREQUENCIES),
    "custom_interval_days": Term("whole", "subscriptions.err_custom_interval", low=1, high=MAX_INTERVAL_DAYS),
    "billing_timing": Term("choice", "subscriptions.err_schedule_timing", choices=TIMINGS),
    "term_start": Term("date", "subscriptions.err_schedule_date"),
    "term_end": Term("date", "subscriptions.err_schedule_date"),
    "cancel_at_period_end": Term("whole", "subscriptions.err_cancel_period"),
    "catchup_periods": Term("whole", "subscriptions.err_catchup_window", low=0, high=CATCHUP_CAP),
    "generation_mode": Term("choice", "subscriptions.err_generation_mode", choices=GENERATION_MODES),
    "auto_send": Term("flag", "subscriptions.err_auto_send"),
    # Read by the books when a bill posts (celerp.services.recognition.deferred_line), not by
    # the schedule, so changing it asks no consent (billing.CONSENT_TERMS).
    "recognize_over_service_period": Term("flag", "recognition.err_flag_invalid"),
    # Read by the pages and the member standing (routes.member_rows), not by the schedule, so
    # changing them asks no consent either.
    "kind": Term("choice", "subscriptions.err_kind", choices=KINDS),
    "beneficiary_contact_id": Term("text", "subscriptions.err_beneficiary"),
    "grace_days": Term("whole", "subscriptions.err_grace_days", low=0, high=MAX_GRACE_DAYS),
}


def term_error(name: str, value) -> ScheduleError | None:
    """The ScheduleError naming term ``name`` when ``value`` does not hold what TERMS declares."""
    term = TERMS[name]
    return None if term.holds(value) else ScheduleError(term.key, **({"day": value} if term.kind == "date" else {}))


def check_terms(state: dict) -> None:
    """ScheduleError naming the first stored term that does not hold what TERMS declares. Every
    set term is checked; the custom interval is also required for a custom frequency."""
    for name in TERMS:
        value = state.get(name)
        required = name == "custom_interval_days" and shown("frequency", state.get("frequency")) == "custom"
        if (value is None or value == "") and not required:
            continue
        if exc := term_error(name, value):
            raise exc
    if state.get("term_start") and state.get("term_end") and state["term_start"] >= state["term_end"]:
        raise ScheduleError("subscriptions.err_term_order")


def _date(value) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("subscriptions.err_schedule_date", day=value) from exc


def _opt_date(value) -> date | None:
    return _date(value) if value else None


@dataclass(frozen=True)
class Schedule:
    anchor: date
    frequency: str
    custom_days: int | None = None
    timing: str = "advance"
    term_start: date | None = None
    term_end: date | None = None
    last: int | None = None  # the final period an end-of-period cancel keeps
    periods: dict[int, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.term_end is not None and self.term_end <= self.anchor:
            raise ScheduleError("subscriptions.err_term_before_first_period")
        try:
            self.start(1)
            self.start(2)
        except (ValueError, OverflowError) as exc:
            raise ScheduleError("subscriptions.err_interval_calendar") from exc

    @classmethod
    def from_state(cls, state: dict) -> Schedule:
        """The schedule an agreement's stored state describes (its SCHEDULE_FIELDS and periods);
        ScheduleError saying why when its terms (TERMS) describe none. Periods that are not a
        mapping of indexes to records fail as they are read."""
        check_terms(state)
        periods = {int(k): v for k, v in (state.get("periods") or {}).items()}
        if not all(isinstance(v, dict) for v in periods.values()):
            raise TypeError("a recorded period is not a record")
        return cls(
            anchor=_date(state["anchor_date"]),
            frequency=shown("frequency", state.get("frequency")),
            custom_days=state.get("custom_interval_days"),
            timing=shown("billing_timing", state.get("billing_timing")),
            term_start=_opt_date(state.get("term_start")),
            term_end=_opt_date(state.get("term_end")),
            last=state.get("cancel_at_period_end"),
            periods=periods,
        )

    def start(self, k: int) -> date:
        if self.frequency in _MONTHS:
            return add_months(self.anchor, k * _MONTHS[self.frequency])
        return self.anchor + timedelta(days=k * (_DAYS.get(self.frequency) or self.custom_days))

    def end(self, k: int) -> date:
        return self.start(k + 1) - timedelta(days=1)

    def bill_date(self, k: int) -> date:
        return self.start(k + 1) if self.timing == "arrears" else self.start(k)

    def service_end(self, k: int) -> date:
        """Last covered day, bounded by the exclusive term end; no implicit proration."""
        end = self.end(k)
        return min(end, self.term_end - timedelta(days=1)) if self.term_end else end

    @property
    def lowest(self) -> int:
        return min((0, *self.periods))

    def exists(self, k: int) -> bool:
        if k < self.lowest or (self.last is not None and k > self.last):
            return False
        try:
            start = self.start(k)
        except (ValueError, OverflowError):
            return False
        return ((self.term_start is None or start >= self.term_start)
                and (self.term_end is None or start < self.term_end))

    def state(self, k: int) -> str | None:
        return (self.periods.get(k) or {}).get("state")

    def is_open(self, k: int) -> bool:
        """Never claimed, or released: a billing target once it is due."""
        return self.state(k) in (None, RELEASED)

    def first(self) -> int | None:
        """The lowest existing period, or None when none exists."""
        k = self.lowest
        if self.term_start is not None:
            k = max(k, self.current(self.term_start))
            if self.start(k) < self.term_start:
                k += 1
        return k if self.exists(k) else None

    def oldest_open(self, *, floor: int | None = None) -> int | None:
        """The oldest existing period still open at or above ``floor``, or None when every
        existing period is claimed or waits for history review."""
        k = self.first()
        if k is None:
            return None
        k = max(k, floor) if floor is not None else k
        while self.exists(k):
            if self.is_open(k):
                return k
            k += 1
        return None

    def next_bill_date(self, *, floor: int | None = None) -> date | None:
        k = self.oldest_open(floor=floor)
        return None if k is None else self.bill_date(k)

    def due(self, today: date, *, floor: int | None = None, limit: int = DUE_LIMIT) -> list[int]:
        """Every open existing period whose bill date is on or before ``today``, oldest first."""
        k = self.first()
        if k is None:
            return []
        k = max(k, floor) if floor is not None else k
        out = []
        while self.exists(k) and self.bill_date(k) <= today and len(out) < limit:
            if self.is_open(k):
                out.append(k)
            k += 1
        return out

    def started_open(self, today: date, *, floor: int | None = None) -> list[int]:
        """Every open existing period at or above ``floor`` that started on or before ``today``,
        oldest first; ScheduleError counting them when there are more than DUE_LIMIT, so no
        request walks an unbounded run of periods."""
        k = self.oldest_open(floor=floor)
        if k is None:
            return []
        last = self._last_existing(self.current(today))
        count = last - k + 1 - sum(1 for c in self.periods if k <= c <= last and not self.is_open(c))
        if count > DUE_LIMIT:
            raise ScheduleError("subscriptions.err_too_many_started", count=count, limit=DUE_LIMIT)
        return [c for c in range(k, last + 1) if self.is_open(c)]

    def latest_due(self, today: date) -> int | None:
        """The newest existing period whose bill date has come."""
        first = self.first()
        if first is None:
            return None
        k = self._last_existing(self.current(today) - (1 if self.timing == "arrears" else 0))
        return k if k >= first and self.exists(k) else None

    def _last_existing(self, k: int) -> int:
        """``k`` lowered to the last period the term end and an end-of-period cancel leave."""
        if self.term_end is not None:
            end = self.current(self.term_end)
            k = min(k, end - (1 if self.start(end) == self.term_end else 0))
        if self.last is not None:
            k = min(k, self.last)
        return k

    def current(self, today: date) -> int:
        """The period containing today, including dates before or after the term."""
        if self.frequency in _MONTHS:
            stride = _MONTHS[self.frequency]
            months = (today.year - self.anchor.year) * 12 + today.month - self.anchor.month
            k = months // stride
            if self.start(k) > today:
                k -= 1
            elif self.start(k + 1) <= today:
                k += 1
            return k
        stride = _DAYS.get(self.frequency) or self.custom_days
        return (today - self.anchor).days // stride

    def ended(self, today: date) -> bool:
        """The term is over and no existing period is left open."""
        return self.term_end is not None and today > self.term_end and self.oldest_open() is None
