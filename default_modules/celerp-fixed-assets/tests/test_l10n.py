# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Country rules from another module: the fixed_asset_rules slot.

The test-only module ZZ (tests/fixtures/celerp_l10n_zz) registers a depreciation
method, a timing convention, an asset regime, two pool regimes (one with deferred tax)
and a statutory form, plus a broken entry and one reusing a core key, and carries a
pack with a fixed_asset_classes table. A rule resolves for a company only while the
company uses the module that registered it.
"""
from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _run
from test_sales import _finalize, _sell

pytestmark = pytest.mark.asyncio

D = Decimal
ZZ_MODULE = "celerp_l10n_zz"
PACKAGE = Path(__file__).resolve().parent.parent / "celerp_fixed_assets"


async def _rules(fa: FA) -> dict:
    return (await fa.ok(await fa.get("/setup")))["rules"]


async def _set_zz(fa: FA, on: bool) -> None:
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    enabled = get_enabled(company.settings) | {"celerp-fixed-assets", "celerp-accounting", "celerp-docs", ZZ_MODULE}
    if not on:
        enabled.discard(ZZ_MODULE)
    company.settings = set_enabled(dict(company.settings or {}), enabled)
    await fa.session.commit()


async def _zz_class(fa: FA) -> dict:
    return await fa.ok(await fa.post("/classes", {
        "name": "ZZ boats", "method": "zz_fixed_rate", "life_months": 120, "params": {"rate": "0.12"}}))


async def _map(fa: FA, class_name: str, categories: dict) -> None:
    cls = (await fa.classes())[class_name]
    await fa.ok(await fa.patch(f"/classes/{cls['id']}", {"tax_categories": categories}))


async def _tax_book(fa: FA, regime: str, year: int) -> dict:
    return await fa.ok(await fa.get("/tax-books", regime=regime, year=year))


async def test_l10n_registers_method_and_regimes(fa, zz):
    rules = await _rules(fa)
    methods = {m["key"]: m for m in rules["methods"]}
    assert methods["zz_fixed_rate"]["module"] == ZZ_MODULE and methods["zz_fixed_rate"]["label"] == "ZZ fixed rate"
    assert methods["straight_line"]["module"] is None
    assert "zz_quarter_start" in {c["key"] for c in rules["conventions"]}
    regimes = {r["key"]: r for r in rules["tax_regimes"]}
    assert set(regimes) == {"zz_asset", "zz_pool", "zz_pool_plain"}
    assert regimes["zz_asset"]["basis"] == "asset" and regimes["zz_asset"]["country"] == "ZZ"
    assert [c["key"] for c in regimes["zz_pool"]["categories"]] == ["zz_general", "zz_cars"]
    assert regimes["zz_pool"]["deferred_tax"] is True and regimes["zz_pool_plain"]["deferred_tax"] is False
    assert [r["key"] for r in rules["statutory_reports"]] == ["zz_form_a"]
    # The registered method depreciates: 12% a year of 12,000 is 120 a month.
    await _managed_from(fa)
    await _zz_class(fa)
    boat = await _contributed(fa, "Survey boat", "ZZ boats")
    await _run(fa, "2026-02")
    charged = D((await fa.asset(boat))["accumulated_depreciation"])
    assert charged > 0 and charged % 120 == 0
    # The settings accept the module's regimes and nothing else.
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset", "zz_pool"], "tax_year_end": "12-31"}))
    s = (await fa.ok(await fa.get("/setup")))["settings"]
    assert s["tax_regimes"] == ["zz_asset", "zz_pool"] and s["tax_year_end"] == "12-31"
    bad = await fa.patch("/settings", {"tax_regimes": ["xx_unknown"]})
    assert bad.status_code == 422 and "xx_unknown" in bad.json()["detail"]


async def test_l10n_pool_regime_additions_disposals(fa, zz):
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"], "tax_year_end": "12-31"}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    await _managed_from(fa)
    car = await _contributed(fa)
    await _contributed(fa, "Second car", cost="8000", on="2026-02-01")
    await _run(fa, "2026-03")
    await _finalize(fa, await _sell(fa, car, price="10000"))
    book = await _tax_book(fa, "zz_pool", 2026)
    assert book["basis"] == "pool" and book["status"] == "open"
    (cars,) = [p for p in book["pools"] if p["key"] == "zz_cars"]
    assert (D(cars["opening"]), D(cars["additions"]), D(cars["disposals"])) == (D(0), D("20000"), D("10000"))
    assert (D(cars["allowance"]), D(cars["closing"])) == (D("2500"), D("7500"))
    nxt = await _tax_book(fa, "zz_pool", 2027)
    (cars,) = [p for p in nxt["pools"] if p["key"] == "zz_cars"]
    assert D(cars["opening"]) == D("7500") and D(cars["allowance"]) == D("1875")


async def test_l10n_class_table_preview_confirm_undo_cas(fa, zz):
    table = json.loads((zz / "pack.json").read_text())["fixed_asset_classes"]
    before = await fa.classes()
    preview = await fa.ok(await fa.post("/class-table", {"classes": table, "preview": True, "op_key": "ct"}))
    assert [c["name"] for c in preview["create"]] == ["ZZ vessels"]
    updated = {c["name"]: c["changes"] for c in preview["update"]}
    assert updated["Buildings"]["life_months"] == 600
    assert updated["Vehicles"] == {"tax_categories": {"zz_asset": "zz_plant", "zz_pool": "zz_cars"}}
    # Nothing changes until confirmed.
    assert await fa.classes() == before
    await fa.ok(await fa.post("/class-table", {"classes": table, "preview": False, "op_key": "ct",
                                               "fingerprint": preview["fingerprint"]}))
    after = await fa.classes()
    assert after["Buildings"]["life_months"] == 600 and after["ZZ vessels"]["method"] == "zz_fixed_rate"
    # The user edits Vehicles after the table: undo restores only what nobody has edited since.
    await fa.ok(await fa.patch(f"/classes/{after['Vehicles']['id']}", {"life_months": 72}))
    undo = await fa.ok(await fa.post("/class-table/ct/undo", {"preview": True, "op_key": "ct-undo"}))
    assert [c["name"] for c in undo["kept"]] == ["Vehicles"]
    await fa.ok(await fa.post("/class-table/ct/undo", {"preview": False, "op_key": "ct-undo",
                                                       "fingerprint": undo["fingerprint"]}))
    final = await fa.classes()
    assert final["Buildings"]["life_months"] == before["Buildings"]["life_months"]
    assert final["Vehicles"]["life_months"] == 72 and final["Vehicles"]["tax_categories"]["zz_pool"] == "zz_cars"
    # A class the table created is archived, never deleted.
    assert "ZZ vessels" not in final
    archived = {c["name"]: c for c in (await fa.ok(await fa.get("/classes", include_archived=True)))["items"]}
    assert archived["ZZ vessels"]["archived"] is True
    # A table entry that cannot be a class is refused by name, before anything changes.
    bad = await fa.post("/class-table", {"classes": [{"name": "Rafts", "method": "no_such_method"}],
                                         "preview": True, "op_key": "ct2"})
    assert bad.status_code == 422 and "Rafts" in bad.json()["detail"]


async def test_l10n_bad_entry_skipped_named(fa, zz):
    rules = await _rules(fa)
    problems = {(p["module"], p["key"]): p["reason"] for p in rules["problems"]}
    assert "amounts" in problems[(ZZ_MODULE, "zz_broken")]
    assert "zz_broken" not in {m["key"] for m in rules["methods"]}
    # The module's other rules still load.
    assert "zz_fixed_rate" in {m["key"] for m in rules["methods"]}
    page = await fa.page("/settings/fixed-assets")
    assert page.status_code == 200 and "zz_broken" in page.text


async def test_l10n_key_cannot_shadow_core(fa, zz):
    from celerp_fixed_assets.calc import methods, rules

    found = {(p["module"], p["key"]): p["reason"] for p in (await _rules(fa))["problems"]}
    assert "core" in found[(ZZ_MODULE, "straight_line")]
    assert isinstance(rules.method("straight_line"), methods.StraightLine)
    (straight,) = [m for m in (await _rules(fa))["methods"] if m["key"] == "straight_line"]
    assert straight["module"] is None


async def test_l10n_rule_unavailable_when_module_off(fa, zz):
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"], "tax_year_end": "12-31"}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    await _managed_from(fa)
    await _zz_class(fa)
    boat = await _contributed(fa, "Survey boat", "ZZ boats")
    await _contributed(fa)
    await _run(fa, "2026-02")
    await _set_zz(fa, False)
    rules = await _rules(fa)
    assert "zz_fixed_rate" not in {m["key"] for m in rules["methods"]}
    assert rules["tax_regimes"] == []
    # The asset still on the method is held by the run with a named reason.
    preview = await fa.ok(await fa.post("/runs", {"month": "2026-03", "preview": True, "op_key": "p"}))
    (held,) = [e for m in preview["months"] for e in m["exceptions"] if e["asset_id"] == boat]
    assert held["action"] == "method_unavailable"
    assert "ZZ fixed rate" in held["reason"] and "ZZ localization" in held["reason"]
    refused = await fa.post("/classes", {"name": "More boats", "method": "zz_fixed_rate", "life_months": 60})
    assert refused.status_code == 422
    # Its tax books can be read but not recalculated or closed.
    book = await _tax_book(fa, "zz_pool", 2026)
    assert book["read_only"] is True and "ZZ localization" in book["read_only_reason"]
    close = await fa.post("/tax-years/zz_pool/2025/close", {})
    assert close.status_code == 409 and "ZZ localization" in close.json()["detail"]
    # Turned back on, the rules resolve again.
    await _set_zz(fa, True)
    assert "zz_fixed_rate" in {m["key"] for m in (await _rules(fa))["methods"]}


async def test_l10n_deferred_tax_only_from_regime(fa, zz):
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool", "zz_pool_plain"], "tax_year_end": "12-31"}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars", "zz_pool_plain": "zz_cars"})
    await _managed_from(fa, "2025-01-01")
    await _contributed(fa, on="2025-01-15")
    await _run(fa, "2025-12")
    with_tax = await fa.ok(await fa.get("/reports/book-tax", regime="zz_pool", year=2025))
    assert with_tax["deferred_tax"] is True
    (row,) = with_tax["rows"]
    # Book: 12,000 less twelve months at 200; tax: 12,000 less a quarter.
    assert (D(row["book_carrying"]), D(row["tax_written_down_value"])) == (D("9600"), D("9000"))
    assert D(row["difference"]) == D("600") and D(row["deferred_tax"]) == D("120")
    plain = await fa.ok(await fa.get("/reports/book-tax", regime="zz_pool_plain", year=2025))
    assert plain["deferred_tax"] is False and "deferred_tax" not in plain["rows"][0]
    page = await fa.page("/fixed-assets/reports/book-tax", regime="zz_pool_plain", year=2025)
    assert page.status_code == 200 and "Deferred tax" not in page.text
    page = await fa.page("/fixed-assets/reports/book-tax", regime="zz_pool", year=2025)
    assert "Deferred tax" in page.text and "Book-tax difference" in page.text


async def test_l10n_tax_books_never_post(fa, zz):
    from test_year_end import _accept_all

    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_asset"], "tax_year_end": "12-31"}))
    await _map(fa, "Vehicles", {"zz_asset": "zz_plant"})
    await _managed_from(fa, "2025-01-01")
    car = await _contributed(fa, on="2025-01-15")
    await _run(fa, "2025-12")
    journals = {je["entity_id"] for je in await fa.entities("journal_entry")}
    await fa.ok(await fa.patch(f"/tax-books/{car}/zz_asset", {"elections": {"full_expensing": True}}))
    await _accept_all(fa, 2025)
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/reopen", {"reason": "Election withdrawn"}))
    assert {je["entity_id"] for je in await fa.entities("journal_entry")} == journals
    # The tax code cannot reach the posting code.
    for path in (PACKAGE / "calc" / "tax.py", PACKAGE / "taxbooks.py"):
        imported = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                imported |= {node.module or "", *(f"{node.module}.{a.name}" for a in node.names)}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        assert not {n for n in imported if n.endswith(("operations", "module_journals", "auto_je"))}, path


async def test_statutory_form_renders_from_regime(fa, zz):
    await fa.ok(await fa.patch("/settings", {"tax_regimes": ["zz_pool"], "tax_year_end": "12-31"}))
    await _map(fa, "Vehicles", {"zz_pool": "zz_cars"})
    await _managed_from(fa)
    await _contributed(fa)
    form = await fa.ok(await fa.get("/reports/statutory/zz_form_a", year=2026))
    assert form["columns"] == ["Pool", "Allowance", "Written-down value"]
    assert form["rows"] == [["zz_cars", "3000.00", "9000.00"]]
    csv = await fa.page("/fixed-assets/reports/statutory/zz_form_a.csv", year=2026)
    assert csv.status_code == 200 and "zz_cars,3000.00,9000.00" in csv.text
    printed = await fa.page("/fixed-assets/reports/statutory/zz_form_a/print", year=2026)
    assert printed.status_code == 200 and "3,000.00" in printed.text and "9,000.00" in printed.text
    missing = await fa.get("/reports/statutory/zz_unknown", year=2026)
    assert missing.status_code == 404


def test_no_country_rules_in_core():
    """Tax regimes, statutory forms and country class tables come only from a country module:
    neither core nor any module Celerp ships carries one."""
    from celerp.modules import loader
    from celerp_fixed_assets.calc import rules

    assert rules.CORE["tax_regime"] == () and rules.CORE["statutory_report"] == ()
    repo = PACKAGE.parents[2]
    for module in sorted(p for p in (repo / "default_modules").iterdir() if (p / "__init__.py").exists()):
        assert "fixed_asset_rules" not in (loader.read_manifest(module).get("slots") or {}), module.name
    shipped = [p for root in (repo / "celerp", repo / "default_modules") for p in root.rglob("*.json")
               if "tests" not in p.relative_to(repo).parts]
    assert shipped
    carrying = [str(p.relative_to(repo)) for p in shipped if "fixed_asset_classes" in p.read_text()]
    assert carrying == []
