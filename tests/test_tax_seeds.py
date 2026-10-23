# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The tax codes a new company starts with match the law in force, each country has one
clear default, a new line's tax is built one way everywhere, and an existing company still
on a code that has been replaced is found so it can be told."""
from __future__ import annotations

import copy
import inspect

import pytest

from celerp import tax_regimes
from celerp.tax_regimes import TAX_REGIMES, default_line_taxes


def _rows(country: str) -> dict[str, dict]:
    return {t["name"]: t for t in TAX_REGIMES[country]["taxes"]}


def test_every_regime_has_exactly_one_default():
    for country, regime in TAX_REGIMES.items():
        defaults = [t["name"] for t in regime["taxes"] if t.get("is_default")]
        assert len(defaults) == 1, (country, defaults)


def test_india_default_is_igst():
    taxes = TAX_REGIMES["IN"]["taxes"]
    assert [t["code"] for t in default_line_taxes(taxes, "invoice", doc=None)] == ["IGST 18%"]
    assert [t["code"] for t in default_line_taxes(taxes, "bill", doc=None)] == ["IGST 18%"]
    rows = _rows("IN")
    assert not rows["CGST 9%"]["is_default"] and not rows["SGST 9%"]["is_default"]
    assert "CGST 9%" in rows["IGST 18%"]["description"] and "SGST 9%" in rows["IGST 18%"]["description"]


def test_ethiopia_seeds_current_law():
    rows = _rows("ET")
    assert not [n for n in rows if n.startswith("TOT")]
    assert "WHT 2%" not in rows
    wht = rows["WHT 3%"]
    assert wht["rate"] == -3.0 and wht["tax_type"] == "purchase" and not wht["is_default"]
    assert "20,000" in wht["description"] and "10,000" in wht["description"] and "30%" in wht["description"]


def test_quebec_qst_is_not_compound_and_charges_on_the_price():
    from celerp_docs.taxes import TaxApplication, compute_tax_amounts

    rows = _rows("CA")
    qst = rows["QST 9.975%"]
    assert qst["is_compound"] is False
    applied = [TaxApplication(code=r["name"], rate=r["rate"], order=r["default_order"], is_compound=r["is_compound"])
               for r in (rows["GST 5%"], qst)]
    amounts = compute_tax_amounts(applied, 1000.0, "CAD")
    assert 1000.0 + sum(a.amount for a in amounts) == pytest.approx(1149.75)


def test_malaysia_seeds_codes_in_force():
    rows = _rows("MY")
    assert "SST 6%" not in rows
    assert [t["code"] for t in default_line_taxes(TAX_REGIMES["MY"]["taxes"], "invoice", doc=None)] == [
        "Sales Tax 10%"]
    assert rows["Sales Tax 10%"]["rate"] == 10.0 and rows["Sales Tax 10%"]["tax_type"] == "sales"
    assert rows["Sales Tax 5%"]["rate"] == 5.0 and not rows["Sales Tax 5%"]["is_default"]
    assert rows["Service Tax 8%"]["rate"] == 8.0
    assert rows["Service Tax 6%"]["rate"] == 6.0 and not rows["Service Tax 6%"]["is_default"]
    assert "Exempt" in rows


def test_vietnam_reduced_rate_is_not_default():
    rows = _rows("VN")
    assert rows["VAT 8%"]["rate"] == 8.0 and rows["VAT 8%"]["tax_type"] == "both"
    assert not rows["VAT 8%"]["is_default"] and rows["VAT 10%"]["is_default"]
    assert "2026" in rows["VAT 8%"]["description"]


def test_indonesia_ppn_rows():
    rows = _rows("ID")
    assert "11/12" in rows["PPN 11%"]["description"] and rows["PPN 11%"]["is_default"]
    assert rows["PPN 12%"]["rate"] == 12.0 and not rows["PPN 12%"]["is_default"]
    assert rows["PPN 12%"]["tax_type"] == "both"


_EM_DASH = "\u2014"


def test_no_seed_text_has_an_em_dash():
    for country, regime in TAX_REGIMES.items():
        for tax in regime["taxes"]:
            assert _EM_DASH not in tax["name"] + tax.get("description", ""), (country, tax["name"])
    assert _EM_DASH not in (tax_regimes.__doc__ or "")


def test_default_line_taxes_matches_the_scan_line():
    """Every path that builds a new line (scan, from items, append, reorder, the editor) takes its
    tax from one helper, so a line looks the same whichever way it was added."""
    from celerp_docs import routes

    th = TAX_REGIMES["TH"]["taxes"]
    assert default_line_taxes(th, "invoice", doc=None) == [
        {"code": "VAT 7%", "rate": 7.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]
    assert default_line_taxes(TAX_REGIMES["IN"]["taxes"], "bill", doc=None)[0]["code"] == "IGST 18%"
    assert default_line_taxes(TAX_REGIMES["US"]["taxes"], "invoice", doc=None) == []
    assert default_line_taxes(TAX_REGIMES["MY"]["taxes"], "bill", doc=None) == []
    assert default_line_taxes(TAX_REGIMES["TH"]["taxes"], None, doc={"doc_taxes": []})[0]["code"] == "VAT 7%"
    assert "line_taxes" in inspect.signature(routes._scan_doc_line).parameters
    assert '"is_compound": bool(' not in inspect.getsource(routes._scan_doc_line)


def test_a_new_line_takes_every_default_tax():
    """A company set up before IGST became the India default holds CGST 9% and SGST 9% both as
    defaults: a new line carries both (18%), never only the first. A zero-rate default adds
    nothing."""
    before = [
        {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True},
        {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True},
        {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": True},
        {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": False},
    ]
    assert [(t["code"], t["rate"]) for t in default_line_taxes(before, "invoice", doc=None)] == [
        ("CGST 9%", 9.0), ("SGST 9%", 9.0)]
    assert [t["code"] for t in default_line_taxes(before, "bill", doc=None)] == ["CGST 9%", "SGST 9%"]


_VAT = {"code": "VAT 7%", "rate": 7.0, "amount": 7.0, "order": 0, "is_compound": False, "label": ""}
_LEVY = {"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}


def _draft(doc_taxes, *line_taxes) -> dict:
    return {"doc_taxes": doc_taxes, "line_items": [{"quantity": 1, "unit_price": 100.0, "taxes": taxes}
                                                   for taxes in line_taxes]}


def _money(doc_taxes: list[dict], lines: list[dict]) -> tuple[float, float]:
    from celerp_docs.doc_money import document_money

    money = document_money({"doc_taxes": doc_taxes}, lines, "USD", keep_unrated_tax=True)
    return money["tax"], money["total"]


def test_a_document_taxed_as_a_whole_keeps_its_taxation_on_a_new_line():
    """While a draft charges a tax as a whole, a new line takes the taxes its lines all carry,
    copied whole with no amount, never a company default; None (the user sets it) when its lines
    differ or it has none. A stored value of another shape is no document tax."""
    th = TAX_REGIMES["TH"]["taxes"]
    compound = {**_LEVY, "order": 1, "is_compound": True, "amount": 5.25}
    assert default_line_taxes(th, "invoice", doc=_draft([_VAT], [], [])) == []
    assert default_line_taxes(th, "invoice", doc=_draft([_VAT], [compound], [compound])) == [{**compound, "amount": 0}]
    assert default_line_taxes(th, "invoice", doc=_draft([_VAT])) is None
    assert default_line_taxes(th, "invoice", doc=_draft([_LEVY], [_VAT], [])) is None
    assert default_line_taxes(th, "invoice", doc=_draft([_LEVY], [_VAT], [{**_VAT, "is_compound": True}])) is None
    assert default_line_taxes(th, "invoice", doc=_draft("7%", []))[0]["code"] == "VAT 7%"
    with pytest.raises(TypeError):
        default_line_taxes(th, "invoice")


def test_a_document_wide_levy_leaves_vat_on_a_line_added_next_to_it():
    """A draft with a $100 line taxed VAT 7% and a document-wide 5% levy: a $100 line added to it
    still takes VAT 7%, so VAT is $14, the levy $10 and the total $224 (not $217)."""
    th = TAX_REGIMES["TH"]["taxes"]
    added = default_line_taxes(th, "invoice", doc=_draft([_LEVY], [_VAT]))
    assert [t["code"] for t in added] == ["VAT 7%"]
    lines = [{"quantity": 1, "unit_price": 100.0, "taxes": [_VAT]}, {"quantity": 1, "unit_price": 100.0, "taxes": added}]
    assert _money([_LEVY], lines) == (24.0, 224.0)


@pytest.mark.parametrize("change", [{"rate": 8.0}, {"is_compound": True}, {"rate": "8"}, {"name": "VAT 8%", "rate": 8.0}],
                         ids=["rate", "compound", "text", "renamed"])
def test_a_tax_changed_in_settings_is_never_added_to_a_draft_taxed_as_a_whole(change):
    """A draft with a $100 line and VAT 7% charged as a whole; the company then changes VAT 7% to
    8% (or to compound, or replaces it with VAT 8%). A $100 line added to the draft takes no VAT of
    its own: the document's VAT 7% covers it, $14, total $214 (not $222)."""
    th = [{**tax, **change} if tax["name"] == "VAT 7%" else tax for tax in TAX_REGIMES["TH"]["taxes"]]
    whole = [{**_VAT, "amount": 0}]
    added = default_line_taxes(th, "invoice", doc=_draft(whole, []))
    lines = [{"quantity": 1, "unit_price": 100.0, "taxes": []}, {"quantity": 1, "unit_price": 100.0, "taxes": added}]
    assert added == []
    assert _money(whole, lines) == (14.0, 214.0)


