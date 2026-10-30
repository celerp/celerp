# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A line saved with only a tax rate (tax_rate, maybe tax_code, no taxes list) charges that rate,
so a new line counts it when it takes the taxes the other lines all carry.

An older version, and a CSV import with tax columns, store a line's tax as tax_rate. The document's
money charges it, so the new-line decision (default_line_taxes) reads it the same way, as the
editor shows it (legacy_line_taxes): the company tax it names, or with no name the one at its rate,
else its own code and rate. A rate that can't be read leaves the new line's tax to the user.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from celerp.services.money import to_decimal
from celerp.tax_regimes import TAX_REGIMES, default_line_taxes, line_tax
from test_new_line_tax_ui import _post, _taxes
from test_ui import _authed, _mock_get_company, _mock_get_modules, ui_client  # noqa: F401  (fixtures)

_TH = TAX_REGIMES["TH"]["taxes"]
_VAT = line_tax(next(tax for tax in _TH if tax["name"] == "VAT 7%"))
_LEVY = {"code": "Levy 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
_COMPOUND = {"name": "Levy 10%", "rate": 10.0, "tax_type": "both", "is_default": False, "is_compound": True,
             "default_order": 1}


def _legacy(rate, code=None) -> dict:
    line = {"description": "Fitting", "quantity": 1, "unit_price": 100.0, "tax_rate": rate}
    if code is not None:
        line["tax_code"] = code
    return line


def _modern(taxes) -> dict:
    return {"description": "Fitting", "quantity": 1, "unit_price": 100.0, "taxes": taxes}


def _doc(*lines, doc_taxes=(_LEVY,)) -> dict:
    return {"doc_type": "invoice", "status": "draft", "doc_taxes": list(doc_taxes), "line_items": list(lines)}


@pytest.mark.parametrize(("lines", "taxes", "expected"), [
    ([_legacy(7, "VAT 7%")], _TH, [_VAT]),
    ([_legacy(7)], _TH, [_VAT]),
    ([_legacy(7, "VAT 7%"), _modern([_VAT])], _TH, [_VAT]),
    ([{**_legacy(7, "VAT 7%"), "taxes": []}], _TH, [_VAT]),
    ([_legacy(7, "Old VAT")], _TH,
     [{"code": "Old VAT", "rate": 7.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]),
    ([_legacy(6)], _TH, [{"code": "", "rate": 6.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]),
    ([_legacy(10, "Levy 10%")], _TH + [_COMPOUND], [line_tax(_COMPOUND)]),
    ([_legacy(0, "VAT 7%"), _modern([])], _TH, []),
    ([_legacy(7, "VAT 7%"), _modern([])], _TH, None),
    ([_legacy(7, "VAT 7%"), _legacy(10, "Levy 10%")], _TH + [_COMPOUND], None),
    ([_legacy("seven", "VAT 7%")], _TH, None),
    ([_legacy(7, "VAT 7%"), {**_legacy(7, "VAT 7%"), "taxes": None}], _TH, None),
], ids=["named", "rate-only", "next-to-same-tax", "empty-list", "unknown-name-kept", "custom-rate", "compound",
        "zero-next-to-none", "rate-next-to-none", "conflicting-rates", "unreadable", "next-to-unset"])
def test_a_new_line_counts_a_line_saved_with_only_a_rate(lines, taxes, expected):
    assert default_line_taxes(taxes, "invoice", doc=_doc(*lines)) == expected


@pytest.mark.parametrize(("currency", "price"), [("USD", 100.0), ("JPY", 1999.0), ("BHD", 10.555)])
def test_the_new_line_charges_what_the_line_saved_with_a_rate_charges(currency, price):
    """A $100 line saved with tax_rate 7 and a document-wide 5% levy: a $100 line added next to it
    is taxed VAT 7% too, so VAT is $14, the levy $10 and the total $224 (not $217). In every
    currency the two lines charge the same as two lines saved with the rate."""
    from celerp_docs.doc_money import document_money

    old = {**_legacy(7, "VAT 7%"), "unit_price": price}
    added = {**_modern(default_line_taxes(_TH, "invoice", doc=_doc(old))), "unit_price": price}
    money = document_money({"doc_taxes": [_LEVY]}, [old, added], currency, keep_unrated_tax=True)
    uniform = document_money({"doc_taxes": [_LEVY]}, [old, dict(old)], currency, keep_unrated_tax=True)
    assert (money["tax"], money["total"]) == (uniform["tax"], uniform["total"])
    if currency == "USD":
        assert (money["tax"], money["total"]) == (24.0, 224.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "body"), [
    ("/docs/from-items/add", b"selected=item%3Ap1&target_id=doc%3AINV-1"),
    ("/api/items/send-to", b"selected=item%3Ap1&send_to_doc_type=invoice&send_to_target=doc%3AINV-1"),
])
async def test_items_added_to_a_draft_with_a_rate_line_take_its_tax(ui_client, url, body):
    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    doc = {**_doc(_legacy(7, "VAT 7%")), "entity_id": "doc:INV-1", "currency": "THB", "version": 3}
    r = await _post(ui_client, url, body, taxes=_taxes(), doc=doc, patch_doc=patch_doc)
    assert r.status_code == 204, r.text
    assert patch_doc.call_args[0][2]["line_items"][-1]["taxes"] == [_VAT]


@pytest.mark.asyncio
async def test_a_csv_row_without_tax_columns_takes_the_rate_lines_tax(ui_client):
    from test_new_line_tax_ui import _import_csv

    patch_doc = AsyncMock(return_value={"event_id": "e1"})
    doc = {**_doc(_legacy(7, "VAT 7%")), "entity_id": "doc:INV-1", "currency": "THB", "version": 3}
    r = await _import_csv(ui_client, "sku,quantity\nJWL-002,1\n", doc=doc, patch_doc=patch_doc)
    assert r.status_code == 200, r.text
    assert patch_doc.call_args[0][2]["line_items"][-1]["taxes"] == [_VAT]


@pytest.mark.asyncio
async def test_the_editor_shows_a_line_saved_with_a_rate_as_the_new_line_reads_it(ui_client):
    """The editor's tax choice for each such line is what legacy_line_taxes reads, so the page's
    own new-line rule (_celerpApplyDefaultTaxes) compares the same taxes as the server."""
    from bs4 import BeautifulSoup

    from celerp.tax_regimes import legacy_line_taxes

    lines = [_legacy(7, "VAT 7%"), _legacy(7, "Old VAT"), _legacy(6)]
    doc = {**_doc(*lines), "entity_id": "doc:INV-1", "id": "doc:INV-1", "ref_id": "INV-1", "currency": "USD",
           "version": 3, "subtotal": 300.0, "tax": 0.0, "total": 300.0}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
            patch("ui.api_client.get_document_taxes", new=_taxes()):
        page = await ui_client.get("/docs/doc:INV-1")
    assert page.status_code == 200, page.text[-500:]
    rows = BeautifulSoup(page.text, "html.parser").select('table.doc-lines tbody tr [data-name="tax_select"]')
    assert len(rows) == len(lines)
    shown = []
    for sel in rows:
        chosen = sel.select_one("option[selected]")
        held = sel.parent.select_one('[data-name="taxes_json"]')
        if held is not None and chosen["value"] == "|stored":
            shown.append(json.loads(held["value"]))
        elif chosen.get("data-tax"):
            shown.append([json.loads(chosen["data-tax"])])
        else:
            assert chosen["value"] == "|custom"
            rate = float(sel.parent.select_one('[data-name="tax_rate_custom"]')["value"])
            shown.append([{"code": "", "rate": rate, "amount": 0, "order": 0, "is_compound": False, "label": ""}])
    assert shown == [legacy_line_taxes(line, _TH, "invoice") for line in lines]


@pytest.mark.parametrize("line", [_legacy(7, "VAT 7%"), _modern([_VAT])], ids=["rate-only", "taxes-list"])
def test_every_reader_of_a_lines_tax_reads_a_line_saved_with_only_a_rate(line):
    """Class guard: each reader of what a line charges outside the money itself (the activity
    feed, a document becoming a quotation list) reads a rate-only line as the same line holding
    the tax as a list, as document_money charges it."""
    from celerp_docs.doc_money import document_money, line_tax_rate, list_totals
    from celerp_docs.received import _lines

    assert line_tax_rate({**line, "line_total": 100.0}) == 7
    received = _lines({"line_items": [{**line, "line_total": 100.0}]}, "list")[0]
    assert document_money({}, [received], "USD", keep_unrated_tax=True)["tax"] == 7


@pytest.mark.parametrize("currency, tax", [("USD", 15.5), ("JPY", 16), ("KWD", 15.5)])
def test_every_reader_of_a_lines_tax_reads_its_taxes_list_before_its_rate(currency, tax):
    """Class guard: a line holding a taxes list beside a ``tax_rate`` (the list editor saves the
    summed rate, an older version may hold a stale one) is read by its taxes everywhere: what a
    list and a document charge it, the activity feed, a document becoming a quotation list and
    the editor's rate. 10% then 5% compound on 100."""
    from celerp_docs.doc_money import document_money, line_tax_rate, list_totals
    from celerp_docs.received import _lines
    from ui.components.activity import _fields_changed_summary

    compound = [{"code": "T1", "rate": 10.0, "amount": 0, "order": 0, "is_compound": False, "label": ""},
                {"code": "T2", "rate": 5.0, "amount": 0, "order": 1, "is_compound": True, "label": ""}]
    line = {**_modern(compound), "tax_rate": 7, "line_total": 100.0}
    listed = list_totals({"currency": currency, "line_items": [dict(line)]})
    assert listed["tax_amount"] == pytest.approx(tax)
    assert document_money({}, [dict(line)], currency, keep_unrated_tax=True)["tax"] == pytest.approx(tax)
    received = list_totals({"currency": currency, "line_items": _lines({"line_items": [line]}, "list")})
    assert received["tax_amount"] == pytest.approx(tax)
    assert line_tax_rate(line) == 15
    shown = _fields_changed_summary({"line_items": {"old": [{**line, "taxes": []}], "new": [line]}}, currency, docs_on=True)
    assert "Tax 7% → 15%" in shown
