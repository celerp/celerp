# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Importing an opening register: rows from a file become opening assets at the cutover.

Every scenario starts from a demo company whose first managed period is confirmed from
the suggestion, so its asset bills fall after the cutover and wait in intake.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from fa_fixtures import FA

pytestmark = pytest.mark.asyncio

D = Decimal


async def _cutover(fa: FA) -> str:
    return (await fa.ok(await fa.get("/setup")))["settings"]["cutover"]


async def _preview(fa: FA, rows: list[dict], **body) -> dict:
    return await fa.ok(await fa.post("/imports", {"rows": rows, "as_at": await _cutover(fa), "preview": True,
                                                  "op_key": f"imp-{uuid.uuid4().hex[:8]}", **body}))


async def _import(fa: FA, rows: list[dict], **body) -> dict:
    return await fa.operate("/imports", {"rows": rows, "as_at": await _cutover(fa), **body},
                            key=f"imp-{uuid.uuid4().hex[:8]}")


async def _batch(fa: FA, batch_id: str, **params) -> dict:
    return await fa.ok(await fa.get(f"/imports/{batch_id}", **params))


def _building(cut: str, **values) -> dict:
    return {"name": "Warehouse", "class": "Buildings", "available_date": "2020-01-01", "cost": "250000",
            "accumulated_depreciation": "40000", **values}


def _errors(preview: dict) -> dict[tuple[int, str], str]:
    return {(e["row"], e["field"]): e["message"] for e in preview["errors"]}


def _account(accounts: list[dict], code: str) -> dict:
    (found,) = [a for a in accounts if a["account"] == code]
    return found


# ── Imports never claim ──────────────────────────────────────────────────────


async def test_s1_import_never_claims_source_lines(fa):
    cut = await _cutover(fa)
    van = await fa.line("Delivery van")
    rows = [{"name": "Delivery van", "class": "Vehicles", "available_date": cut, "cost": van["amount"]},
            {"name": "Delivery van", "class": "Vehicles", "available_date": van["date"], "cost": van["amount"]}]
    preview = await _preview(fa, rows)
    assert f"after the cutover {cut}" in _errors(preview)[(2, "available_date")]
    assert preview["counts"]["create"] == 1 and preview["counts"]["blocked"] == 1
    done = await _import(fa, rows, exclude=[2])
    (asset,) = done["assets"]
    (mv,) = [m for m in await fa.entities("fa_movement") if m["asset_id"] == asset["id"]]
    assert mv["kind"] == "opening" and mv["posting_date"] == cut and mv["contributions"] == []
    assert mv["provenance"] == done["batch"]["id"]
    assert await fa.entities("fa_src") == [] and await fa.module_journals() == []
    assert (await fa.line("Delivery van"))["source"] == van["source"]
    state = await fa.asset(asset["id"])
    assert state["sources"] == [] and D(state["cost"]) == D(van["amount"])


# ── Row errors ───────────────────────────────────────────────────────────────


async def test_j5_row_errors_named(fa):
    cut = await _cutover(fa)
    vehicles = (await fa.classes())["Vehicles"]["id"]
    held = await fa.operate("/assets", {"kind": "opening", "class_id": vehicles, "name": "Car",
                                        "available_date": cut, "cost": "9000"}, key="held")
    car = held["assets"][0]
    await fa.ok(await fa.patch(f"/assets/{car['id']}", {"changes": {"tag": "CAR-1"}, "previous": {"tag": None}}))
    late = (date.fromisoformat(cut) + timedelta(days=1)).isoformat()
    rows = [
        _building(cut, **{"class": "Spaceships"}),
        _building(cut, cost="-5"),
        _building(cut, accumulated_depreciation="260000"),
        _building(cut, number="A-1"),
        _building(cut, number="a-1"),
        _building(cut, tag="car-1"),
        _building(cut, location="Moon base"),
        _building(cut, revaluation_surplus="1000"),
        _building(cut, name=""),
        _building(cut, acquired_date=late),
        _building(cut, parent_number="FA-9999"),
        _building(cut, number=car["number"]),
        _building(cut, available_date="31/31/2020"),
        _building(cut, method="sum_of_digits"),
    ]
    errors = _errors(await _preview(fa, rows))
    assert "Spaceships" in errors[(1, "class")]
    assert "Cost" in errors[(2, "cost")]
    assert "cannot be more than the cost" in errors[(3, "accumulated_depreciation")]
    assert (4, "number") not in errors and "A-1 is on row 4" in errors[(5, "number")]
    assert f"CAR-1 is already on {car['number']}" in errors[(6, "tag")]
    assert "Moon base" in errors[(7, "location")]
    assert "revaluation" in errors[(8, "revaluation_surplus")]
    assert "name" in errors[(9, "name")]
    assert f"after the cutover {cut}" in errors[(10, "acquired_date")]
    assert "FA-9999" in errors[(11, "parent_number")]
    assert f"{car['number']} is already" in errors[(12, "number")]
    assert (13, "available_date") in errors
    assert (14, "method") in errors
    # A confirm with rows still in error is refused, whatever its fingerprint.
    preview = await _preview(fa, rows[:2])
    refused = await fa.post("/imports", {"rows": rows[:2], "as_at": cut, "op_key": "bad",
                                         "fingerprint": preview["fingerprint"]})
    assert refused.status_code == 422 and "Fix or leave out" in refused.json()["detail"]


