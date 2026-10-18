# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Moving an asset (location and custodian), reclassifying it, and transferring it to
another company the same owner keeps books for.

A contributed car (12,000 over 60 months, straight line, full month, from January
2026) charges 200 a month, so what each operation moves can be read off the calendar.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fa_fixtures import FA
from test_l10n import _map, _tax_book
from test_runs import _contributed, _lines, _lock, _managed_from, _movements, _race_company, _run, _set_module

pytestmark = pytest.mark.asyncio

D = Decimal


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _location(fa: FA, name: str = "Warehouse 2") -> str:
    r = await fa.client.post("/companies/me/locations", headers=fa.h, json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _move(fa: FA, asset_id: str, headers=None, **body) -> dict:
    return await fa.operate(f"/assets/{asset_id}/move", body, key=f"m-{uuid.uuid4().hex[:8]}", headers=headers)


async def _plant(fa: FA) -> dict:
    """A class on accounts of its own: 1211 cost, 1221 accumulated depreciation, ten years."""
    for code, name in (("1211", "Plant and machinery"), ("1221", "Accumulated depreciation - plant")):
        r = await fa.client.post("/accounting/accounts", headers=fa.h, json={
            "code": code, "name": name, "account_type": "asset", "parent_code": "1200"})
        assert r.status_code == 200, r.text
    return await fa.ok(await fa.post("/classes", {
        "name": "Plant", "kind": "tangible", "asset_account": "1211", "accumulated_depreciation_account": "1221",
        "depreciation_account": "6400", "method": "straight_line", "life_months": 120}))


async def _reclassify(fa: FA, asset_id: str, class_id: str, on: str, headers=None) -> dict:
    return await fa.operate(f"/assets/{asset_id}/reclassify", {"date": on, "class_id": class_id},
                            key=f"r-{uuid.uuid4().hex[:8]}", headers=headers)


def _journal_lines(journals: list[dict]) -> dict[str, tuple[D, D]]:
    out: dict[str, tuple[D, D]] = {}
    for je in journals:
        for code, (dr, cr) in _lines(je).items():
            was = out.get(code, (D(0), D(0)))
            out[code] = (was[0] + dr, was[1] + cr)
    return out


async def _journals_of(fa: FA, asset_id: str, operation: str) -> list[dict]:
    key = f"je:mod:celerp-fixed-assets:asset:{asset_id.removeprefix('fa:asset:')}:{operation}:"
    return [je for je in await fa.module_journals() if je["entity_id"].startswith(key) and je["status"] == "posted"]


async def _second_company(fa: FA, name: str = "Beta Co", *, managed: str = "2026-01-01") -> FA:
    """Another company of the same owner, with its demo data, fixed assets started and managed."""
    r = await fa.client.post("/companies", headers=fa.h, json={"name": name})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await fa.client.get("/companies/me", headers=headers)).json()
    b = FA(fa.client, fa.session, {"headers": headers, "company_id": uuid.UUID(me["id"])})
    r = await fa.client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200, r.text
    await b.ok(await b.post("/start"))
    await _managed_from(b, managed)
    return b


def _transfer_body(b: FA, on: str = "2026-04-20", price: str = "11000", **extra) -> dict:
    return {"date": on, "target_company_id": str(b.auth["company_id"]), "price": price,
            "receivable_account": "1120", "payable_account": "2110", **extra}


async def _transfer(fa: FA, b: FA, asset_id: str, **body) -> dict:
    vehicles = (await b.classes())["Vehicles"]["id"]
    return await fa.operate(f"/assets/{asset_id}/transfer", {**_transfer_body(b, class_id=vehicles), **body},
                            key=f"t-{uuid.uuid4().hex[:8]}")


async def _refused_transfer(fa: FA, b: FA, asset_id: str, **body):
    vehicles = (await b.classes())["Vehicles"]["id"]
    return await fa.post(f"/assets/{asset_id}/transfer", {**_transfer_body(b, class_id=vehicles), **body,
                                                          "preview": True, "op_key": "x"})


