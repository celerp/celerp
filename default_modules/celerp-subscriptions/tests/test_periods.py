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