async def test_j5_as_at_must_be_the_cutover(fa):
    cut = await _cutover(fa)
    other = (date.fromisoformat(cut) - timedelta(days=1)).isoformat()
    refused = await fa.post("/imports", {"rows": [_building(cut)], "as_at": other, "preview": True, "op_key": "x"})
    assert refused.status_code == 422 and f"as at the cutover {cut}" in refused.json()["detail"]
    empty = await fa.post("/imports", {"rows": [], "as_at": cut, "preview": True, "op_key": "x"})
    assert empty.status_code == 422


# ── A clean subset, then Fix and import rows ─────────────────────────────────


async def test_j5_clean_subset_then_fix_and_import(fa):
    cut = await _cutover(fa)
    rows = [_building(cut), _building(cut, name="Garage", cost="50000", accumulated_depreciation="0",
                                      **{"class": "Garages"})]
    done = await _import(fa, rows, exclude=[2])
    batch_id = done["batch"]["id"]
    batch = await _batch(fa, batch_id)
    assert batch["counts"] == {"imported": 1, "rejected": 1, "history": 0} and batch["state"] == "imported"
    rejected = await _batch(fa, batch_id, status="rejected")
    (row,) = rejected["rows"]
    assert row["row"] == 2 and row["values"]["name"] == "Garage" and "Garages" in row["errors"][0]["message"]
    assert D(_account(batch["accounts"], "1210")["registered"]) == D("250000")
    fixed = {"rows": [{"row": 2, "values": {**row["values"], "class": "Buildings"}}]}
    preview = await fa.ok(await fa.post(f"/imports/{batch_id}/rows", {**fixed, "preview": True, "op_key": "fix"}))
    assert preview["errors"] == [] and preview["counts"]["create"] == 1
    again = await fa.ok(await fa.post(f"/imports/{batch_id}/rows", {
        **fixed, "op_key": "fix", "fingerprint": preview["fingerprint"]}))
    (garage,) = again["assets"]
    batch = await _batch(fa, batch_id)
    assert batch["counts"] == {"imported": 2, "rejected": 0, "history": 0}
    assert D(_account(batch["accounts"], "1210")["registered"]) == D("300000")
    assert D(batch["totals"]["1210"]) == D("300000") and D(batch["totals"]["1220"]) == D("-40000")
    (mv,) = [m for m in await fa.entities("fa_movement") if m["asset_id"] == garage["id"]]
    assert mv["provenance"] == batch_id
    # A row already imported is not imported twice.
    twice = await fa.post(f"/imports/{batch_id}/rows", {**fixed, "preview": True, "op_key": "fix2"})
    assert twice.status_code == 422 and "row 2" in twice.json()["detail"].lower()


async def test_j5_numbers_from_the_file_and_assigned(fa):
    cut = await _cutover(fa)
    done = await _import(fa, [_building(cut, number="FA-0007"), _building(cut, name="Annex")])
    numbers = sorted(a["number"] for a in done["assets"])
    assert numbers == ["FA-0007", "FA-0008"]


# ── Preview: reconciliation and possible duplicates ──────────────────────────


