# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tax books: each asset's tax figures per regime and year, kept beside the books and
never posted. Uses the ZZ test module's asset regime (zz_plant at 20%, a full
expensing election) on tax years ending 30 June."""
from __future__ import annotations

from decimal import Decimal

import pytest

from fa_fixtures import FA
from test_l10n import _map, _tax_book
from test_runs import _contributed, _managed_from, _run
from test_year_end import _accept_all, _mark, _year_end

pytestmark = pytest.mark.asyncio

D = Decimal


async def _setup(fa: FA, *, mapped: bool = True) -> str:
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    if mapped:
        await _map(fa, "Vehicles", {"zz_asset": "zz_plant"})
    await _managed_from(fa, "2024-01-01")
    car = await _contributed(fa, on="2025-01-15")
    await _run(fa, "2026-06")
    return car


def _row(book: dict, asset_id: str) -> dict:
    (row,) = [r for r in book["rows"] if r["asset_id"] == asset_id]
    return row


def _figures(row: dict) -> tuple:
    return tuple(D(row[k]) for k in ("opening", "additions", "disposals", "allowance", "closing"))


async def test_tax_book_asset_regime_years(fa, zz):
    car = await _setup(fa)
    book = await _tax_book(fa, "zz_asset", 2025)
    assert book["basis"] == "asset" and (book["start"], book["end"]) == ("2024-07-01", "2025-06-30")
    row = _row(book, car)
    assert row["status"] == "ok" and row["category"] == "zz_plant"
    assert _figures(row) == (D(0), D("12000"), D(0), D("2400"), D("9600"))
    assert _figures(_row(await _tax_book(fa, "zz_asset", 2026), car)) == (D("9600"), D(0), D(0), D("1920"), D("7680"))
    # Before the asset was available for use it has no tax figures.
    assert (await _tax_book(fa, "zz_asset", 2024))["rows"] == []
    # A regime the company has not chosen, or one no module provides, is refused.
    assert (await fa.get("/tax-books", regime="zz_pool", year=2025)).status_code == 422
    assert (await fa.get("/tax-books", regime="xx_unknown", year=2025)).status_code == 404


async def test_tax_book_elections(fa, zz):
    car = await _setup(fa)
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"full_expensing": True}}))
    assert _figures(_row(await _tax_book(fa, "zz_asset", 2025), car)) == (D(0), D("12000"), D(0), D("12000"), D(0))
    # Each election shows the name its module gives it, on the API and on the page.
    assert (await _tax_book(fa, "zz_asset", 2025))["election_labels"] == {"full_expensing": "Full expensing"}
    page = await fa.page("/fixed-assets/tax-books", regime="zz_asset", year=2025)
    assert "Full expensing" in page.text and ">full_expensing<" not in page.text
    bad = await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"bonus": True}})
    assert bad.status_code == 422 and "bonus" in bad.json()["detail"]
    wrong_type = await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"full_expensing": "yes"}})
    assert wrong_type.status_code == 422
    operator = await fa.role_headers("operator")
    assert (await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {}}, headers=operator)).status_code == 403


async def test_tax_book_no_category_then_mapped(fa, zz):
    car = await _setup(fa, mapped=False)
    row = _row(await _tax_book(fa, "zz_asset", 2025), car)
    assert row["status"] == "no_category" and row["allowance"] is None
    refused = await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_cars"})
    assert refused.status_code == 422 and "zz_cars" in refused.json()["detail"]
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_buildings"}))
    row = _row(await _tax_book(fa, "zz_asset", 2025), car)
    assert row["status"] == "ok" and row["category"] == "zz_buildings" and D(row["allowance"]) == D("600")


async def test_tax_year_close_freezes_figures(fa, zz):
    car = await _setup(fa)
    await _accept_all(fa, 2025)
    closed = await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    assert closed["status"] == "closed"
    # An election made afterwards changes the open years only.
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"full_expensing": True}}))
    book = await _tax_book(fa, "zz_asset", 2025)
    assert book["status"] == "closed" and D(_row(book, car)["allowance"]) == D("2400")
    later = _row(await _tax_book(fa, "zz_asset", 2026), car)
    assert (D(later["opening"]), D(later["allowance"]), D(later["closing"])) == (D("9600"), D("9600"), D(0))
    again = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert again.status_code == 409


async def test_tax_year_reopen_with_history(fa, zz):
    car = await _setup(fa)
    await _accept_all(fa, 2025)
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"full_expensing": True}}))
    no_reason = await fa.post("/tax-years/zz_asset/2025/reopen", {})
    assert no_reason.status_code == 422
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/reopen", {"reason": "Election made late"}))
    book = await _tax_book(fa, "zz_asset", 2025)
    assert book["status"] == "open" and D(_row(book, car)["allowance"]) == D("12000")
    assert [h["action"] for h in book["history"]] == ["reopened", "closed"]
    assert book["history"][0]["reason"] == "Election made late"


async def test_tax_year_close_order_and_timing(fa, zz):
    await _setup(fa)
    # 2026 cannot close before 2025, which has figures.
    await _accept_all(fa, 2026)
    first = await fa.post("/tax-years/zz_asset/2026/close", {})
    assert first.status_code == 409 and "2025" in first.json()["detail"]
    await _accept_all(fa, 2025)
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    await fa.ok(await fa.post("/tax-years/zz_asset/2026/close", {}))
    # 2025 cannot reopen while 2026 stands closed.
    refused = await fa.post("/tax-years/zz_asset/2025/reopen", {"reason": "Late election"})
    assert refused.status_code == 409 and "2026" in refused.json()["detail"]
    # A year that has not ended cannot close.
    early = await fa.post("/tax-years/zz_asset/2027/close", {})
    assert early.status_code == 409 and "2027-06-30" in early.json()["detail"]
    operator = await fa.role_headers("operator")
    assert (await fa.post("/tax-years/zz_asset/2026/reopen", {"reason": "x"}, headers=operator)).status_code == 403


# ── A tax year closes only on figures the regime worked out ──────────────────

_REMEDY_CATEGORY = "Set its tax category"


async def _accept_books(fa: FA, year: int) -> None:
    """Accept every year-end item that is not a tax figure, as an accountant would."""
    for item in (await _year_end(fa, year))["items"]:
        if item["kind"] != "tax" and item["state"] != "accepted":
            await fa.ok(await _mark(fa, year, item["key"], "accepted", reason="Reviewed at year end",
                                    effect="None on the figures"))


def _fail(*_args, **_kwargs):
    raise ArithmeticError("no rate")


async def test_tax_figure_missing_cannot_be_accepted_or_closed(fa, zz):
    car = await _setup(fa, mapped=False)
    number = (await fa.asset(car))["number"]
    key = f"tax:zz_asset:{car}"
    refused = await _mark(fa, 2025, key, "accepted", reason="Reviewed", effect="None")
    assert refused.status_code == 409 and _REMEDY_CATEGORY in refused.json()["detail"]
    # Noting it, with the reason it waits, is still allowed.
    await fa.ok(await _mark(fa, 2025, key, "noted", reason="Category to be agreed"))
    await _accept_books(fa, 2025)
    close = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert close.status_code == 409, close.text
    assert number in close.json()["detail"] and _REMEDY_CATEGORY in close.json()["detail"]
    assert (await _tax_book(fa, "zz_asset", 2025))["status"] == "open"
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_plant"}))
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))


async def test_tax_acceptance_saved_before_the_rule_does_not_close(fa, zz):
    from celerp_fixed_assets import yearend
    from celerp_fixed_assets.store import _emit

    car = await _setup(fa, mapped=False)
    number = (await fa.asset(car))["number"]
    key = f"tax:zz_asset:{car}"
    await _accept_books(fa, 2025)
    saved = (await fa.state(yearend.yearend_id(2025))) or {}
    marks = {**(saved.get("marks") or {}), key: {"state": "accepted", "reason": "Reviewed", "effect": "None",
                                                 "amount": None, "at": "2025-07-01", "user": "x"}}
    await _emit(fa.session, fa.auth["company_id"], None, yearend.yearend_id(2025), "fa_yearend",
                "fa.yearend.recorded", {"year": 2025, "marks": marks})
    await fa.session.commit()
    items = {i["key"]: i for i in (await _year_end(fa, 2025))["items"]}
    assert items[key]["state"] == "open" and items[key]["accepted_amount"] is None
    close = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert close.status_code == 409 and number in close.json()["detail"], close.text
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_plant"}))
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    again = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert again.status_code == 409 and "already closed" in again.json()["detail"]
    book = await _tax_book(fa, "zz_asset", 2025)
    assert book["status"] == "closed" and [h["action"] for h in book.get("history") or []] in ([], ["closed"])


async def test_tax_close_refusal_in_thai_names_the_asset(fa, zz):
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "06-30"}))
    await _managed_from(fa, "2024-01-01")
    car = await _contributed(fa, name="รถยนต์ของเจ้าของ", on="2025-01-15")
    await _run(fa, "2026-06")
    number = (await fa.asset(car))["number"]
    th = {**fa.h, "Accept-Language": "th"}
    refused = await _mark(fa, 2025, f"tax:zz_asset:{car}", "accepted", headers=th, reason="ตรวจแล้ว", effect="ไม่มี")
    assert refused.status_code == 409 and f"{number} ไม่มีหมวดภาษี" in refused.json()["detail"], refused.text
    await _accept_books(fa, 2025)
    close = await fa.post("/tax-years/zz_asset/2025/close", {}, headers=th)
    assert close.status_code == 409 and number in close.json()["detail"], close.text
    page = await fa.page("/fixed-assets/tax-books", regime="zz_asset", year=2025)
    assert page.status_code == 200 and "รถยนต์ของเจ้าของ" in page.text


async def test_tax_calculation_failure_blocks_close(fa, zz, monkeypatch):
    from celerp_l10n_zz.rules import AssetRegime

    car = await _setup(fa)
    number = (await fa.asset(car))["number"]
    monkeypatch.setattr(AssetRegime, "asset_year", _fail)
    assert _row(await _tax_book(fa, "zz_asset", 2025), car)["status"] == "error"
    accepted = await _mark(fa, 2025, f"tax:zz_asset:{car}", "accepted", reason="Reviewed", effect="None")
    assert accepted.status_code == 409
    await _accept_books(fa, 2025)
    close = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert close.status_code == 409 and number in close.json()["detail"], close.text
    assert (await _tax_book(fa, "zz_asset", 2025))["status"] == "open"


async def test_tax_pool_failure_blocks_close(fa, zz, monkeypatch):
    from celerp_l10n_zz.rules import PoolRegime

    await _setup(fa)
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"]}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    monkeypatch.setattr(PoolRegime, "pool_year", _fail)
    (pool,) = [p for p in (await _tax_book(fa, "zz_pool", 2025))["pools"] if p["key"] == "zz_cars"]
    assert pool["status"] == "error"
    assert "tax:zz_pool:pool:zz_cars" in {i["key"] for i in (await _year_end(fa, 2025))["items"]}
    await _accept_books(fa, 2025)
    close = await fa.post("/tax-years/zz_pool/2025/close", {})
    assert close.status_code == 409 and "zz_cars" in close.json()["detail"], close.text


def _empty(*_args, **_kwargs):
    return None


def _no_closing(_regime, inputs):
    from celerp_fixed_assets.calc.tax import TaxYearResult

    return TaxYearResult(opening=inputs.opening, additions=inputs.additions, disposals=inputs.disposals,
                         allowance=Decimal(0), closing=None)


@pytest.mark.parametrize("result", [_empty, _no_closing])
async def test_tax_result_without_figures_blocks_close(fa, zz, monkeypatch, result):
    from celerp_l10n_zz.rules import AssetRegime, PoolRegime

    car = await _setup(fa)
    number = (await fa.asset(car))["number"]
    monkeypatch.setattr(AssetRegime, "asset_year", result)
    assert _row(await _tax_book(fa, "zz_asset", 2025), car)["status"] == "error"
    await _accept_books(fa, 2025)
    close = await fa.post("/tax-years/zz_asset/2025/close", {})
    assert close.status_code == 409 and number in close.json()["detail"], close.text
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"]}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    monkeypatch.setattr(PoolRegime, "pool_year", result)
    (pool,) = [p for p in (await _tax_book(fa, "zz_pool", 2025))["pools"] if p["key"] == "zz_cars"]
    assert pool["status"] == "error"
    close = await fa.post("/tax-years/zz_pool/2025/close", {})
    assert close.status_code == 409 and "zz_cars" in close.json()["detail"], close.text


async def test_tax_year_closed_without_a_figure_is_not_carried(fa, zz):
    from celerp_fixed_assets import taxbooks
    from celerp_fixed_assets.store import _emit

    car = await _setup(fa, mapped=False)
    number = (await fa.asset(car))["number"]
    # Tax year 2025 closed, as it could before, with no figure for the car.
    open_2025 = await _tax_book(fa, "zz_asset", 2025)
    assert _row(open_2025, car)["status"] == "no_category"
    await _emit(fa.session, fa.auth["company_id"], None, taxbooks.taxyear_id("zz_asset", 2025), "fa_taxyear",
                "fa.taxyear.recorded", {"regime": "zz_asset", "year": 2025, "status": "closed",
                                        "start": open_2025["start"], "end": open_2025["end"],
                                        "rows": open_2025["rows"], "pools": [], "history": []})
    await fa.session.commit()
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"category": "zz_plant"}))
    later = _row(await _tax_book(fa, "zz_asset", 2026), car)
    assert later["status"] == "no_basis" and later["opening"] is None and later["closing"] is None
    assert "2025" in later["reason"]
    items = {i["key"]: i for i in (await _year_end(fa, 2026))["items"]}
    assert number in items[f"tax:zz_asset:{car}"]["label"]
    await _accept_books(fa, 2026)
    close = await fa.post("/tax-years/zz_asset/2026/close", {})
    assert close.status_code == 409 and number in close.json()["detail"] and "2025" in close.json()["detail"]
    # Reopened, 2025 works the car out and 2026 starts from it.
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/reopen", {"reason": "Category set"}))
    assert _figures(_row(await _tax_book(fa, "zz_asset", 2026), car)) == (
        D("9600"), D(0), D(0), D("1920"), D("7680"))


async def test_tax_pool_additions_counted_once_across_a_closed_year(fa, zz):
    await _setup(fa)
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"]}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    (before,) = [p for p in (await _tax_book(fa, "zz_pool", 2026))["pools"] if p["key"] == "zz_cars"]
    await _accept_books(fa, 2025)
    await fa.ok(await fa.post("/tax-years/zz_pool/2025/close", {}))
    (after,) = [p for p in (await _tax_book(fa, "zz_pool", 2026))["pools"] if p["key"] == "zz_cars"]
    assert (after["opening"], after["additions"], after["closing"]) == (
        before["opening"], before["additions"], before["closing"])
