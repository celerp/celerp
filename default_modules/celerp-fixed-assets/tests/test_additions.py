# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Adding to an asset: improvements, replacement parts, and construction placed in service.

A contributed car (12,000 over 60 months, straight line, full month, from January 2026)
charges 200 a month, so every layer and derecognized part can be read off the calendar.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _lines, _managed_from, _movements, _run

pytestmark = pytest.mark.asyncio

D = Decimal


async def _add(fa: FA, asset_id: str, **body) -> dict:
    return await fa.operate(f"/assets/{asset_id}/add", body, key=f"add-{uuid.uuid4().hex[:8]}")


async def _undo_latest(fa: FA, asset_id: str) -> dict:
    return await fa.operate(f"/assets/{asset_id}/undo-latest", {}, key=f"ul-{uuid.uuid4().hex[:8]}")


async def _journal(fa: FA, movement: dict) -> dict:
    return await fa.state(movement["journal_id"])


async def test_improvement_keeps_the_end_date_or_takes_a_new_life(fa):
    """1,080 in July keeping the end date is 54 months (20 a month); 600 in August over a
    new life of 12 months is 50 a month, until it is undone."""
    await _managed_from(fa)
    car = await _contributed(fa)
    await _add(fa, car, kind="improvement", date="2026-07-01", amount="1080", credit_account="3100")
    moves = await _movements(fa, car)
    assert moves[-1]["operation"] == "improvement" and moves[-1]["life_months"] is None
    assert _lines(await _journal(fa, moves[-1])) == {"1210": (D(1080), D(0)), "3100": (D(0), D(1080))}
    await _run(fa, "2026-07")
    charged = [D(m["amounts"]["accumulated_depreciation"]) for m in await _movements(fa, car)
               if m["operation"] == "run"]
    assert charged[-1] == D(220)
    await _add(fa, car, kind="improvement", date="2026-08-01", amount="600", credit_account="3100", life_months=12)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-08", "preview": True, "op_key": "p8"}))
    assert D(preview["months"][-1]["total"]) == D(270)
    await _undo_latest(fa, car)
    assert D((await fa.asset(car))["cost"]) == D(13080)
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-08", "preview": True, "op_key": "p9"}))
    assert D(preview["months"][-1]["total"]) == D(220)
    await fa.fold_matches(car)


async def test_s2_component_replacement_derecognizes_old(fa):
    """A new engine of 4,000 at the end of June replaces one never recorded separately,
    estimated at 3,000 of the car's cost: a quarter of the car leaves the books with the
    depreciation it took (250 for January to May; full month charges nothing in the month
    it leaves), a 2,750 loss. The engine is a component with its own 36-month life; when
    it is replaced in turn at the end of September, it leaves whole."""
    await _managed_from(fa)
    car = await _contributed(fa)
    await _add(fa, car, kind="replacement", date="2026-06-30", amount="4000", credit_account="3100",
               name="Engine", life_months=36, replaced={"estimate": "3000"})
    moves = await _movements(fa, car)
    out = moves[-1]
    assert out["operation"] == "disposal" and out["reason"] == "replaced" and D(out["share"]) == D("0.25")
    assert _lines(await _journal(fa, out)) == {"6400": (D(1150), D(0)), "1220": (D(250), D(1150)),
                                               "1210": (D(0), D(3000)), "6980": (D(2750), D(0))}
    got = await fa.asset(car)
    assert D(got["cost"]) == D(9000) and D(got["accumulated_depreciation"]) == D(900)
    assert got["status"] == "active"
    components = (await fa.ok(await fa.get(f"/assets/{car}/components")))["items"]
    assert [(c["name"], D(c["cost"]), c["policy"]["life_months"]) for c in components] == [("Engine", D(4000), 36)]
    engine = components[0]["id"]
    assert (await fa.asset(engine))["parent_id"] == car
    await _add(fa, car, kind="replacement", date="2026-09-30", amount="4500", credit_account="3100",
               name="Engine", life_months=36, replaced={"asset_id": engine})
    gone = await fa.asset(engine)
    assert gone["status"] == "disposed" and gone["disposal"]["reason"] == "replaced"
    last = (await _movements(fa, engine))[-1]
    assert _lines(await _journal(fa, last)) == {"6400": (D("333.33"), D(0)), "1220": (D("333.33"), D("333.33")),
                                                "1210": (D(0), D(4000)), "6980": (D("3666.67"), D(0))}
    names = [(c["name"], c["status"]) for c in (await fa.ok(await fa.get(f"/assets/{car}/components")))["items"]]
    assert sorted(names) == [("Engine", "active"), ("Engine", "disposed")]
    await fa.fold_matches(car)
    await fa.fold_matches(engine)