async def _snapshot(fa: FA, asset_id: str) -> tuple:
    return (await fa.state(asset_id), len(await _movements(fa, asset_id)),
            sorted(je["entity_id"] for je in await fa.module_journals()))


# ── Move ─────────────────────────────────────────────────────────────────────


async def test_move_location_and_custodian_with_history_and_undo(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    yard = await _location(fa, "Yard")
    driver, _ = await fa.member("operator")
    before = len(await fa.module_journals())
    await _move(fa, car, date="2026-02-01", location_id=yard, custodian={"user_id": str(driver)})
    asset = await fa.asset(car)
    assert asset["location_id"] == yard and asset["custodian"] == {"user_id": str(driver), "name": "Operator"}
    await _move(fa, car, date="2026-03-01", custodian={"name": "Night shift lead", "not_user": True})
    asset = await fa.asset(car)
    assert asset["location_id"] == yard and asset["custodian"] == {"name": "Night shift lead", "not_user": True}
    assert [p["date"] for p in asset["placements"]] == ["2026-02-01", "2026-03-01"]
    # A move posts nothing and leaves the figures alone.
    assert len(await fa.module_journals()) == before and D(asset["cost"]) == D("12000")
    await fa.operate(f"/assets/{car}/undo-move", {}, key="um")
    asset = await fa.asset(car)
    assert asset["custodian"] == {"user_id": str(driver), "name": "Operator"}
    assert [p["date"] for p in asset["placements"]] == ["2026-02-01"]


async def test_move_several_assets_at_once(fa):
    await _managed_from(fa)
    car, van = await _contributed(fa), await _contributed(fa, name="Van")
    yard = await _location(fa, "Yard")
    done = await fa.operate("/move", {"asset_ids": [car, van], "date": "2026-02-01", "location_id": yard},
                            key="bulk")
    assert {a["id"] for a in done["assets"]} == {car, van}
    assert {(await fa.asset(a))["location_id"] for a in (car, van)} == {yard}


async def test_move_validates_and_checks_roles(fa):
    await _managed_from(fa)
    car = await _contributed(fa)

    async def refused(body, headers=None):
        return await fa.post(f"/assets/{car}/move", {"date": "2026-02-01", **body, "preview": True, "op_key": "x"},
                             headers)

    nothing = await refused({})
    assert nothing.status_code == 422 and "location or a custodian" in nothing.json()["detail"]
    assert (await refused({"location_id": str(uuid.uuid4())})).status_code == 422
    stranger = await refused({"custodian": {"user_id": str(uuid.uuid4())}})
    assert stranger.status_code == 422 and "member" in stranger.json()["detail"]
    assert (await refused({"custodian": {"name": "  "}})).status_code == 422
    yard = await _location(fa, "Yard")
    await _move(fa, car, date="2026-03-01", location_id=yard)
    earlier = await refused({"date": "2026-02-15", "location_id": yard})
    assert earlier.status_code == 422 and "2026-03-01" in earlier.json()["detail"]
    # Moving an asset is an operator's job; a viewer cannot reach fixed assets at all.
    operator = await fa.role_headers("operator")
    await _move(fa, car, headers=operator, date="2026-03-02", custodian={"name": "Driver", "not_user": True})
    assert (await refused({"location_id": yard}, await fa.role_headers("viewer"))).status_code == 403


async def test_move_stale_form_asks_to_review_again(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    yard, dock = await _location(fa, "Yard"), await _location(fa, "Dock")
    body = {"date": "2026-02-01", "location_id": yard}
    preview = await fa.ok(await fa.post(f"/assets/{car}/move", {**body, "preview": True, "op_key": "a"}))
    await _move(fa, car, date="2026-02-01", location_id=dock)
    stale = await fa.post(f"/assets/{car}/move", {**body, "preview": False, "op_key": "a",
                                                  "fingerprint": preview["fingerprint"]})
    assert stale.status_code == 409 and "Review again" in stale.json()["detail"]
    await _move(fa, car, **body)
    assert (await fa.asset(car))["location_id"] == yard


async def test_location_holding_assets_cannot_be_deleted(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    yard = await _location(fa, "Yard")
    await _move(fa, car, date="2026-02-01", location_id=yard)
    refused = await fa.client.delete(f"/companies/me/locations/{yard}", headers=fa.h)
    assert refused.status_code == 409 and "1 asset" in refused.json()["detail"]
    await fa.operate(f"/assets/{car}/undo-move", {}, key="um")
    assert (await fa.client.delete(f"/companies/me/locations/{yard}", headers=fa.h)).status_code == 200


# ── Reclassify ───────────────────────────────────────────────────────────────


async def test_reclassify_moves_balances_and_depreciates_on_the_new_class(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    plant = await _plant(fa)
    preview = await fa.ok(await fa.post(f"/assets/{car}/reclassify", {
        "date": "2026-05-10", "class_id": plant["id"], "preview": True, "op_key": "r"}))
    assert preview["journals"][0]["lines"]
    await _reclassify(fa, car, plant["id"], "2026-05-10")
    # April is caught up on the old class, then cost and accumulated depreciation move.
    assert _journal_lines(await _journals_of(fa, car, "reclassify")) == {
        "6400": (D("200"), D(0)), "1220": (D("800"), D("200")),
        "1211": (D("12000"), D(0)), "1210": (D(0), D("12000")), "1221": (D(0), D("800"))}
    asset = await fa.asset(car)
    assert asset["class_id"] == plant["id"] and asset["policy"]["life_months"] == 120
    assert D(asset["accumulated_depreciation"]) == D("800")
    await fa.fold_matches(car)
    # From May the new class's policy runs on what is left: 11,200 over the 116 months left.
    await _run(fa, "2026-05")
    may = (await fa.module_journals())
    (run,) = [je for je in may if je["entity_id"].endswith("run:2026-05:1")]
    assert _lines(run)["1221"] == (D(0), D("96.55")) and "1220" not in _lines(run)
    assert D((await fa.balance("1220"))) == 0 and D(await fa.balance("1221")) == D("-896.55")


async def test_reclassify_shows_in_the_schedule_transfers_column(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    plant = await _plant(fa)
    await _reclassify(fa, car, plant["id"], "2026-05-10")
    got = await fa.ok(await fa.get("/reports/schedule", date_from="2026-01-01", date_to="2026-05-31"))
    by_name = {c["name"]: c for c in got["classes"]}
    vehicles, moved = by_name["Vehicles"], by_name["Plant"]
    assert D(vehicles["additions"]) == D("12000") and D(vehicles["transfers"]) == D("-12000")
    assert D(vehicles["cost_closing"]) == 0 and D(vehicles["depreciation_closing"]) == 0
    assert D(vehicles["depreciation_charge"]) == D("800") and D(vehicles["depreciation_other"]) == D("-800")
    assert D(moved["transfers"]) == D("12000") and D(moved["depreciation_other"]) == D("800")
    assert D(moved["cost_closing"]) == D("12000") and D(moved["depreciation_closing"]) == D("800")


async def test_reclassify_undo_and_refusals(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    plant = await _plant(fa)
    vehicles = (await fa.classes())["Vehicles"]["id"]
    charged = await fa.post(f"/assets/{car}/reclassify", {"date": "2026-03-10", "class_id": plant["id"],
                                                          "preview": True, "op_key": "x"})
    assert charged.status_code == 409 and "2026-03" in charged.json()["detail"]
    same = await fa.post(f"/assets/{car}/reclassify", {"date": "2026-05-10", "class_id": vehicles,
                                                       "preview": True, "op_key": "x"})
    assert same.status_code == 422
    operator = await fa.role_headers("operator")
    assert (await fa.post(f"/assets/{car}/reclassify", {"date": "2026-05-10", "class_id": plant["id"],
                                                        "preview": True, "op_key": "x"}, operator)).status_code == 403
    await _reclassify(fa, car, plant["id"], "2026-05-10")
    assert (await fa.asset(car))["undoable"]
    await fa.operate(f"/assets/{car}/undo-latest", {}, key="u")
    asset = await fa.asset(car)
    assert asset["class_id"] == vehicles and asset["policy"]["life_months"] == 60
    assert not asset.get("class_history")
    assert await _journals_of(fa, car, "reclassify") == []
    assert await fa.balance("1211") == 0 and await fa.balance("1220") == D("-600")
    await fa.fold_matches(car)
    # Undone, May runs on the old class again.
    await _run(fa, "2026-05")
    assert await fa.balance("1220") == D("-1000")


# ── Inter-company transfer ───────────────────────────────────────────────────


async def test_transfer_targets_only_companies_the_user_can_post_in(fa):
    b = await _second_company(fa)
    targets = (await fa.ok(await fa.get("/transfer-targets")))["items"]
    assert [t["company_id"] for t in targets] == [str(b.auth["company_id"])]
    assert targets[0]["name"] == "Beta Co"
    # The form chooses the class and the liability account there, so both come along.
    assert "Vehicles" in [c["name"] for c in targets[0]["classes"]]
    assert "2110" in [a["code"] for a in targets[0]["accounts"]]
    assert "1120" not in [a["code"] for a in targets[0]["accounts"]]
    await _set_module(b, False)
    reachable = await fa.ok(await fa.get("/transfer-targets"))
    assert reachable["items"] == []
    assert reachable["others"] == [{"company_id": str(b.auth["company_id"]), "name": "Beta Co", "reason": "not_using"}]


async def test_s5_transfer_uses_related_party_accounts_no_clearing(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    b = await _second_company(fa)
    done = await _transfer(fa, b, car)
    # A: depreciation to the date, then out at the price against the related company's account.
    lines = _journal_lines(await _journals_of(fa, car, "ict"))
    assert lines["1120"] == (D("11000"), D(0)) and lines["1210"] == (D(0), D("12000"))
    assert "1160" not in lines and await fa.balance("1160") == 0
    assert (await fa.asset(car))["status"] == "disposed"
    assert (await fa.asset(car))["disposal"]["reason"] == "transferred"
    await fa.fold_matches(car)
    # B: the asset in at the price against the related company's account, no clearing.
    target = done["target"]
    assert target["company_id"] == str(b.auth["company_id"])
    theirs = await b.asset(target["asset_id"])
    assert D(theirs["cost"]) == D("11000") and theirs["status"] == "active"
    assert theirs["available_date"] == "2026-04-20" and theirs["policy"]["life_months"] == 56
    assert theirs["transferred_from"]["number"] == (await fa.asset(car))["number"]
    assert [m["operation"] for m in theirs["transferred_from"]["history"]][0] == "contributed"
    their_lines = _journal_lines(await _journals_of(b, target["asset_id"], "ict"))
    assert their_lines == {"1210": (D("11000"), D(0)), "2110": (D(0), D("11000"))}
    assert await b.balance("1160") == 0
    await b.fold_matches(target["asset_id"])
    # A header account in either company is refused.
    van = await _contributed(fa, name="Van")
    header = await _refused_transfer(fa, b, van, receivable_account="1100")
    assert header.status_code == 422 and "header" in header.json()["detail"]
    header = await _refused_transfer(fa, b, van, payable_account="2100")
    assert header.status_code == 422 and "header" in header.json()["detail"]


async def test_s5_related_party_account_type_checked(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    b = await _second_company(fa)
    expense = await _refused_transfer(fa, b, car, receivable_account="6200")
    assert expense.status_code == 422
    assert "asset account" in expense.json()["detail"] and "Demo Co" in expense.json()["detail"]
    receivable = await _refused_transfer(fa, b, car, payable_account="1120")
    assert receivable.status_code == 422
    assert "liability account" in receivable.json()["detail"] and "Beta Co" in receivable.json()["detail"]


async def test_s5_transfer_atomic_both_or_neither(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    b = await _second_company(fa)
    before = await _snapshot(fa, car)
    await _lock(b, "2026-04-30")
    locked = await _refused_transfer(fa, b, car)
    assert locked.status_code == 409 and "Beta Co" in locked.json()["detail"]
    await _lock(b, None)
    r = await b.client.patch("/accounting/accounts/2140", headers=b.h, json={"is_active": False})
    assert r.status_code == 200, r.text
    inactive = await _refused_transfer(fa, b, car, payable_account="2140")
    assert inactive.status_code == 422 and "2140" in inactive.json()["detail"]
    await _set_module(b, False)
    off = await _refused_transfer(fa, b, car)
    assert off.status_code == 409 and "Beta Co" in off.json()["detail"]
    assert await _snapshot(fa, car) == before
    assert [a for a in await b.entities("fa_asset") if a.get("transferred_from")] == []


async def test_s5_authority_checked_in_both(fa):
    from celerp.models.accounting import UserCompany

    await _managed_from(fa)
    car = await _contributed(fa)
    b = await _second_company(fa)
    vehicles = (await b.classes())["Vehicles"]["id"]
    body = {**_transfer_body(b, class_id=vehicles), "op_key": "t"}
    preview = await fa.ok(await fa.post(f"/assets/{car}/transfer", {**body, "preview": True}))
    # The role in the target company is read again when the transfer is confirmed.
    await fa.session.execute(update(UserCompany).where(
        UserCompany.company_id == b.auth["company_id"]).values(role="operator"))
    await fa.session.commit()
    refused = await fa.post(f"/assets/{car}/transfer", {**body, "fingerprint": preview["fingerprint"]})
    assert refused.status_code == 403
    reachable = await fa.ok(await fa.get("/transfer-targets"))
    assert reachable["items"] == [] and [o["reason"] for o in reachable["others"]] == ["no_rights"]
    assert (await fa.asset(car))["status"] == "active"
    # An operator in the source company cannot transfer either.
    operator = await fa.role_headers("operator")
    assert (await fa.post(f"/assets/{car}/transfer", {**body, "preview": True}, operator)).status_code == 403


async def test_s5_currency_mismatch_needs_rate(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    b = await _second_company(fa)
    r = await b.client.patch("/companies/me/books", headers=b.h, json={"currency": "EUR"})
    assert r.status_code == 200, r.text
    no_rate = await _refused_transfer(fa, b, car)
    assert no_rate.status_code == 422 and "rate" in no_rate.json()["detail"]
    assert (await _refused_transfer(fa, b, car, rate="-1")).status_code == 422
    done = await _transfer(fa, b, car, rate="0.9")
    theirs = await b.asset(done["target"]["asset_id"])
    assert D(theirs["cost"]) == D("9900")
    assert _journal_lines(await _journals_of(b, theirs["id"], "ict")) == {
        "1210": (D("9900"), D(0)), "2110": (D(0), D("9900"))}


async def test_transfer_undo_both_while_untouched(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    b = await _second_company(fa)
    done = await _transfer(fa, b, car)
    theirs = done["target"]["asset_id"]
    assert (await fa.asset(car))["undoable"]
    await fa.operate(f"/assets/{car}/undo-latest", {}, key="u")
    assert (await fa.asset(car))["status"] == "active"
    assert await _journals_of(fa, car, "ict") == []
    assert (await b.asset(theirs))["status"] == "undone"
    assert await _journals_of(b, theirs, "ict") == []
    await fa.fold_matches(car)


async def test_transfer_not_undone_once_the_target_moved_on(fa):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    b = await _second_company(fa)
    done = await _transfer(fa, b, car)
    await _run(b, "2026-05")
    refused = await fa.post(f"/assets/{car}/undo-latest", {"preview": True, "op_key": "u"})
    assert refused.status_code == 409 and "Beta Co" in refused.json()["detail"]
    assert (await b.asset(done["target"]["asset_id"]))["status"] == "active"


async def test_transfer_tax_value_from_the_related_party_rule(fa, zz):
    await _managed_from(fa, "2024-01-01")
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    await _map(fa, "Vehicles", {"zz_asset": "zz_plant"})
    car = await _contributed(fa, on="2025-01-15")
    await _run(fa, "2026-03")
    b = await _second_company(fa, managed="2026-01-01")
    await b.ok(await b.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    await _map(b, "Vehicles", {"zz_asset": "zz_plant"})
    done = await _transfer(fa, b, car, price="9000")
    # ZZ carries the written-down value across a related-party transfer, not the price.
    (mine,) = [r for r in (await _tax_book(fa, "zz_asset", 2026))["rows"] if r["asset_id"] == car]
    assert D(mine["disposals"]) == D("9600")
    theirs = done["target"]["asset_id"]
    (row,) = [r for r in (await _tax_book(b, "zz_asset", 2026))["rows"] if r["asset_id"] == theirs]
    assert D(row["additions"]) == D("9600")


async def _taxed_pair(fa: FA, *, mapped: bool = True) -> tuple[str, FA]:
    """A car in the ZZ asset regime here, tax years to 30 June, and Beta Co keeping ZZ too."""
    await _managed_from(fa, "2024-01-01")
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    if mapped:
        await _map(fa, "Vehicles", {"zz_asset": "zz_plant"})
    car = await _contributed(fa, on="2025-01-15")
    await _run(fa, "2026-03")
    b = await _second_company(fa, managed="2026-01-01")
    await b.ok(await b.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    await _map(b, "Vehicles", {"zz_asset": "zz_plant"})
    return car, b


async def _refused_untouched(fa: FA, b: FA, car: str, *needles: str) -> None:
    """The transfer is refused naming the regime and each of ``needles``, and neither company changed."""
    before = await _snapshot(fa, car)
    their_journals = sorted(je["entity_id"] for je in await b.module_journals())
    refused = await _refused_transfer(fa, b, car, price="9000")
    assert refused.status_code == 409, refused.text
    for needle in ("ZZ asset allowances", *needles):
        assert needle in refused.json()["detail"], refused.text
    done = await fa.post(f"/assets/{car}/transfer", {
        **_transfer_body(b, price="9000", class_id=(await b.classes())["Vehicles"]["id"]), "op_key": "t-refused", "fingerprint": "stale"})
    assert done.status_code == 409 and "ZZ asset allowances" in done.json()["detail"], done.text
    assert await _snapshot(fa, car) == before
    assert [a for a in await b.entities("fa_asset") if a.get("transferred_from")] == []
    assert sorted(je["entity_id"] for je in await b.module_journals()) == their_journals


async def test_f06_transfer_refused_while_the_carryover_cannot_be_worked_out(fa, zz, monkeypatch):
    from celerp_l10n_zz.rules import AssetRegime
    from test_l10n import _set_zz

    car, b = await _taxed_pair(fa, mapped=False)
    # No tax category: the price would silently become the tax value.
    await _refused_untouched(fa, b, car, "tax category")
    await _map(fa, "Vehicles", {"zz_asset": "zz_plant"})

    # The regime's own code fails.
    def broken(self, inputs):
        raise ValueError("no table for this year")
    monkeypatch.setattr(AssetRegime, "related_party_transfer", broken)
    await _refused_untouched(fa, b, car, "could not work this out")
    # Its result is not a value at all.
    monkeypatch.setattr(AssetRegime, "related_party_transfer", lambda self, inputs: None)
    await _refused_untouched(fa, b, car, "could not work this out")
    monkeypatch.undo()

    # The rule's module is turned off.
    await _set_zz(fa, False)
    await _refused_untouched(fa, b, car, "ZZ localization")
    # Back on, the same transfer goes through at the value ZZ carries across.
    await _set_zz(fa, True)
    done = await _transfer(fa, b, car, price="9000")
    (mine,) = [r for r in (await _tax_book(fa, "zz_asset", 2026))["rows"] if r["asset_id"] == car]
    assert D(mine["disposals"]) == D("9600")
    (row,) = [r for r in (await _tax_book(b, "zz_asset", 2026))["rows"]
              if r["asset_id"] == done["target"]["asset_id"]]
    assert D(row["additions"]) == D("9600")
    await fa.fold_matches(car)


async def test_f06_transfer_refused_into_a_closed_tax_year(fa, zz):
    from test_year_end import _accept_all

    car, b = await _taxed_pair(fa)
    for year in (2025, 2026):
        await _accept_all(fa, year)
        await fa.ok(await fa.post(f"/tax-years/zz_asset/{year}/close", {}))
    await _refused_untouched(fa, b, car, "2026", "closed")


async def test_f06_regime_declaring_transfer_price_carries_the_price(fa, zz, monkeypatch):
    from celerp_fixed_assets.calc.tax import TaxTransferResult
    from celerp_l10n_zz.rules import AssetRegime

    car, b = await _taxed_pair(fa)
    monkeypatch.setattr(AssetRegime, "related_party_transfer",
                        lambda self, inputs: TaxTransferResult(written_down_value=None))
    done = await _transfer(fa, b, car, price="9000")
    (mine,) = [r for r in (await _tax_book(fa, "zz_asset", 2026))["rows"] if r["asset_id"] == car]
    assert D(mine["disposals"]) == D("9000")
    (row,) = [r for r in (await _tax_book(b, "zz_asset", 2026))["rows"]
              if r["asset_id"] == done["target"]["asset_id"]]
    assert D(row["additions"]) == D("9000")


async def test_f06_carryover_converted_into_the_receiving_currency(fa, zz):
    car, b = await _taxed_pair(fa)
    r = await b.client.patch("/companies/me/books", headers=b.h, json={"currency": "EUR"})
    assert r.status_code == 200, r.text
    done = await _transfer(fa, b, car, price="9000", rate="0.9")
    (row,) = [r for r in (await _tax_book(b, "zz_asset", 2026))["rows"]
              if r["asset_id"] == done["target"]["asset_id"]]
    assert D(row["additions"]) == D("8640")
    assert D((await b.asset(done["target"]["asset_id"]))["cost"]) == D("8100")


async def test_s5_concurrent_transfers_one_wins(committed_engine):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company
    from celerp_fixed_assets import setup, transfer
    from test_helpers import provision_company_books

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = await _race_company(factory)
    bid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=bid, name="Beta Co", slug=f"beta-{bid.hex[:8]}", settings={"currency": "USD"}))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=bid, role="admin", is_active=True))
        await provision_company_books(s, bid)
        await s.commit()
    async with factory() as s:
        await setup.start(s, bid, uid)
        await setup.update_settings(s, bid, uid, {"first_managed_period": "2026-01-01"})
        await s.commit()
    async with factory() as s:
        (car,) = [a["id"] for a in await _assets(s, cid)]
        vehicles = next(c for c in await setup.classes(s, bid) if c["name"] == "Vehicles")["id"]
    body = {"date": "2026-04-20", "target_company_id": str(bid), "price": "11000", "receivable_account": "1120",
            "payable_account": "2110", "class_id": vehicles}

    async def attempt(key: str):
        async with factory() as s:
            preview = await transfer.transfer(s, cid, uid, car, {**body, "op_key": key, "preview": True})
            await s.rollback()
        async with factory() as s:
            try:
                done = await transfer.transfer(s, cid, uid, car, {**body, "op_key": key,
                                                                  "fingerprint": preview["fingerprint"]})
                await s.commit()
                return done
            except HTTPException as e:
                await s.rollback()
                return e

    outcomes = await asyncio.gather(attempt("one"), attempt("two"))
    wins = [o for o in outcomes if not isinstance(o, HTTPException)]
    assert len(wins) == 1, outcomes
    (lost,) = [o for o in outcomes if isinstance(o, HTTPException)]
    assert lost.status_code == 409
    async with factory() as s:
        moved = [a for a in await _assets(s, bid) if a.get("transferred_from")]
        assert len(moved) == 1


async def _assets(session, company_id) -> list[dict]:
    from celerp.models.projections import Projection

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_asset"))).scalars()
    return [{"id": r.entity_id, **r.state} for r in rows]


# ── Pages ────────────────────────────────────────────────────────────────────


async def test_asset_page_offers_move_reclassify_and_transfer(fa):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    await _second_company(fa)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    for key in ("fixed_assets.move", "fixed_assets.reclassify", "fixed_assets.transfer"):
        assert t(key) in page.text, key
    move = await fa.page(f"/fixed-assets/assets/{car}/move")
    assert move.status_code == 200 and t("fixed_assets.custodian_not_user") in move.text
    reclassify = await fa.page(f"/fixed-assets/assets/{car}/reclassify")
    assert reclassify.status_code == 200 and "Computer equipment" in reclassify.text
    transfer = await fa.page(f"/fixed-assets/assets/{car}/transfer")
    assert transfer.status_code == 200 and "Beta Co" in transfer.text
    # The company chosen to receive it reads as a choice under its label, not as a lone action.
    chosen = re.sub(r"\s*([<>])\s*", r"\1", transfer.text)
    assert re.search(r'<label class="form-label">Transfer to</label><div class="flex-row[^"]*"><a [^>]*'
                     r'aria-current="true"[^>]*>Beta Co</a>', chosen), chosen
    assert t("fixed_assets.transfer_taxable_hint") in transfer.text
    # Both companies keep books in the default currency, so the price is in USD and no rate is asked.
    assert t("fixed_assets.transfer_price_hint", currency="USD") in transfer.text
    assert 'name="rate"' not in transfer.text
    register = await fa.page("/fixed-assets")
    assert t("fixed_assets.move") in register.text
    assert "fixed_assets." not in page.text + move.text + reclassify.text + transfer.text


async def test_transfer_reviewed_and_confirmed_from_its_page(fa):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    b = await _second_company(fa)
    vehicles = (await b.classes())["Vehicles"]["id"]
    form = {**_transfer_body(b, class_id=vehicles), "asset_id": car, "idempotency_key": str(uuid.uuid4())}
    review = await fa.submit("/fixed-assets/op/transfer/review", form)
    assert review.status_code == 200, review.text
    # Both companies' journals are shown, each under its own heading.
    assert t("fixed_assets.transfer_here") in review.text
    assert t("fixed_assets.transfer_there", company="Beta Co") in review.text
    fingerprint = re.search(r'name="fingerprint" value="([^"]+)"', review.text).group(1)
    done = await fa.submit("/fixed-assets/op/transfer/confirm", {**form, "fingerprint": fingerprint})
    assert done.headers.get("HX-Redirect") == f"/fixed-assets/assets/{car}", done.text
    theirs = [a for a in await b.entities("fa_asset") if a.get("transferred_from")][0]
    page = await b.page(f"/fixed-assets/assets/{theirs['entity_id']}")
    assert t("fixed_assets.transferred_from_title") in page.text and "Demo Co" in page.text
    assert t("fixed_assets.movement.contributed") in page.text
    # Its origin is the transfer, not a hand entry.
    assert t("fixed_assets.no_sources") not in page.text
    ours = await fa.page(f"/fixed-assets/assets/{car}")
    assert "Beta Co" in ours.text and t("fixed_assets.undo_disposal") not in ours.text
    assert t("fixed_assets.undo_latest_hint", change=t("fixed_assets.movement.transfer")) in ours.text


async def test_move_reviewed_from_the_register_selection(fa):
    from ui.i18n import t

    await _managed_from(fa)
    car = await _contributed(fa)
    picked = await fa.submit("/fixed-assets/move/selected", {"selected": [car]})
    assert picked.headers["HX-Redirect"].startswith("/fixed-assets/move?ids=")
    page = await fa.page("/fixed-assets/move", ids=car)
    assert page.status_code == 200 and t("fixed_assets.move_several_title", n=1) in page.text
    form = {"asset_ids": car, "date": "2026-02-01", "custodian_name": "Night shift lead",
            "idempotency_key": str(uuid.uuid4())}
    review = await fa.submit("/fixed-assets/op/move/review", form)
    assert "Night shift lead" in review.text and t("fixed_assets.posts_nothing") in review.text
    # A move carries no amounts, so the review shows no amount column.
    assert t("fixed_assets.amount") not in review.text
    fingerprint = re.search(r'name="fingerprint" value="([^"]+)"', review.text).group(1)
    done = await fa.submit("/fixed-assets/op/move/confirm", {**form, "fingerprint": fingerprint})
    assert done.headers.get("HX-Redirect") == "/fixed-assets", done.text
    asset_page = await fa.page(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.placement_title") in asset_page.text and "Night shift lead" in asset_page.text
    assert t("fixed_assets.undo_move") in asset_page.text
