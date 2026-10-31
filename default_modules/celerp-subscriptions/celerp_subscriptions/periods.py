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


def _opt_date(value) -> date | None:
    return date.fromisoformat(value) if value else None


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
            raise ValueError(f"Unknown frequency {self.frequency!r}")
        if self.frequency == "custom" and not (isinstance(self.custom_days, int) and self.custom_days > 0):
            raise ValueError("A custom frequency needs its interval in days")
        if self.timing not in TIMINGS:
            raise ValueError(f"Unknown billing timing {self.timing!r}")

    @classmethod
    def from_state(cls, state: dict) -> Schedule:
        """The schedule an agreement's stored state describes."""
        return cls(
            anchor=date.fromisoformat(state["anchor_date"]),
            frequency=state.get("frequency") or "monthly",
            custom_days=state.get("custom_interval_days"),
            timing=state.get("billing_timing") or "advance",
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

    @property
    def lowest(self) -> int:
        return min(self.periods) if self.periods else 0

    def exists(self, k: int) -> bool:
        if k < self.lowest or (self.last is not None and k > self.last):
            return False
        start = self.start(k)
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
        while self.term_start is not None and self.start(k) < self.term_start:
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

    def due(self, today: date, *, floor: int | None = None) -> list[int]:
        """Every open existing period whose bill date is on or before ``today``, oldest first."""
        k = self.first()
        if k is None:
            return []
        k = max(k, floor) if floor is not None else k
        out = []
        while self.exists(k) and self.bill_date(k) <= today:
            if self.is_open(k):
                out.append(k)
            k += 1
        return out

    def latest_due(self, today: date) -> int | None:
        """The newest existing period whose bill date has come."""
        k = self.first()
        if k is None or self.bill_date(k) > today:
            return None
        while self.exists(k + 1) and self.bill_date(k + 1) <= today:
            k += 1
        return k

    def current(self, today: date) -> int:
        """The period ``today`` falls in (may be before the lowest index or past the term)."""
        k = 0
        while self.start(k) > today:
            k -= 1
        while self.start(k + 1) <= today:
            k += 1
        return k

    def ended(self, today: date) -> bool:
        """The term is over and no existing period is left open."""
        return self.term_end is not None and today > self.term_end and self.oldest_open() is None