def test_a_new_line_takes_the_companys_calculation_order():
    """VAT 5% (order 0) and a compound 10% levy (order 1) listed levy first: a $100 line is taxed
    VAT $5 then levy $10.50, total $115.50 (not $115), whatever order the list is in."""
    taxes = [
        {"name": "Levy 10%", "rate": 10.0, "tax_type": "both", "is_default": True, "is_compound": True,
         "default_order": 1},
        {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": True, "is_compound": False,
         "default_order": 0},
    ]
    line_taxes = default_line_taxes(taxes, "invoice", doc=None)
    assert [(t["code"], t["order"]) for t in line_taxes] == [("Levy 10%", 1), ("VAT 5%", 0)]
    assert _money([], [{"quantity": 1, "unit_price": 100.0, "taxes": line_taxes}]) == (15.5, 115.5)


def _settings(sales: list[dict] | None = None, purchasing: list[dict] | None = None) -> dict:
    settings: dict = {}
    if sales is not None:
        settings["taxes"] = copy.deepcopy(sales)
    if purchasing is not None:
        settings["purchasing_taxes"] = copy.deepcopy(purchasing)
    return settings


_OLD_ET = [
    {"name": "VAT 15%", "rate": 15.0, "tax_type": "both", "is_default": True, "is_compound": False},
    {"name": "TOT 2%", "rate": 2.0, "tax_type": "sales", "is_default": False, "is_compound": False},
    {"name": "TOT 10%", "rate": 10.0, "tax_type": "sales", "is_default": False, "is_compound": False},
    {"name": "WHT 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False, "is_compound": False},
]
_OLD_IN = [
    {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True, "is_compound": False},
    {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": True, "is_compound": False},
    {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": False, "is_compound": False},
]


def test_retired_seed_rows_matches_old_seeds():
    from celerp.tax_regimes import retired_seed_rows

    found = {m["id"]: m["lists"] for m in retired_seed_rows(_settings(_OLD_ET))}
    assert set(found) == {"et-wht-2025", "et-tot-2025"}
    assert found["et-tot-2025"] == {"sales"}
    assert {m["id"] for m in retired_seed_rows(_settings(_OLD_IN))} == {"in-default-2025"}
    # Names match whatever their case and surrounding spaces.
    renamed = [{**t, "name": f"  {t['name'].lower()} "} for t in _OLD_IN]
    assert {m["id"] for m in retired_seed_rows(_settings(renamed))} == {"in-default-2025"}
    # A row only in the saved purchasing list is found there, once.
    purchasing_only = retired_seed_rows(_settings([_OLD_ET[0]], [_OLD_ET[3]]))
    assert [(m["id"], m["lists"]) for m in purchasing_only] == [("et-wht-2025", {"purchase"})]
    # The same old row in both saved lists is one entry naming both lists.
    both = retired_seed_rows(_settings([_OLD_ET[3]], [_OLD_ET[3]]))
    assert [(m["id"], m["lists"]) for m in both] == [("et-wht-2025", {"sales", "purchase"})]
    # Without a saved purchasing list the derived copy of the sales list is not read twice.
    assert [m["lists"] for m in retired_seed_rows(_settings([_OLD_ET[3]]))] == [{"sales"}]
    # Old Quebec compound QST and Malaysia SST 6%.
    qst = {"name": "QST 9.975%", "rate": 9.975, "tax_type": "both", "is_default": False, "is_compound": True}
    sst = {"name": "SST 6%", "rate": 6.0, "tax_type": "sales", "is_default": True, "is_compound": False}
    assert {m["id"] for m in retired_seed_rows(_settings([qst, sst]))} == {"ca-qst-compound", "my-sst-6"}
    # A fixed row no longer matches: one default left, QST not compound.
    fixed = [{**_OLD_IN[0], "is_default": False}, _OLD_IN[1], _OLD_IN[2]]
    assert retired_seed_rows(_settings(fixed)) == []
    assert retired_seed_rows(_settings([{**qst, "is_compound": False}])) == []


@pytest.mark.parametrize("bad", [{}, {"taxes": None}, {"taxes": "VAT"}, {"taxes": [{"rate": 2}, "x", None]},
                                 {"taxes": [], "purchasing_taxes": {"name": "WHT 2%"}}])
def test_retired_seed_rows_reads_bad_shapes_as_empty(bad):
    from celerp.tax_regimes import retired_seed_rows

    assert retired_seed_rows(bad) == []


def test_retired_seed_rows_finds_no_match_on_any_fresh_seed():
    from celerp.tax_regimes import retired_seed_rows

    for country, regime in TAX_REGIMES.items():
        taxes = regime["taxes"]
        assert retired_seed_rows(_settings(taxes, taxes)) == [], country
