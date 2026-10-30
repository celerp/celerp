# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Revaluation, impairment and its reversal, the class measurement policy and the annual
review of an intangible with an indefinite life.

Land is not depreciated, so its examples read straight off the fair values. A contributed
car (12,000 over 60 months, straight line, full month, from January 2026) charges 200 a
month, so its carrying amounts can be read off the calendar.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _lines, _managed_from, _movements, _notices, _run, _tick

pytestmark = pytest.mark.asyncio

D = Decimal


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _rows(values: dict[str, str]) -> list[dict]:
    return [{"asset_id": a, "fair_value": v, "basis": "Valuer report"} for a, v in values.items()]


async def _revalue_class(fa: FA, class_name: str, on: str, values: dict[str, str]) -> dict:
    cls = (await fa.classes())[class_name]
    return await fa.operate(f"/classes/{cls['id']}/revalue", {"date": on, "rows": _rows(values)},
                            key=_key("rc"))


async def _revalue(fa: FA, asset_id: str, on: str, value: str) -> dict:
    return await fa.operate(f"/assets/{asset_id}/revalue", {"date": on, "fair_value": value,
                                                            "basis": "Valuer report"}, key=_key("rv"))


async def _impair(fa: FA, asset_id: str, on: str, value: str) -> dict:
    return await fa.operate(f"/assets/{asset_id}/impair", {"date": on, "recoverable_amount": value,
                                                           "basis": "Market quote"}, key=_key("im"))


async def _reverse(fa: FA, asset_id: str, on: str, value: str) -> dict:
    return await fa.operate(f"/assets/{asset_id}/reverse-impairment", {"date": on, "recoverable_amount": value,
                                                                       "basis": "Market quote"}, key=_key("ri"))


async def _latest_journal(fa: FA, asset_id: str) -> dict:
    moves = await _movements(fa, asset_id)
    return await fa.state(moves[-1]["journal_id"])


async def test_u37_an_impairment_brings_depreciation_up_to_date_as_its_own_movement(fa):
    """Owed depreciation an impairment charges first is a Depreciation movement, not part of
    the Impairment; undoing the impairment undoes both."""
    await _managed_from(fa)
    car = await _contributed(fa)
    await _impair(fa, car, "2026-06-30", "8000")
    moves = await _movements(fa, car)
    assert await fa.charges_apart(car) == ["contributed", "depreciation", "impairment"]
    caught_up = moves[1]
    assert D(caught_up["amounts"]["accumulated_depreciation"]) == sum(D(v) for v in caught_up["periods"].values()) > 0
    assert caught_up["journal_id"] == moves[2]["journal_id"] and caught_up["op_key"] == moves[2]["op_key"]
    assert D(moves[2]["amounts"].get("accumulated_depreciation") or 0) == 0
    await fa.fold_matches(car)
    await fa.operate(f"/assets/{car}/undo-latest", {}, key=_key("ul"))
    got = await fa.asset(car)
    assert D(got["accumulated_depreciation"]) == 0 and D(got["accumulated_impairment"]) == 0
    await fa.fold_matches(car)


async def test_revaluation_reversal_pnl_first_then_surplus(fa):
    await _managed_from(fa)
    land = await _contributed(fa, "Plot", cls="Land", cost="600")
    await _revalue_class(fa, "Land", "2026-03-31", {land: "500"})
    assert _lines(await _latest_journal(fa, land)) == {"6410": (D(100), D(0)), "1210": (D(0), D(100))}
    await _revalue(fa, land, "2026-06-30", "650")
    assert _lines(await _latest_journal(fa, land)) == {"1210": (D(150), D(0)), "6410": (D(0), D(100)),
                                                         "3400": (D(0), D(50))}
    got = await fa.asset(land)
    assert D(got["cost"]) == 650 and D(got["revaluation_surplus"]) == 50
    await _revalue(fa, land, "2026-09-30", "560")
    assert _lines(await _latest_journal(fa, land)) == {"3400": (D(50), D(0)), "6410": (D(40), D(0)),
                                                         "1210": (D(0), D(90))}
    got = await fa.asset(land)
    assert D(got["cost"]) == 560 and D(got["revaluation_surplus"]) == 0
    assert await fa.balance("6410") == D(40) and await fa.balance("3400") == 0
    await fa.fold_matches(land)


