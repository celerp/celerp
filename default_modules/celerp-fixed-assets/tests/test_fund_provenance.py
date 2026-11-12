# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fund provenance (attribution rule P): an asset keeps the fund of the line it was bought
on, and every journal moving its value (registration, depreciation, a class account move)
carries that fund, so each fund's balance sheet holds its own assets.

The company is managed from 1 January 2026 with funds on: a restricted fund and an
unrestricted one. A car bought on a restricted manual line (12,000, 60 months) charges 200
a month from January; a runabout on the unrestricted line (6,000) charges 100.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from fa_fixtures import FA

pytestmark = pytest.mark.asyncio

D = Decimal


async def _funds(fa: FA) -> dict:
    await fa.ok(await fa.client.put("/accounting/funds/setting", headers=fa.h, json={"enabled": True}))

    async def fund(name, restricted):
        r = await fa.client.post("/accounting/funds", headers=fa.h, json={
            "request_key": uuid.uuid4().hex, "name": name, "restricted": restricted,
            "purpose": "Outreach" if restricted else None})
        assert r.status_code == 200, r.text
        return r.json()["id"]

    return {"R": await fund("Outreach gift", True), "U": await fund("Board reserve", False)}


async def _bought(fa: FA, account: str, amount: int, fund: str, memo: str, ts: str = "2026-01-15") -> str:
    r = await fa.client.post("/accounting/journal-entries", headers=fa.h, json={
        "ts": ts, "memo": memo, "idempotency_token": str(uuid.uuid4()), "entries": [
            {"account": account, "debit": amount, "fund_id": fund},
            {"account": "3100", "credit": amount, "fund_id": fund}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _start(fa: FA) -> dict:
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    await _bought(fa, "1210", 12000, funds["R"], "Outreach car")
    await _bought(fa, "1210", 6000, funds["U"], "Board runabout")
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    van = (await fa.register((await fa.line("Board runabout"))["source"], key="reg-van"))["assets"][0]["id"]
    return {**funds, "car": car, "van": van}


def _by_fund(je: dict) -> dict[tuple[str, str | None], tuple[D, D]]:
    out: dict[tuple[str, str | None], tuple[D, D]] = {}
    for e in je["entries"]:
        key = (e["account"], e.get("fund_id"))
        dr, cr = out.get(key, (D(0), D(0)))
        out[key] = (dr + D(str(e.get("debit") or 0)), cr + D(str(e.get("credit") or 0)))
    return out


async def test_fixed_asset_depreciation_keeps_acquisition_fund(fa):
    s = await _start(fa)
    assert (await fa.state(s["car"]))["fund_id"] == s["R"]
    assert (await fa.state(s["van"]))["fund_id"] == s["U"]

    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")
    prefix = "je:mod:celerp-fixed-assets:"
    run = next(je for je in await fa.module_journals() if je["entity_id"] == f"{prefix}run:2026-01:1")
    assert _by_fund(run) == {("6400", s["R"]): (D("200"), D(0)), ("6400", s["U"]): (D("100"), D(0)),
                             ("1220", s["R"]): (D(0), D("200")), ("1220", s["U"]): (D(0), D("100"))}
    # Each asset's movement points at the credit line of its own fund.
    for asset, fund in ((s["car"], s["R"]), (s["van"], s["U"])):
        mv = next(m for m in await fa.entities("fa_movement") if m["asset_id"] == asset and m["operation"] == "run")
        line = run["entries"][mv["contributions"][0]["line_index"]]
        assert (line["account"], line.get("fund_id")) == ("1220", fund)


async def test_fixed_asset_registration_reclass_keeps_line_fund(fa):
    """A line bought on another account is moved onto the class account on its own fund, and
    a line registered as an expense moves off the asset account on its own fund."""
    s = await _start(fa)
    await _bought(fa, "1240", 900, s["R"], "Outreach trailer", ts="2026-01-20")
    trailer = (await fa.line("Outreach trailer"))["source"]
    done = await fa.register(trailer, key="reg-trailer")
    reg = await fa.state(done["journals"][0])
    assert {e.get("fund_id") for e in reg["entries"]} == {s["R"]}
    assert (await fa.state(done["assets"][0]["id"]))["fund_id"] == s["R"]

    await _bought(fa, "1210", 300, s["U"], "Car mats")
    mats = (await fa.line("Car mats"))["source"]
    expensed = await fa.operate("/register", {"source": mats, "mode": "expense", "expense_account": "6400"},
                                key="reg-mats")
    reclass = await fa.state(expensed["journals"][0])
    assert {e.get("fund_id") for e in reclass["entries"]} == {s["U"]}


async def test_fixed_asset_addition_from_another_fund_refused(fa):
    s = await _start(fa)
    await _bought(fa, "1210", 1080, s["U"], "Roof rack", ts="2026-03-01")
    rack = (await fa.line("Roof rack"))["source"]
    refused = await fa.post(f"/assets/{s['car']}/add", {"kind": "improvement", "source": rack,
                                                        "preview": True, "op_key": "rack"})
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["message_key"] == "fixed_assets.err_line_other_fund"
    assert detail["params"] == {"number": (await fa.state(s["car"]))["number"],
                                "line_fund": "Board reserve", "asset_fund": "Outreach gift"}
    # The van is on the same fund as the line: the improvement is taken.
    added = await fa.operate(f"/assets/{s['van']}/add", {"kind": "improvement", "source": rack}, key="rack-van")
    assert added["assets"][0]["id"] == s["van"]


async def test_fixed_asset_account_move_keeps_each_fund(fa):
    s = await _start(fa)
    r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
        "code": "1216", "name": "Vehicles at cost", "account_type": "asset", "parent_code": "1200"})
    assert r.status_code == 200, r.text
    cls = (await fa.classes())["Vehicles"]
    done = await fa.operate(f"/classes/{cls['id']}/fix-account",
                            {"field": "asset_account", "account": "1216", "mode": "move"}, key="mv")
    moved = await fa.state(done["journals"][0])
    by = _by_fund(moved)
    assert by[("1216", s["R"])] == (D("12000"), D(0)) and by[("1216", s["U"])] == (D("6000"), D(0))
    assert sum(cr for (acct, fund), (_, cr) in by.items() if (acct, fund) == ("1210", s["R"])) == D("12000")
    assert sum(cr for (acct, fund), (_, cr) in by.items() if (acct, fund) == ("1210", s["U"])) == D("6000")
