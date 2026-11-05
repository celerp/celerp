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
    "released_bills": {}, "resume_restarts": False,
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
        if self.frequency not in FREQUENCIES:
            raise ScheduleError("subscriptions.err_schedule_frequency")
        if self.frequency == "custom" and not (
                type(self.custom_days) is int and 0 < self.custom_days <= 36500):
            raise ScheduleError("subscriptions.err_custom_interval")
        if self.timing not in TIMINGS:
            raise ScheduleError("subscriptions.err_schedule_timing")
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
        ScheduleError saying why when they describe none."""
        return cls(
            anchor=_date(state["anchor_date"]),
            frequency=shown("frequency", state.get("frequency")),
            custom_days=state.get("custom_interval_days"),
            timing=shown("billing_timing", state.get("billing_timing")),
            term_start=_opt_date(state.get("term_start")),
            term_end=_opt_date(state.get("term_end")),
            last=state.get("cancel_at_period_end"),
            periods={int(k): v for k, v in (state.get("periods") or {}).items()},
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

    def due(self, today: date, *, floor: int | None = None, limit: int = 500) -> list[int]:
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

    def latest_due(self, today: date) -> int | None:
        """The newest existing period whose bill date has come."""
        first = self.first()
        if first is None or self.bill_date(first) > today:
            return None
        k = self.current(today) - (1 if self.timing == "arrears" else 0)
        if self.term_end is not None:
            end = self.current(self.term_end)
            k = min(k, end - (1 if self.start(end) == self.term_end else 0))
        if self.last is not None:
            k = min(k, self.last)
        return k if k >= first and self.exists(k) else None

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