async def test_s2_impairment_on_revalued_asset_surplus_first_reversal_pnl_then_surplus(fa):
    await _managed_from(fa)
    land = await _contributed(fa, "Warehouse plot", cls="Land", cost="850")
    await _revalue_class(fa, "Land", "2026-03-31", {land: "1000"})
    assert _lines(await _latest_journal(fa, land)) == {"1210": (D(150), D(0)), "3400": (D(0), D(150))}
    await _impair(fa, land, "2026-06-30", "700")
    assert _lines(await _latest_journal(fa, land)) == {"3400": (D(150), D(0)), "6410": (D(150), D(0)),
                                                         "1220": (D(0), D(300))}
    got = await fa.asset(land)
    assert D(got["accumulated_impairment"]) == 300 and D(got["revaluation_surplus"]) == 0
    await _reverse(fa, land, "2026-09-30", "900")
    assert _lines(await _latest_journal(fa, land)) == {"1220": (D(200), D(0)), "6410": (D(0), D(150)),
                                                         "3400": (D(0), D(50))}
    got = await fa.asset(land)
    assert D(got["accumulated_impairment"]) == 100 and D(got["revaluation_surplus"]) == 50
    assert D(got["nbv"]) == 900
    await fa.fold_matches(land)


async def test_s2_impairment_reversal_capped(fa):
    """Impaired to 8,550 at the end of March (carrying 11,400, so 2,850 lost), then 150 a
    month over the 57 months left; at the end of June the carrying amount is 8,100 and the
    car would have carried 10,800 had it never been impaired, so the reversal stops at
    2,700 and the car depreciates at 200 a month again."""
    await _managed_from(fa)
    car = await _contributed(fa)
    r = await fa.post(f"/assets/{car}/reverse-impairment", {"date": "2026-02-28", "recoverable_amount": "13000",
                                                            "preview": True, "op_key": "ri0"})
    assert r.status_code == 422 and "no impairment" in r.json()["detail"]
    await _impair(fa, car, "2026-03-31", "8550")
    assert _lines(await _latest_journal(fa, car)) == {"6400": (D(600), D(0)), "6410": (D(2850), D(0)),
                                                        "1220": (D(0), D(3450))}
    preview = await fa.ok(await fa.post(f"/assets/{car}/reverse-impairment", {
        "date": "2026-06-30", "recoverable_amount": "12000", "preview": True, "op_key": "ri1"}))
    assert D(preview["reversal"]) == D(2700) and D(preview["limit"]) == D(10800)
    await _reverse(fa, car, "2026-06-30", "12000")
    assert _lines(await _latest_journal(fa, car)) == {"6400": (D(450), D(0)), "1220": (D(2700), D(450)),
                                                        "6410": (D(0), D(2700))}
    got = await fa.asset(car)
    assert D(got["nbv"]) == D(10800) and D(got["accumulated_impairment"]) == D(150)
    await fa.fold_matches(car)
    await _run(fa, "2026-07")
    charged = [m for m in await _movements(fa, car) if m["operation"] == "run"]
    assert [D(m["amounts"]["accumulated_depreciation"]) for m in charged] == [D(200)]


async def test_s2_class_revaluation_table(fa):
    """Three buildings of 48,000 over 480 months (100 a month from January): carrying
    47,700 each at the end of March."""
    await _managed_from(fa)
    ids = [await _contributed(fa, f"Building {n}", cls="Buildings", cost="48000") for n in "ABC"]
    numbers = [(await fa.asset(a))["number"] for a in ids]
    cls = (await fa.classes())["Buildings"]
    single = await fa.post(f"/assets/{ids[0]}/revalue", {"date": "2026-03-31", "fair_value": "50000",
                                                         "preview": True, "op_key": "one"})
    assert single.status_code == 422
    assert "This class uses the cost model" in single.json()["detail"] and "all 3 assets" in single.json()["detail"]
    path = f"/classes/{cls['id']}/revalue"
    partial = {"date": "2026-03-31", "rows": _rows({ids[0]: "50000", ids[1]: "47000"})}
    preview = await fa.ok(await fa.post(path, {**partial, "preview": True, "op_key": "cr1"}))
    assert preview["missing"] == [numbers[2]]
    refused = await fa.post(path, {**partial, "preview": False, "op_key": "cr1",
                                   "fingerprint": preview["fingerprint"]})
    assert refused.status_code == 422 and numbers[2] in refused.json()["detail"]
    full = {"date": "2026-03-31", "rows": _rows({ids[0]: "50000", ids[1]: "47000", ids[2]: "47700"})}
    preview = await fa.ok(await fa.post(path, {**full, "preview": True, "op_key": "cr2"}))
    assert preview["missing"] == []
    assert {k: D(v) for k, v in preview["totals"].items()} == {
        "carrying_amount": D(143100), "fair_value": D(144700), "surplus": D(2300), "profit_or_loss": D(-700)}
    done = await fa.ok(await fa.post(path, {**full, "preview": False, "op_key": "cr2",
                                            "fingerprint": preview["fingerprint"]}))
    assert done["totals"] == preview["totals"]
    assert [D((await fa.asset(a))["nbv"]) for a in ids] == [D(50000), D(47000), D(47700)]
    assert (await fa.classes())["Buildings"]["measurement_policy"] == "revaluation"
    op = await fa.state("fa:op:cr2")
    assert len(op["effects"]["journals"]) == 3
    assert await fa.balance("3400") == D(-2300) and await fa.balance("6410") == D(700)
    back = await fa.patch(f"/classes/{cls['id']}", {"measurement_policy": "cost"})
    assert back.status_code == 409 and "revalued" in back.json()["detail"]
    vehicles = (await fa.classes())["Vehicles"]
    direct = await fa.patch(f"/classes/{vehicles['id']}", {"measurement_policy": "revaluation"})
    assert direct.status_code == 422 and "Change class to revaluation" in direct.json()["detail"]
    await _revalue(fa, ids[0], "2026-06-30", "51000")


