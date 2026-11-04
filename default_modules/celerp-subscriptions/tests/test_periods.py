# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Period arithmetic: every bound is counted from the anchor, never from the last bill."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agreement_kit import agreement, freeze_instant, generate_due, register


def _s(anchor: date, frequency: str = "monthly", **kw):
    from celerp_subscriptions.periods import Schedule

    return Schedule(anchor=anchor, frequency=frequency, **kw)


def test_period_bounds_month_end_anchor_never_drifts():
    s = _s(date(2028, 1, 31))
    assert [s.start(k) for k in range(5)] == [
        date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31), date(2028, 4, 30), date(2028, 5, 31)]
    assert [s.bill_date(k) for k in range(3)] == [date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)]
    assert s.end(1) == date(2028, 3, 30)  # the day before the next period starts
    assert s.start(-1) == date(2027, 12, 31) and s.start(-2) == date(2027, 11, 30)


def test_period_bounds_weekly_biweekly_quarterly_annual_custom():
    assert _s(date(2026, 1, 5), "weekly").start(2) == date(2026, 1, 19)
    assert _s(date(2026, 1, 5), "biweekly").start(2) == date(2026, 2, 2)
    q = _s(date(2025, 11, 30), "quarterly")
    assert [q.start(k) for k in range(3)] == [date(2025, 11, 30), date(2026, 2, 28), date(2026, 5, 30)]
    a = _s(date(2028, 2, 29), "annually")
    assert [a.start(k) for k in (1, 2, 3, 4)] == [date(2029, 2, 28), date(2030, 2, 28), date(2031, 2, 28),
                                                 date(2032, 2, 29)]
    c = _s(date(2026, 1, 1), "custom", custom_days=10)
    assert c.start(3) == date(2026, 1, 31) and c.end(0) == date(2026, 1, 10)
    arrears = _s(date(2026, 1, 1), timing="arrears")
    assert arrears.bill_date(0) == date(2026, 2, 1)
    with pytest.raises(ValueError):
        _s(date(2026, 1, 1), "custom")


