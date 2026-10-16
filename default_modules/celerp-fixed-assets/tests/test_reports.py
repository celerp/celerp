# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed asset reports: the schedule rolled forward per class and asset, the register,
depreciation by period, and disposals, each with its print and CSV.

Every scenario starts from a started demo company managed from 1 January 2026. A
contributed car (12,000 over 60 months) charges 200 a month from January.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_disposal import _dispose
from test_opening import _opening
from test_runs import _contributed, _managed_from, _run

pytestmark = pytest.mark.asyncio

D = Decimal


async def _schedule(fa: FA, date_from: str = "2026-01-01", date_to: str = "2026-03-31") -> dict:
    return await fa.ok(await fa.get("/reports/schedule", date_from=date_from, date_to=date_to))


def _row(rows: list[dict], name: str) -> dict:
    (found,) = [r for r in rows if r["name"] == name]
    return found


async def test_schedule_rolls_forward_per_class_and_asset(fa):
    await _managed_from(fa)
    await _opening(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    got = await _schedule(fa)
    vehicles = _row(got["classes"], "Vehicles")
    assert D(vehicles["cost_opening"]) == 0 and D(vehicles["additions"]) == D("12000")
    assert D(vehicles["cost_closing"]) == D("12000")
    assert D(vehicles["depreciation_charge"]) == D("400") and D(vehicles["depreciation_closing"]) == D("400")
    assert D(vehicles["impairment_closing"]) == 0
    assert D(vehicles["planned_not_posted"]) == D("200")
    assert D(vehicles["nbv_closing"]) == D("11600")
    (row,) = [a for a in vehicles["assets"] if a["id"] == car]
    assert D(row["nbv_closing"]) == D("11600") and D(row["planned_not_posted"]) == D("200")
    buildings = _row(got["classes"], "Buildings")
    assert D(buildings["cost_opening"]) == D("250000") and D(buildings["additions"]) == 0
    assert D(buildings["depreciation_opening"]) == D("40000")
    total = got["totals"]
    assert D(total["cost_closing"]) == D("262000")
    assert D(total["nbv_closing"]) == D(total["cost_closing"]) - D(total["depreciation_closing"]) \
        - D(total["impairment_closing"])


async def test_schedule_disposal_column_and_empty_period(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    await _dispose(fa, car, "2026-03-20")
    vehicles = _row((await _schedule(fa))["classes"], "Vehicles")
    assert D(vehicles["additions"]) == D("12000") and D(vehicles["disposals"]) == D("-12000")
    assert D(vehicles["cost_closing"]) == 0 and D(vehicles["depreciation_closing"]) == 0
    assert D(vehicles["planned_not_posted"]) == 0
    empty = await _schedule(fa, "2027-01-01", "2027-01-31")
    assert all(not c["assets"] or all(D(a["cost_closing"]) == 0 for a in c["assets"]) for c in empty["classes"])


async def test_schedule_refuses_before_cutover_and_reversed_range(fa):
    await _managed_from(fa)
    r = await fa.get("/reports/schedule", date_from="2025-12-31", date_to="2026-03-31")
    assert r.status_code == 422 and "first managed period" in r.text
    r = await fa.get("/reports/schedule", date_from="2026-03-01", date_to="2026-01-31")
    assert r.status_code == 422


async def test_depreciation_by_period_posted_skipped_forecast(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-02")
    got = await fa.ok(await fa.get("/reports/depreciation", date_from="2026-01-01", date_to="2026-04-30"))
    months = {m["month"]: m for m in got["months"]}
    assert D(months["2026-01"]["posted"]) == D("200") and D(months["2026-02"]["posted"]) == D("200")
    assert D(months["2026-03"]["posted"]) == 0 and D(months["2026-03"]["forecast"]) == D("200")
    assert list(months) == sorted(months, reverse=True)
    (row,) = [a for a in months["2026-01"]["assets"] if a["id"] == car]
    assert D(row["posted"]) == D("200")


async def test_disposals_report_figures_and_journal(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    await _dispose(fa, car, "2026-05-20")
    got = await fa.ok(await fa.get("/reports/disposals", date_from="2026-01-01", date_to="2026-12-31"))
    (row,) = got["items"]
    assert row["asset_id"] == car and row["date"] == "2026-05-20"
    assert D(row["carrying_amount"]) == D("11200") and D(row["gain_or_loss"]) == D("-11200")
    assert D(row["proceeds"]) == 0 and row["journal_id"].startswith("je:mod:celerp-fixed-assets:asset:")
    assert (await fa.ok(await fa.get("/reports/disposals", date_from="2027-01-01",
                                     date_to="2027-12-31")))["items"] == []


async def test_reports_need_financial_report_permission(fa):
    await _managed_from(fa)
    viewer = await fa.role_headers("viewer")
    for path in ("/reports/schedule", "/reports/depreciation", "/reports/disposals"):
        r = await fa.client.get(f"/fixed-assets/api{path}", headers=viewer,
                                params={"date_from": "2026-01-01", "date_to": "2026-03-31"})
        assert r.status_code == 403, (path, r.text)