async def test_s2_intangible_indefinite_and_active_market(fa):
    await _managed_from(fa)
    other = (await fa.classes())["Other intangibles"]
    await fa.ok(await fa.patch(f"/classes/{other['id']}", {"method": "none", "life_months": None}))
    licence = await _contributed(fa, "Broadcast licence", cls="Other intangibles", cost="5000", on="2025-09-01")
    number = (await fa.asset(licence))["number"]
    due = (await fa.ok(await fa.get("/reviews")))["items"]
    assert [(r["number"], r["due"], r["overdue"]) for r in due] == [(number, "2026-09-01", True)]
    await _tick(fa, date(2026, 10, 16))
    assert any(n.title == "Impairment reviews due" for n in await _notices(fa))
    nothing = await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "r3"})
    assert nothing.status_code == 422 and "Nothing to depreciate" in nothing.json()["detail"], nothing.text
    await fa.ok(await fa.post(f"/assets/{licence}/reviews", {"date": "2026-10-01", "outcome": "no_indication",
                                                             "basis": "Licence renewed to 2036"}))
    assert (await fa.ok(await fa.get("/reviews")))["items"] == []
    assert (await fa.asset(licence))["review_due"] == "2027-10-01"
    await _tick(fa, date(2026, 10, 17))
    assert not any(n.title == "Impairment reviews due" for n in await _notices(fa))
    found = await fa.ok(await fa.post(f"/assets/{licence}/reviews", {
        "date": "2026-10-05", "outcome": "recoverable_amount", "recoverable_amount": "4000",
        "basis": "Value in use"}))
    assert found["impairment"] == {"date": "2026-10-05", "recoverable_amount": "4000.00", "basis": "Value in use"}
    path = f"/classes/{other['id']}/revalue"
    body = {"date": "2026-10-05", "rows": _rows({licence: "6000"}), "preview": True, "op_key": "am"}
    refused = await fa.post(path, body)
    assert refused.status_code == 422 and "active market" in refused.json()["detail"]
    await fa.ok(await fa.patch(f"/classes/{other['id']}", {
        "active_market": {"date": "2026-10-01", "basis": "Quoted on the licence exchange"}}))
    await fa.ok(await fa.post(path, body))


async def test_only_the_latest_review_is_withdrawn_and_the_earlier_one_sets_the_due_date_again(fa):
    await _managed_from(fa)
    other = (await fa.classes())["Other intangibles"]
    await fa.ok(await fa.patch(f"/classes/{other['id']}", {"method": "none", "life_months": None}))
    licence = await _contributed(fa, "Broadcast licence", cls="Other intangibles", cost="5000", on="2025-09-01")
    first = await fa.ok(await fa.post(f"/assets/{licence}/reviews", {
        "date": "2026-09-01", "outcome": "no_indication", "basis": "Licence renewed"}))
    latest = await fa.ok(await fa.post(f"/assets/{licence}/reviews", {
        "date": "2026-10-01", "outcome": "no_indication", "basis": "Entered twice"}))
    refused = await fa.post(f"/assets/{licence}/reviews/{first['id']}/withdraw", {})
    assert refused.status_code == 409, refused.text
    assert await fa.ok(await fa.post(f"/assets/{licence}/reviews/{latest['id']}/withdraw", {})) == {
        "id": latest["id"], "status": "withdrawn"}
    assert (await fa.asset(licence))["review_due"] == "2027-09-01"