async def test_j5_preview_reconciles_and_flags_duplicates(fa):
    cut = await _cutover(fa)
    vehicles = (await fa.classes())["Vehicles"]["id"]
    held = (await fa.operate("/assets", {"kind": "opening", "class_id": vehicles, "name": "Car",
                                         "available_date": cut, "cost": "9000"}, key="held"))["assets"][0]
    await fa.ok(await fa.patch(f"/assets/{held['id']}", {"changes": {"serial": "SN-1"}, "previous": {"serial": None}}))
    rows = [{"name": "Truck", "class": "Vehicles", "available_date": cut, "cost": "30000", "serial": "sn-1"},
            _building(cut), _building(cut)]
    preview = await _preview(fa, rows)
    assert preview["errors"] == []
    flagged = {d["row"]: d["message"] for d in preview["duplicates"]}
    assert held["number"] in flagged[1] and "SN-1" in flagged[1].upper()
    assert "row 2" in flagged[3].lower()
    accounts = preview["accounts"]
    # Buildings and vehicles share the fixed asset account: two buildings and the truck.
    fixed = _account(accounts, "1210")
    assert D(fixed["importing"]) == D("530000")
    assert D(fixed["difference"]) == D(fixed["ledger"]) - D(fixed["registered"]) - D("530000")
    assert D(_account(accounts, "1220")["importing"]) == D("-80000")
    assert [a["row"] for a in preview["assets"]] == [1, 2, 3]


# ── Undo import ──────────────────────────────────────────────────────────────


async def _undo(fa: FA, batch_id: str, key: str = "undo-imp"):
    preview = await fa.post(f"/imports/{batch_id}/undo", {"preview": True, "op_key": key})
    if preview.status_code != 200:
        return preview
    return await fa.post(f"/imports/{batch_id}/undo", {"op_key": key, "fingerprint": preview.json()["fingerprint"]})


async def test_j5_undo_import_removes_the_batch(fa):
    cut = await _cutover(fa)
    done = await _import(fa, [_building(cut), _building(cut, name="Annex")])
    batch_id = done["batch"]["id"]
    undone = await fa.ok(await _undo(fa, batch_id))
    assert undone["undone"] == batch_id
    assert {(await fa.asset(a["id"]))["status"] for a in done["assets"]} == {"undone"}
    batch = await _batch(fa, batch_id)
    assert batch["state"] == "undone"
    assert D(_account(batch["accounts"], "1210")["registered"]) == 0
    again = await _undo(fa, batch_id, key="undo-2")
    assert again.status_code == 409


async def test_j5_undo_import_refused_naming_changed_assets(fa):
    cut = await _cutover(fa)
    done = await _import(fa, [_building(cut), _building(cut, name="Annex")])
    warehouse, annex = done["assets"]
    await fa.operate(f"/assets/{annex['id']}/correct", {
        "changes": {"opening_values": {"cost": "250000", "accumulated_depreciation": "41000"}}}, key="fix-annex")
    refused = await _undo(fa, done["batch"]["id"])
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert annex["number"] in detail and warehouse["number"] not in detail and "Correct" in detail
    assert (await fa.asset(warehouse["id"]))["status"] == "active"


# ── History rows, policy, parents, places and people ─────────────────────────


async def test_j5_disposed_rows_are_history(fa):
    cut = await _cutover(fa)
    late = (date.fromisoformat(cut) + timedelta(days=1)).isoformat()
    rows = [_building(cut, name="Old shed", disposed_date="2019-06-30", disposal_proceeds="100"),
            _building(cut, name="New shed", disposed_date=late)]
    preview = await _preview(fa, rows)
    assert f"after the cutover {cut}" in _errors(preview)[(2, "disposed_date")]
    assert preview["counts"]["history"] == 1 and preview["counts"]["create"] == 0
    done = await _import(fa, rows, exclude=[2])
    assert done["assets"] == []
    history = await _batch(fa, done["batch"]["id"], status="history")
    assert [r["values"]["name"] for r in history["rows"]] == ["Old shed"]
    assert all(a.get("name") != "Old shed" for a in await fa.entities("fa_asset"))


