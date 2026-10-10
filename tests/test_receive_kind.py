# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A purchase line's kind (stock, expense or asset) is decided once and read the same way everywhere.

The stored kind wins. A line saved without one keeps the reading it always had: a line naming
an item or SKU is stock, anything else is an expense. A parcel the receipt created does not turn
an expense line into stock, the catalog item changing type later does not rewrite it, and the
Type column shows the stored kind in the user's language.
"""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from celerp.services import auto_je
from ui import i18n


@pytest.fixture
def lang():
    yield i18n.set_lang
    i18n.set_lang("en")


def _bill(status: str, lines: list[dict]) -> dict:
    return {
        "entity_id": "doc:bill-kind-1", "doc_type": "bill", "status": status, "ref_id": "BILL-K-1",
        "currency": "USD", "subtotal": 100, "tax": 0, "total": 100, "line_items": lines,
    }


def _line(**kw) -> dict:
    return {"name": "Line", "quantity": 1, "unit_price": 10, "line_total": 10, "tax_rate": 0, **kw}


def _markup(html: str) -> str:
    return re.sub(r"<script.*?</script>", "", html, flags=re.S)


def _type_cells(html: str) -> list[str]:
    body = _markup(html)
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r'<td[^>]*class="col-type"[^>]*>(.*?)</td>', body, re.S)]


def _selected_kinds(html: str) -> list[str]:
    """The kind selected in each saved row's Type select (the new-row template carries no
    data-kind-set)."""
    selects = re.findall(r'<select[^>]*data-name="receive_as"[^>]*data-kind-set="1"[^>]*>(.*?)</select>',
                         _markup(html), re.S)
    found = [re.search(r'<option value="(\w+)" selected', s) for s in selects]
    return [m.group(1) if m else None for m in found]


def test_receive_kind_stable():
    from celerp.services.units import line_receive_kind

    # Stored kind wins, case and spacing ignored.
    assert line_receive_kind({"receive_as": " Asset ", "sku": "A"}) == "asset"
    assert line_receive_kind({"receive_as": "expense", "item_id": "item:1"}) == "expense"
    # Legacy lines: an item or SKU means stock, anything else an expense.
    assert line_receive_kind({"sku": "A"}) == "stock"
    assert line_receive_kind({"item_id": "item:1"}) == "stock"
    assert line_receive_kind({"name": "Freight"}) == "expense"
    # A parcel the receipt created (entity_id) does not make a legacy expense line stock.
    assert line_receive_kind({"name": "Freight", "entity_id": "item:received"}) == "expense"
    # An unknown stored value is not trusted as a kind.
    assert line_receive_kind({"receive_as": "bogus", "name": "Freight"}) == "expense"
    # The bill journal reads the same kind.
    for li in ({"name": "Freight", "entity_id": "item:r"}, {"sku": "A"}, {"receive_as": "asset"}):
        assert auto_je.bill_line_kind(li) == line_receive_kind(li)


def test_legacy_expense_line_with_receipt_parcel_reads_expense_on_the_bill():
    doc = _bill("received", [_line(name="Freight", entity_id="item:received-1")])
    from ui.routes.documents import _doc_detail
    html = to_xml(_doc_detail(doc, item_status_map={"item:received-1": "available"}))
    assert _type_cells(html) == ["Expense"]
    # An expense line has no stock status to show.
    assert "badge--available" not in _markup(html)


def test_asset_kind_round_trip():
    """An asset line renders with Asset selected, so autosave sends asset back unchanged."""
    from ui.routes.documents import _doc_detail
    doc = _bill("draft", [_line(sku="A-1", receive_as="asset"), _line(sku="S-1", receive_as="stock"),
                          _line(name="Fee", receive_as="expense"), _line(name="Legacy fee")])
    html = to_xml(_doc_detail(doc, item_categories={}))
    assert _selected_kinds(html) == ["asset", "stock", "expense", "expense"]


def test_catalog_type_change_keeps_receive_kind():
    """A stored row's kind is marked as set, and picking an item only fills the kind of a row
    that has none yet, so a catalog type change never rewrites an existing line's kind."""
    from ui.routes.documents import _doc_detail
    doc = _bill("draft", [_line(sku="S-1", item_id="item:s1", receive_as="stock")])
    html = to_xml(_doc_detail(doc, item_categories={}))
    selects = re.findall(r'<select[^>]*data-name="receive_as"[^>]*>', _markup(html))
    assert any('data-kind-set="1"' in sel for sel in selects)
    assert "receiveAsEl.dataset.kindSet" in html


@pytest.mark.parametrize("code,expected", [
    ("en", ["Stock", "Expense", "Asset"]),
    ("es", ["Existencias", "Gasto", "Activo"]),
    ("de", ["Lagerbestand", "Aufwand", "Anlagegut"]),
])
def test_type_cells_translated(lang, code, expected):
    from ui.routes.documents import _doc_detail
    lang(code)
    doc = _bill("awaiting_payment", [_line(sku="S-1", receive_as="stock"), _line(name="Fee", receive_as="expense"),
                                     _line(sku="A-1", receive_as="asset")])
    assert _type_cells(to_xml(_doc_detail(doc, item_status_map={}))) == expected


def test_doc_asset_in_every_locale():
    for code in i18n.available_langs():
        assert i18n.t("doc.asset", code) != "doc.asset", code