async def test_corrections_do_not_restate_across_a_remeasurement(fa):
    """Impaired at the end of March: an error correction would restate what the impairment
    measured, so it is refused; a change in estimate from April on is still made."""
    await _managed_from(fa)
    car = await _contributed(fa)
    await _impair(fa, car, "2026-03-31", "8550")
    path = f"/assets/{car}/correct"
    error = await fa.post(path, {"changes": {"cost": "13000"}, "preview": True, "op_key": "c1"})
    assert error.status_code == 422 and "remeasured on 2026-03-31" in error.json()["detail"]
    early = await fa.post(path, {"changes": {"life_months": 48}, "classification": "estimate",
                                 "effective_date": "2026-03-01", "preview": True, "op_key": "c2"})
    assert early.status_code == 422, early.text
    await fa.ok(await fa.post(path, {"changes": {"life_months": 48}, "classification": "estimate",
                                     "effective_date": "2026-04-01", "preview": True, "op_key": "c3"}))


# ── Class revaluation: the valuer's report and fair values from a file ──────


PDF = b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"


async def _buildings(fa: FA, count: int = 2) -> tuple[dict, list[str], list[str]]:
    await _managed_from(fa)
    ids = [await _contributed(fa, f"Building {n}", cls="Buildings", cost="48000") for n in "ABC"[:count]]
    return (await fa.classes())["Buildings"], ids, [(await fa.asset(a))["number"] for a in ids]


async def test_valuer_report_attached_to_every_asset_in_the_class_and_removed(fa):
    cls, ids, _ = await _buildings(fa)
    path = f"/fixed-assets/api/classes/{cls['id']}/valuer-report"
    r = await fa.client.post(path, headers=fa.h, files={"file": ("valuation.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["filename"] == "valuation.pdf" and sorted(f["asset_id"] for f in report["files"]) == sorted(ids)
    for asset_id in ids:
        assert [(f["filename"], f["document_tag"]) for f in (await fa.asset(asset_id))["files"]] == [
            ("valuation.pdf", "valuations")]
    await fa.ok(await fa.post(f"/classes/{cls['id']}/valuer-report/remove", {"files": report["files"]}))
    assert [(await fa.asset(a))["files"] for a in ids] == [[] for _ in ids]
    land = (await fa.classes())["Land"]
    empty = await fa.client.post(f"/fixed-assets/api/classes/{land['id']}/valuer-report", headers=fa.h,
                                 files={"file": ("valuation.pdf", PDF, "application/pdf")})
    assert empty.status_code == 422 and "No assets in use" in empty.json()["detail"]
    stranger = await fa.post(f"/classes/{cls['id']}/valuer-report/remove",
                             {"files": [{"asset_id": ids[0], "file_id": "nope"}]})
    assert stranger.status_code == 404


async def test_fair_values_from_a_file_fill_the_class_table(fa):
    from ui.i18n import t

    cls, ids, numbers = await _buildings(fa, 3)
    csv = (f"Asset no,Fair value,Basis\n{numbers[0]},50000,Valuer A\n{numbers[1]},47 000.50,\n"
           f"FA-9999,1,\n{numbers[2]},abc,\n")
    r = await fa._ui("POST", f"/fixed-assets/classes/{cls['id']}/revalue/fair-values",
                     files={"csv_file": ("values.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    assert f'name="fair_value:{ids[0]}"' in r.text and 'value="50000"' in r.text and 'value="Valuer A"' in r.text
    assert 'value="47000.50"' in r.text
    assert "FA-9999" in r.text and numbers[2] in r.text and "flash--warning" in r.text
    assert t("fixed_assets.fair_values_filled", count=2) in r.text and "fixed_assets." not in r.text
    r = await fa._ui("POST", f"/fixed-assets/classes/{cls['id']}/revalue/fair-values",
                     files={"csv_file": ("values.csv", b"", "text/csv")})
    assert r.status_code == 200 and "flash--error" in r.text


async def test_valuer_report_on_the_class_revalue_page(fa):
    from ui.i18n import t

    cls, ids, _ = await _buildings(fa)
    page = await fa.page(f"/fixed-assets/classes/{cls['id']}/revalue")
    assert t("fixed_assets.valuer_report") in page.text and t("fixed_assets.fair_values_file") in page.text
    r = await fa._ui("POST", f"/fixed-assets/classes/{cls['id']}/revalue/report",
                     files={"file": ("valuation.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    assert t("fixed_assets.valuer_report_attached", name="valuation.pdf", count=2) in r.text
    assert t("fixed_assets.valuer_report_basis", name="valuation.pdf") in r.text, "the shared basis names the report"
    assert t("fixed_assets.valuer_report_remove") in r.text