@pytest.mark.asyncio
async def test_period_bounds_company_timezone_dst(client, monkeypatch):
    """New York leaves daylight time at 06:00 UTC on 1 Nov 2026. At 03:30 UTC it is still
    31 Oct there, so the 1 Nov bill is not due; at 05:30 UTC (01:30 local) it is."""
    h = await register(client)
    assert (await client.patch("/companies/me", headers=h,
                               json={"settings": {"timezone": "America/New_York"}})).status_code == 200
    freeze_instant(monkeypatch, datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc))
    eid = await agreement(client, h, start="2026-10-01")
    assert (await generate_due(client, h, eid)).json()["period_index"] == 0
    early = await generate_due(client, h, eid)
    assert early.status_code == 200 and early.json()["period_index"] == 0  # the due period, already billed
    freeze_instant(monkeypatch, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    r = await generate_due(client, h, eid)
    assert r.status_code == 200 and r.json()["period_index"] == 1

def test_large_period_offsets_and_terminated_due_date_are_bounded():
    daily = _s(date(1900, 1, 1), "custom", custom_days=1)
    assert daily.current(date(2026, 7, 1)) == (date(2026, 7, 1) - date(1900, 1, 1)).days
    assert len(daily.due(date(2026, 7, 1))) == 500
    month_end = _s(date(1900, 1, 31))
    current = month_end.current(date(2026, 7, 1))
    assert month_end.start(current) <= date(2026, 7, 1) < month_end.start(current + 1)
    ended = _s(date(2025, 1, 1), term_end=date(2025, 3, 1))
    assert ended.latest_due(date(2026, 7, 1)) == 1
    assert ended.current(date(2026, 7, 1)) > ended.latest_due(date(2026, 7, 1))


def test_arrears_catchup_window_begins_at_bill_date():
    from celerp_subscriptions.billing import outside_window

    arrears = _s(date(2026, 1, 1), timing="arrears")
    today = date(2026, 2, 1)
    assert arrears.due(today) == [0]
    assert not outside_window({"catchup_periods": 0}, arrears, [0], today)
    overdue = date(2026, 3, 1)
    assert outside_window({"catchup_periods": 0}, arrears, [0, 1], overdue)
    assert not outside_window({"catchup_periods": 1}, arrears, [0, 1], overdue)

    advance = _s(date(2026, 1, 1))
    assert not outside_window({"catchup_periods": 0}, advance, [0], date(2026, 1, 1))
    assert outside_window({"catchup_periods": 0}, advance, [0, 1], date(2026, 2, 1))

    expired = _s(date(2026, 1, 1), term_end=date(2026, 2, 1))
    assert outside_window({"catchup_periods": 0}, expired, [0], date(2026, 7, 1))

def test_invalid_calendar_intervals_and_terms_are_rejected():
    with pytest.raises(ValueError):
        _s(date(2026, 1, 1), "custom", custom_days=36501)
    with pytest.raises(ValueError):
        _s(date(2026, 1, 1), "custom", custom_days=10**12)
    with pytest.raises(ValueError):
        _s(date(9999, 12, 31), "monthly")
    with pytest.raises(ValueError):
        _s(date(2026, 1, 1), term_end=date(2025, 12, 31))


def test_partial_final_period_preserves_exact_service_end_without_prorating():
    schedule = _s(date(2026, 1, 1), term_end=date(2026, 2, 15))
    assert schedule.exists(1)
    assert schedule.service_end(0) == date(2026, 1, 31)
    assert schedule.service_end(1) == date(2026, 2, 14)
    assert not schedule.exists(2)


def test_term_end_truncates_displayed_and_issued_service_end():
    from celerp_subscriptions.billing import period_row

    schedule = _s(date(2026, 1, 1), term_end=date(2026, 2, 15))
    assert schedule.service_end(0) == date(2026, 1, 31)
    assert schedule.service_end(1) == date(2026, 2, 14)
    assert period_row(schedule, 1)["service_end"] == "2026-02-14"
    assert schedule.bill_date(1) == date(2026, 2, 1)
    assert not schedule.exists(2)


def test_future_skip_does_not_hide_earlier_periods():
    anchor = date(2026, 1, 1)
    schedule = _s(anchor, periods={5: {"state": "skipped"}})
    assert schedule.lowest == 0
    assert schedule.first() == 0
    assert schedule.oldest_open() == 0
    assert schedule.due(date(2026, 2, 1)) == [0, 1]
    assert schedule.next_bill_date() == anchor

    # Imported claims before the anchor still define negative historical periods.
    historical = _s(anchor, periods={-3: {"state": "billed"}, -2: {"state": "billed"}})
    assert historical.lowest == -3
    assert historical.oldest_open() == -1
    assert historical.due(anchor) == [-1, 0]


@pytest.mark.asyncio
async def test_period_history_can_reach_oldest_open_without_unbounded_rows():
    from celerp_subscriptions.billing import period_list

    state = {"anchor_date": "2020-01-01", "frequency": "monthly", "status": "active"}
    today = date(2026, 7, 1)
    first = await period_list(None, None, state, today)
    assert len(first["rows"]) == 24
    assert first["oldest_due_row"]["period_index"] == 0
    assert first["older_before"] == first["rows"][-1]["period_index"]

    seen = [r["period_index"] for r in first["rows"]]
    cursor = first["older_before"]
    while cursor is not None:
        page = await period_list(None, None, state, today, before=cursor)
        assert len(page["rows"]) <= 24
        assert all(r["period_index"] < cursor for r in page["rows"])
        assert page["oldest_due_row"] is None
        seen.extend(r["period_index"] for r in page["rows"])
        cursor = page["older_before"]
    assert len(seen) == len(set(seen))
    assert min(seen) == 0
    assert max(seen) > 24