async def test_s2_construction_allocated_exactly_once(fa):
    """The office fit-out bill (18,500 to assets under construction) plus 1,500 added by
    hand is placed in service in two steps; the balance is never allocated twice."""
    line = await fa.line("Office fit-out")
    site = (await fa.register(line["source"], key="fit", class_name="Assets under construction"))["assets"][0]["id"]
    day = line["date"]
    await _add(fa, site, kind="improvement", date=day, amount="1500", credit_account="3100")
    got = await fa.asset(site)
    assert got["under_construction"] is True and D(got["cost"]) == D(20000)
    classes = await fa.classes()
    path = f"/assets/{site}/place-in-service"
    first = {"date": day, "allocations": [
        {"class_id": classes["Furniture and fittings"]["id"], "name": "Office fittings", "amount": "12000"},
        {"class_id": classes["Computer equipment"]["id"], "name": "Meeting room screens", "amount": "5000"}]}
    preview = await fa.ok(await fa.post(path, {**first, "preview": True, "op_key": "pis1"}))
    assert D(preview["remainder"]) == D(3000)
    done = await fa.ok(await fa.post(path, {**first, "preview": False, "op_key": "pis1",
                                            "fingerprint": preview["fingerprint"]}))
    placed = [await fa.asset(a["id"]) for a in done["assets"]]
    assert [(a["name"], D(a["cost"]), a["available_date"]) for a in placed] == [
        ("Office fittings", D(12000), day), ("Meeting room screens", D(5000), day)]
    assert _lines(await fa.state(done["journals"][0])) == {"1210": (D(17000), D(0)), "1240": (D(0), D(17000))}
    got = await fa.asset(site)
    assert got["status"] == "active" and D(got["cost"]) == D(3000)
    over = await fa.post(path, {"date": day, "allocations": [
        {"class_id": classes["Furniture and fittings"]["id"], "name": "Reception desk", "amount": "3000.01"}],
        "preview": True, "op_key": "pis2"})
    assert over.status_code == 422 and "$3,000.00 FA-0001 holds" in over.json()["detail"]
    rest = {"date": day, "allocations": [
        {"class_id": classes["Furniture and fittings"]["id"], "name": "Reception desk", "amount": "3000"}]}
    await fa.operate(path, rest, key="pis3")
    got = await fa.asset(site)
    assert got["status"] == "placed_in_service" and D(got["cost"]) == 0
    again = await fa.post(path, {**rest, "preview": True, "op_key": "pis4"})
    assert again.status_code == 409
    await _undo_latest(fa, site)
    got = await fa.asset(site)
    assert got["status"] == "active" and D(got["cost"]) == D(3000)
    assert [a["status"] for a in await fa.entities("fa_asset") if a.get("name") == "Reception desk"] == ["undone"]
    await fa.fold_matches(site)


async def test_u7_a_header_account_is_refused_at_review_and_not_offered(fa):
    """Review refuses a header credit account as Confirm would, posting nothing; the Add
    form offers only accounts a journal line can post to."""
    await _managed_from(fa)
    car = await _contributed(fa)
    moves, held = len(await _movements(fa, car)), await fa.balance("1110")
    r = await fa.post(f"/assets/{car}/add", {"kind": "improvement", "date": "2026-07-01", "amount": "1080",
                                             "credit_account": "1110", "preview": True, "op_key": "u7-review"})
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.destination.header", r.text
    assert len(await _movements(fa, car)) == moves and await fa.balance("1110") == held
    form = (await fa.page(f"/fixed-assets/assets/{car}/add")).text
    assert 'value="1110"' not in form and 'value="3100"' in form, "a header is no choice; a leaf is"