async def test_j5_policy_parent_place_and_custodian(fa):
    cut = await _cutover(fa)
    office = await fa.ok(await fa.client.post("/companies/me/locations", headers=fa.h,
                                              json={"name": "North yard", "type": "warehouse"}))
    me = (await fa.ok(await fa.client.get("/companies/me/users", headers=fa.h)))["items"][0]
    rows = [_building(cut, number="B-1", life_months="480", residual_percent="5", location="north YARD",
                      custodian=me["email"], tag="BLD-1"),
            {"name": "Roof", "class": "Buildings", "available_date": "2020-01-01", "cost": "20000",
             "parent_number": "B-1", "custodian": "Site manager"}]
    done = await _import(fa, rows)
    building, roof = [await fa.asset(a["id"]) for a in done["assets"]]
    assert building["policy"]["life_months"] == 480 and building["policy"]["residual_percent"] == "5"
    assert building["location_id"] == office["id"] and building["tag"] == "BLD-1"
    assert building["custodian"]["user_id"] == me["id"]
    assert roof["parent_id"] == done["assets"][0]["id"]
    assert roof["custodian"] == {"name": "Site manager", "not_user": True}


# ── The import screens ───────────────────────────────────────────────────────


async def test_j14_import_page_turns_the_module_on(fa):
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    loaded = get_enabled(company.settings) | {"celerp-accounting", "celerp-docs", "celerp-inventory"}
    company.settings = set_enabled(dict(company.settings or {}), loaded - {"celerp-fixed-assets"})
    await fa.session.commit()
    page = await fa.page("/import/asset-register")
    assert page.status_code == 200
    assert "Turn on Fixed assets to import the asset register" in page.text
    assert "/modules/celerp-fixed-assets/enable" in page.text
    company = await locked_company(fa.session, fa.auth["company_id"])
    company.settings = set_enabled(dict(company.settings or {}), loaded | {"celerp-fixed-assets"})
    await fa.session.commit()
    on = await fa._ui("GET", "/import/asset-register", follow_redirects=False)
    assert on.status_code in (302, 303) and on.headers["location"] == "/fixed-assets/import"


async def test_j5_wizard_upload_review_import(fa):
    cut = await _cutover(fa)
    template = await fa.page("/fixed-assets/import/template")
    assert template.status_code == 200 and template.text.splitlines()[0].startswith("number,name,class")
    csv_text = ("Asset,Name,Class,In use from,Cost,Depreciation to date\n"
                "FA-0050,Warehouse,Buildings,2020-01-01,250000,40000\n"
                "FA-0051,Garage,Garages,2020-01-01,50000,0\n")
    mapped = await fa._ui("POST", "/fixed-assets/import/preview",
                          files={"csv_file": ("register.csv", csv_text.encode(), "text/csv")})
    assert mapped.status_code == 200 and "csv_ref" in mapped.text
    csv_ref = mapped.text.split('name="csv_ref" value="', 1)[1].split('"', 1)[0]
    form = {"csv_ref": csv_ref, "map__Asset": "number", "map__Name": "name", "map__Class": "class",
            "map__In use from": "available_date", "map__Cost": "cost",
            "map__Depreciation to date": "accumulated_depreciation"}
    review = await fa._ui("POST", "/fixed-assets/import/mapped", data=form, follow_redirects=True)
    assert review.status_code == 200 and "Garages" in review.text
    draft_ref = str(review.url).rsplit("/", 1)[1]
    revision = review.text.split('name="revision" value="', 1)[1].split('"', 1)[0]
    excluded = await fa.submit("/fixed-assets/import/review", {"csv_ref": draft_ref, "revision": revision,
                                                                "exclude": "2", "fixes_json": ""})
    assert 'name="preview_hash"' in excluded.text
    preview_hash = excluded.text.split('name="preview_hash" value="', 1)[1].split('"', 1)[0]
    done = await fa.submit("/fixed-assets/import/confirm", {"csv_ref": draft_ref, "preview_hash": preview_hash})
    assert done.status_code == 200
    batch_id = next(b["id"] for b in (await fa.ok(await fa.get("/imports")))["items"])
    assert batch_id.removeprefix("fa:import:") in done.headers.get("hx-redirect", "") + done.text
    page = await fa.page(f"/fixed-assets/imports/{batch_id}")
    assert page.status_code == 200 and "Garage" in page.text and "FA-0050" in page.text
    assert "Fix and import rows" in page.text and "Undo import" in page.text
    assert cut in page.text
