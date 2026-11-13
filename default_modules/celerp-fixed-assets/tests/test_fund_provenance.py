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


async def _bill_asset_on(fa: FA, fund: str, name: str, amount: int) -> str:
    """An open bill from the document import whose one line buys ``name`` onto the vehicles
    account, its fund chosen on the import; it posts as it is imported."""
    from income_books import _issued_import, _line

    doc = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    record = _issued_import(doc, [_line(None, name, amount, account_code="1210")])
    record["data"].update(doc_type="bill", contact_id="supplier:1")
    r = await fa.client.post("/docs/import/batch", headers=fa.h, json={
        "records": [record], "fund_choices": {"records": {doc: fund}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    return doc


async def _change_bill(fa: FA, doc: str, fund: str):
    from celerp.models.projections import Projection

    fa.session.expire_all()
    row = await fa.session.get(Projection, {"company_id": fa.auth["company_id"], "entity_id": doc})
    return await fa.client.post(f"/docs/{doc}/change-fund", headers=fa.h, json={
        "document": fund, "lines": {}, "reason": "Bought for the board, not the gift",
        "expected_version": row.version, "request_key": uuid.uuid4().hex})


async def _fa_standing(fa: FA) -> dict[tuple[str | None, str], D]:
    """(fund, account) -> what the posted fixed-asset journals hold, zeros left out."""
    out: dict[tuple[str | None, str], D] = {}
    for je in await fa.module_journals():
        if je.get("status") != "posted":
            continue
        for e in je["entries"]:
            key = (e.get("fund_id"), e["account"])
            out[key] = out.get(key, D(0)) + D(str(e.get("debit") or 0)) - D(str(e.get("credit") or 0))
    return {k: v for k, v in out.items() if v}


async def test_change_fund_moves_dependents(fa):
    """An asset bought on an outside bill's line on R, depreciated in a run beside an asset on
    U: Change fund on the bill to U moves the asset's fund and every fixed-asset entry it has
    (its share of the run included) onto U at their own dates, inside the one judged action;
    with the run's month locked the whole change is refused and nothing moves."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    await _bought(fa, "1210", 6000, funds["U"], "Board runabout")
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    van = (await fa.register((await fa.line("Board runabout"))["source"], key="reg-van"))["assets"][0]["id"]
    assert (await fa.state(car))["fund_id"] == funds["R"]
    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")
    before = await _fa_standing(fa)
    assert any(fund == funds["R"] for fund, _ in before), before

    await fa.client.post("/accounting/period-lock", headers=fa.h, json={"lock_date": "2026-01-31"})
    locked = await _change_bill(fa, bill, funds["U"])
    assert locked.status_code == 422, locked.text
    assert (await fa.state(car))["fund_id"] == funds["R"] and await _fa_standing(fa) == before
    await fa.client.post("/accounting/period-lock", headers=fa.h, json={"lock_date": None})

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 200, r.text
    assert (await fa.state(car))["fund_id"] == funds["U"]
    assert (await fa.state(van))["fund_id"] == funds["U"]
    after = await _fa_standing(fa)
    moved: dict[tuple[str | None, str], D] = {}
    for (fund, account), amount in before.items():
        key = (funds["U"] if fund == funds["R"] else fund, account)
        moved[key] = moved.get(key, D(0)) + amount
    assert after == {k: v for k, v in moved.items() if v}
    # The car's movement still reads its share of the run, now on U.
    prefix = "je:mod:celerp-fixed-assets:"
    mv = next(m for m in await fa.entities("fa_movement") if m["asset_id"] == car and m["operation"] == "run")
    live = await fa.module_journals()
    from celerp.services.auto_je import current_entry

    run_id = await current_entry(fa.session, fa.auth["company_id"], f"{prefix}run:2026-01:1")
    run = next(je for je in live if je["entity_id"] == run_id)
    assert run.get("status") == "posted" and run_id != f"{prefix}run:2026-01:1"
    line = run["entries"][mv["contributions"][0]["line_index"]]
    assert (line["account"], line.get("fund_id")) == ("1220", funds["U"])
    # The next run posts the car on U.
    await fa.operate("/runs", {"month": "2026-02"}, key="run-feb")
    feb = next(je for je in await fa.module_journals() if je["entity_id"] == f"{prefix}run:2026-02:1")
    assert {fund for _, fund in _by_fund(feb)} == {funds["U"]}


async def _run_line(fa: FA, asset: str, month: str) -> dict:
    """The live run line an asset's movement for ``month`` points at."""
    from celerp.services.auto_je import current_entry

    prefix = "je:mod:celerp-fixed-assets:"
    mv = next(m for m in await fa.entities("fa_movement")
              if m["asset_id"] == asset and m["operation"] == "run" and m.get("posting_date", "")[:7] == month)
    c = mv["contributions"][0]
    run_id = await current_entry(fa.session, fa.auth["company_id"], c["journal_id"])
    run = next(je for je in await fa.module_journals() if je["entity_id"] == run_id)
    assert run.get("status") == "posted" and run_id != f"{prefix}run:{month}:1"
    return {**run["entries"][c["line_index"]], "_run": run}


async def test_change_fund_splits_run_line_shared_with_another_asset(fa):
    """Two assets on R share the run's R lines: Change fund on the bill of one moves only its
    share onto U as new lines, and each asset's movement still points at its own share."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    await _bought(fa, "1210", 6000, funds["R"], "Outreach van")
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    van = (await fa.register((await fa.line("Outreach van"))["source"], key="reg-van"))["assets"][0]["id"]
    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 200, r.text
    assert (await fa.state(car))["fund_id"] == funds["U"]
    assert (await fa.state(van))["fund_id"] == funds["R"]
    car_line, van_line = await _run_line(fa, car, "2026-01"), await _run_line(fa, van, "2026-01")
    assert (car_line["account"], car_line.get("fund_id"), D(str(car_line["credit"]))) == ("1220", funds["U"], D("200"))
    assert (van_line["account"], van_line.get("fund_id"), D(str(van_line["credit"]))) == ("1220", funds["R"], D("100"))
    assert _by_fund(car_line["_run"]) == {("6400", funds["R"]): (D("100"), D(0)), ("6400", funds["U"]): (D("200"), D(0)),
                                          ("1220", funds["R"]): (D(0), D("100")), ("1220", funds["U"]): (D(0), D("200"))}


async def test_change_fund_refused_when_asset_lines_would_split(fa):
    """An asset bought on the bill's line and improved from a manual line, both on R: moving
    only the bill to U would leave the asset on two funds, so the change is refused."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    await _bought(fa, "1210", 1080, funds["R"], "Roof rack", ts="2026-03-01")
    await fa.operate(f"/assets/{car}/add", {"kind": "improvement", "source": (await fa.line("Roof rack"))["source"]},
                     key="rack")
    before = await _fa_standing(fa)

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "fixed_assets.err_change_fund_splits_asset"
    assert detail["params"] == {"number": (await fa.state(car))["number"], "funds": "Board reserve, Outreach gift"}
    assert (await fa.state(car))["fund_id"] == funds["R"] and await _fa_standing(fa) == before


async def test_change_fund_refused_after_class_account_move(fa):
    """A class account move posts one line per fund for all the class's assets: an asset it
    carried refuses Change fund on its bill and names the move."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
        "code": "1216", "name": "Vehicles at cost", "account_type": "asset", "parent_code": "1200"})
    assert r.status_code == 200, r.text
    cls = (await fa.classes())["Vehicles"]
    done = await fa.operate(f"/classes/{cls['id']}/fix-account",
                            {"field": "asset_account", "account": "1216", "mode": "move"}, key="mv")
    moved_on = str((await fa.state(done["journals"][0])).get("ts"))[:10]
    before = await _fa_standing(fa)

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "fixed_assets.err_change_fund_class_move"
    assert detail["params"] == {"number": (await fa.state(car))["number"], "cls": "Vehicles", "date": moved_on}
    assert (await fa.state(car))["fund_id"] == funds["R"] and await _fa_standing(fa) == before


async def test_change_fund_refused_when_improvement_line_would_split(fa):
    """An asset registered from a manual line on R and improved from the bill's line on R:
    moving the bill to U would leave the asset on two funds, so the change is refused."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    await _bought(fa, "1210", 12000, funds["R"], "Outreach car")
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    bill = await _bill_asset_on(fa, funds["R"], "Roof rack", 1080)
    await fa.operate(f"/assets/{car}/add", {"kind": "improvement", "source": (await fa.line("Roof rack"))["source"]},
                     key="rack")
    before = await _fa_standing(fa)

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "fixed_assets.err_change_fund_splits_asset"
    assert (await fa.state(car))["fund_id"] == funds["R"] and await _fa_standing(fa) == before


async def test_change_fund_moves_expensed_line_journal(fa):
    """A bill line registered as an expense moved off the asset account on its fund: Change
    fund on the bill moves that journal onto the new fund with the line."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Car mats", 300)
    await fa.operate("/register", {"source": (await fa.line("Car mats"))["source"], "mode": "expense",
                                   "expense_account": "6400"}, key="reg-mats")
    assert await _fa_standing(fa) == {(funds["R"], "1210"): D("-300"), (funds["R"], "6400"): D("300")}

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 200, r.text
    assert await _fa_standing(fa) == {(funds["U"], "1210"): D("-300"), (funds["U"], "6400"): D("300")}


async def test_change_fund_moves_assets_placed_from_a_moved_site(fa):
    """A site under construction bought on the bill's line on R, placed in service as a car
    that is then depreciated: Change fund on the bill to U moves the site, the car it built
    and the car's depreciation, since all of the car's cost came from that line."""
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    classes = await fa.classes()
    site_class = classes["Assets under construction"]
    from income_books import _issued_import, _line

    bill = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    record = _issued_import(bill, [_line(None, "Workshop build", 12000, account_code=site_class["asset_account"])])
    record["data"].update(doc_type="bill", contact_id="supplier:1")
    r = await fa.client.post("/docs/import/batch", headers=fa.h, json={
        "records": [record], "fund_choices": {"records": {bill: funds["R"]}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    line = await fa.line("Workshop build")
    site = (await fa.register(line["source"], key="site", class_name="Assets under construction"))["assets"][0]["id"]
    placed = await fa.operate(f"/assets/{site}/place-in-service", {"date": line["date"], "allocations": [
        {"class_id": classes["Vehicles"]["id"], "name": "Workshop van", "amount": "12000"}]}, key="place")
    van = placed["assets"][0]["id"]
    assert (await fa.state(van))["fund_id"] == funds["R"]
    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")
    before = await _fa_standing(fa)

    r = await _change_bill(fa, bill, funds["U"])
    assert r.status_code == 200, r.text
    assert (await fa.state(site))["fund_id"] == funds["U"] and (await fa.state(van))["fund_id"] == funds["U"]
    moved: dict[tuple[str | None, str], D] = {}
    for (fund, account), amount in before.items():
        key = (funds["U"] if fund == funds["R"] else fund, account)
        moved[key] = moved.get(key, D(0)) + amount
    assert await _fa_standing(fa) == {k: v for k, v in moved.items() if v}


async def test_change_fund_then_undo_leaves_every_fund_zero_for_a_bought_asset(fa):
    """The asset case of test_change_fund_then_undo_leaves_every_fund_zero: a bill whose line
    bought a car depreciated once, changed from R to U, then voided at the source. The bill's own
    entries leave every fund at zero; the car's entries stay posted on U, as a source void leaves
    an asset's entries; unvoid restores the bill on U, not on R."""
    from income_books import _standing

    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")
    assert (await _change_bill(fa, bill, funds["U"])).status_code == 200
    prefix = f"je:auto:{bill}:"
    own = await _standing(fa.session, fa.auth, prefix)
    assert own and {fund for fund, _ in own} == {funds["U"]}, own
    assets = await _fa_standing(fa)
    assert assets and {fund for fund, _ in assets} == {funds["U"]}, assets

    r = await fa.client.post(f"/docs/{bill}/void", headers=fa.h, json={"reason": "Raised in error"})
    assert r.status_code == 200, r.text
    assert await _standing(fa.session, fa.auth, prefix) == {}
    fa.session.expire_all()
    assert await _fa_standing(fa) == assets and (await fa.state(car))["fund_id"] == funds["U"]
    assert (await fa.client.post(f"/docs/{bill}/unvoid", headers=fa.h, json={})).status_code == 200
    assert await _standing(fa.session, fa.auth, prefix) == own
    fa.session.expire_all()
    assert await _fa_standing(fa) == assets


async def test_journal_reads_follow_change_fund(fa):
    """A car bought on a bill's line, depreciated and scrapped, then the bill moved from R to U:
    undoing the disposal shows the disposal journal Change fund posted again on U, the entry
    standing for the one the disposal stored, not nothing. The module reads a stored journal id
    only through store._journal, which follows Change fund; store._state refuses a journal id."""
    from celerp_fixed_assets import store

    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    funds = await _funds(fa)
    bill = await _bill_asset_on(fa, funds["R"], "Outreach car", 12000)
    car = (await fa.register((await fa.line("Outreach car"))["source"], key="reg-car"))["assets"][0]["id"]
    await fa.operate("/runs", {"month": "2026-01"}, key="run-jan")
    await fa.operate(f"/assets/{car}/dispose", {"date": "2026-02-10", "reason": "scrapped"}, key="scrap")
    shown = (await fa.ok(await fa.post(f"/assets/{car}/undo-disposal", {"preview": True, "op_key": "u1"})))["journals"]
    assert len(shown) == 1, shown

    assert (await _change_bill(fa, bill, funds["U"])).status_code == 200
    after = await fa.ok(await fa.post(f"/assets/{car}/undo-disposal", {"preview": True, "op_key": "u2"}))
    assert after["journals"] == shown, after
    await fa.ok(await fa.post(f"/assets/{car}/undo-disposal", {"preview": False, "op_key": "u2",
                                                              "fingerprint": after["fingerprint"]}))
    assert not any(fund == funds["R"] for fund, _ in await _fa_standing(fa))

    disposal = next(m for m in await fa.entities("fa_movement") if m["asset_id"] == car and m.get("journal_id")
                    and m["operation"] == "disposal")
    with pytest.raises(ValueError):
        await store._state(fa.session, fa.auth["company_id"], disposal["journal_id"])
    live = await store._journal(fa.session, fa.auth["company_id"], disposal["journal_id"])
    assert live["status"] == "void" and {e.get("fund_id") for e in live["entries"]} == {funds["U"]}


async def test_asset_sale_proceeds_keep_the_asset_fund(fa):
    """Selling the car bought on R: the draft invoice's line takes R, so the invoice's clearing
    leg and the disposal's meet on R and clearing nets to zero on every fund (rule P). A line
    moved to another fund before finalizing is refused naming both funds, since the proceeds
    would land on one fund and the car leave another."""
    from celerp.services.journal_accounts import fund_account_balances
    from test_sales import _asset_line, _doc, _patch, _patch_lines, _sell

    s = await _start(fa)
    invoice = await _sell(fa, s["car"], on="2026-02-10")
    line = await _asset_line(fa, invoice)
    assert line.get("fund_id") == s["R"], line
    others = [li for li in (await fa.state(invoice))["line_items"] if li.get("line_id") != line["line_id"]]
    assert (await _patch(fa, invoice, [*others, {**line, "fund_id": s["U"]}])).status_code == 200
    r = await _doc(fa, invoice, "finalize")
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "fixed_assets.err_line_other_fund", r.text
    await _patch_lines(fa, invoice, [*others, line])

    await fa.ok(await _doc(fa, invoice, "finalize"))
    held = await fund_account_balances(fa.session, fa.auth["company_id"])
    clearing = {fund: by["1160"] for fund, by in held.items() if by.get("1160")}
    assert not clearing, clearing
