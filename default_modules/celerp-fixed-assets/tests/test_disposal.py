# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Disposing of an asset that is not sold: scrapped, lost or donated.

A contributed car (12,000 over 60 months, straight line, full month, from January
2026) charges 200 a month, so the depreciation to the disposal date, the balances
removed and the loss can be read off the calendar.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _lines, _lock, _managed_from, _movements, _run

pytestmark = pytest.mark.asyncio

D = Decimal


async def _dispose(fa: FA, asset_id: str, on: str, **body) -> dict:
    return await fa.operate(f"/assets/{asset_id}/dispose", {"date": on, "reason": "scrapped", **body},
                            key=f"d-{uuid.uuid4().hex[:8]}")


async def _disposal_journal(fa: FA, asset_id: str) -> dict:
    found = [je for je in await fa.module_journals()
             if je.get("source", {}).get("entity_id") == asset_id and je["metadata_"]["operation"] == "disposal"]
    assert len(found) == 1, found
    return found[0]


async def test_dispose_posts_depreciation_to_date_and_derecognizes(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    preview = await fa.ok(await fa.post(f"/assets/{car}/dispose", {"date": "2026-05-20", "reason": "scrapped",
                                                                   "preview": True, "op_key": "dp"}))
    assert D(preview["depreciation"]) == D("200")
    assert D(preview["carrying_amount"]) == D("11200") and D(preview["gain_or_loss"]) == D("-11200")
    await _dispose(fa, car, "2026-05-20")
    je = await _disposal_journal(fa, car)
    assert je["ts"][:10] == "2026-05-20"
    assert _lines(je) == {"6400": (D("200"), D(0)), "1220": (D("800"), D("200")), "1210": (D(0), D("12000")),
                          "6980": (D("11200"), D(0))}
    got = await fa.asset(car)
    assert got["status"] == "disposed" and got["disposal"]["date"] == "2026-05-20"
    assert got["disposal"]["reason"] == "scrapped" and D(got["disposal"]["gain_or_loss"]) == D("-11200")
    assert D(got["cost"]) == 0 and D(got["accumulated_depreciation"]) == 0 and D(got["nbv"]) == 0
    await fa.fold_matches(car)
    before = len(await _movements(fa, car))
    await _run(fa, "2026-06")
    assert len(await _movements(fa, car)) == before


async def test_dispose_before_posted_runs_takes_back_their_depreciation(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-06")
    await _dispose(fa, car, "2026-04-10")
    je = await _disposal_journal(fa, car)
    assert _lines(je) == {"1220": (D("600") + D("600"), D(0)), "6400": (D(0), D("600")),
                          "1210": (D(0), D("12000")), "6980": (D("11400"), D(0))}
    moves = await _movements(fa, car)
    assert moves[-1]["periods"] == {"2026-04": "-200.00", "2026-05": "-200.00", "2026-06": "-200.00"}
    await fa.fold_matches(car)


async def test_dispose_on_a_locked_date_names_the_first_open_date(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _lock(fa, "2026-03-31")
    r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-03-15", "reason": "lost", "preview": True,
                                                 "op_key": "dl"})
    assert r.status_code == 422 and "2026-04-01" in r.json()["detail"]


async def test_dispose_validates_its_inputs(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    for body, words in (({"date": "2026-05-01", "reason": "stolen"}, "Choose why"),
                        ({"date": "not a date", "reason": "lost"}, "is a date"),
                        ({"date": "2025-12-31", "reason": "lost"}, "before"),
                        ({"date": "2026-05-01", "reason": "lost", "percent": "120"}, "percent"),
                        ({"date": "2026-05-01", "reason": "lost", "quantity": "2"}, "quantity")):
        r = await fa.post(f"/assets/{car}/dispose", {**body, "preview": True, "op_key": "v"})
        assert r.status_code == 422 and words in r.json()["detail"], (body, r.text)
    r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-05-01", "reason": "lost", "preview": True,
                                                 "op_key": "v"}, headers=await fa.role_headers("viewer"))
    assert r.status_code == 403


async def test_partial_dispose_by_percentage_splits_pro_rata_and_depreciates_on(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-06")
    await _dispose(fa, car, "2026-07-10", percent="25")
    je = await _disposal_journal(fa, car)
    # July for the kept three quarters (150), then a quarter of cost and of 1,350 out.
    assert _lines(je) == {"6400": (D("150"), D(0)), "1220": (D("337.50"), D("150")), "1210": (D(0), D("3000")),
                          "6980": (D("2662.50"), D(0))}
    got = await fa.asset(car)
    assert got["status"] == "active"
    assert D(got["cost"]) == D("9000") and D(got["accumulated_depreciation"]) == D("1012.50")
    await _run(fa, "2026-08")
    run_moves = [m for m in await _movements(fa, car) if m["operation"] == "run"]
    assert D(run_moves[-1]["amounts"]["accumulated_depreciation"]) == D("150")
    assert run_moves[-1]["effective_date"] == "2026-08-31"
    await fa.fold_matches(car)


async def test_partial_dispose_of_a_quantity_record_by_quantity(fa):
    laptops = await fa.line("Laptop")
    done = await fa.register(laptops["source"], key="reg-q", class_name="Computer equipment", mode="quantity")
    record = done["assets"][0]["id"]
    await _dispose(fa, record, laptops["date"], quantity="1", reason="donated")
    got = await fa.asset(record)
    assert got["quantity"] == 2 and D(got["cost"]) == D("2900") and got["status"] == "active"
    await fa.fold_matches(record)
    r = await fa.post(f"/assets/{record}/dispose", {"date": laptops["date"], "reason": "lost", "percent": "50",
                                                    "preview": True, "op_key": "pq"})
    assert r.status_code == 422 and "quantity" in r.json()["detail"]


async def test_bulk_dispose_and_undo_disposal(fa):
    await _managed_from(fa)
    a = await _contributed(fa, name="Car A")
    b = await _contributed(fa, name="Car B")
    done = await fa.operate("/dispose", {"asset_ids": [a, b], "date": "2026-05-20", "reason": "scrapped"},
                            key="bulk")
    assert len(done["journals"]) == 2
    assert {(await fa.asset(x))["status"] for x in (a, b)} == {"disposed"}
    await fa.operate(f"/assets/{a}/undo-disposal", {}, key="undo-a")
    got = await fa.asset(a)
    assert got["status"] == "active" and got.get("disposal") is None and D(got["cost"]) == D("12000")
    assert D(await fa.balance("1210")) >= D("12000")
    await fa.fold_matches(a)
    again = await fa.post(f"/assets/{a}/undo-disposal", {"preview": True, "op_key": "undo-a2"})
    assert again.status_code == 422


async def test_undo_disposal_refused_once_its_period_is_locked(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _dispose(fa, car, "2026-05-20")
    await _lock(fa, "2026-05-31")
    r = await fa.post(f"/assets/{car}/undo-disposal", {"preview": True, "op_key": "u"})
    assert r.status_code == 409 and "Correct" in r.json()["detail"]


async def test_disposed_asset_refuses_another_disposal(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _dispose(fa, car, "2026-05-20")
    r = await fa.post(f"/assets/{car}/dispose", {"date": "2026-06-01", "reason": "lost", "preview": True,
                                                 "op_key": "again"})
    assert r.status_code == 409 and "Disposed on 2026-05-20" in r.json()["detail"]
