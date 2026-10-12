# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the documents route.

The documents page builds its user-facing labels, table headers, item-status
badges, and the strings it hands to client-side JavaScript by calling ``t()``
at render time, so a request in a non-English language gets translated output.
These tests register a sentinel language ``xx`` and assert the sentinel text
reaches the rendered output while ``xx`` is active. They are red against a tree
that hardcodes English or resolves any of these strings at import time.

Each conversion mechanism is covered at least once:
  - a module-level label dict resolved at render (``_STATUS_BADGE``),
  - a dynamic enum display label,
  - a table header,
  - a value handed to inline JavaScript via a ``json.dumps`` config object.
"""

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.documents import (
    _item_status_badge_cell,
    _doc_history_section,
    _payment_section,
)
import pytest

# Sentinel catalog: one unmistakable value per key exercised below.
_XX = {
    "documents.status_reserved": "XX_RESERVED",
    "enum.item_status.sold": "XX_SOLD",
    "documents.history": "XX_HISTORY",
    "documents.no_activity_recorded": "XX_NOACT",
    "documents.applied_to": "XX_APPLIEDTO",
    "documents.select_invoice_option": "XX_SELECTINV",
}


# A credit note with an outstanding balance and one applied payment, so
# _payment_section renders both its history table header and the invoice-picker
# script.
_CREDIT_NOTE = {
    "entity_id": "doc:1",
    "id": "doc:1",
    "doc_type": "credit_note",
    "status": "final",
    "currency": "USD",
    "amount_outstanding": 10,
    "total": 10,
    "payments": [
        {"amount": 5, "payment_date": "2026-01-01", "method": "cash",
         "status": "posted", "target_doc_id": "doc:9"},
    ],
}
_BANKS = [{"chart_account_code": "1000", "name": "Cash"}]


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the
    registry and the context language afterwards so nothing leaks between
    tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_status_badge_module_dict_label_translates():
    # Module-level _STATUS_BADGE stores i18n KEYS; the label resolves at render.
    html = to_xml(_item_status_badge_cell("reserved", "inv:1"))
    assert "XX_RESERVED" in html


def test_status_badge_enum_display_label_translates():
    # A status whose display label lives under enum.item_status.* (R3).
    html = to_xml(_item_status_badge_cell("sold", "inv:1"))
    assert "XX_SOLD" in html


def test_history_section_chrome_translates():
    html = to_xml(_doc_history_section([]))
    assert "XX_HISTORY" in html
    assert "XX_NOACT" in html


def test_payment_history_table_header_translates():
    # A table header built with t() at render (the credit-note history column).
    html = to_xml(_payment_section(_CREDIT_NOTE, bank_accounts=_BANKS))
    assert "XX_APPLIEDTO" in html


def test_payment_js_config_value_translates():
    # R2: strings baked into inline JS are handed over as a json.dumps config
    # object built with t() at render, never spliced as English source.
    html = to_xml(_payment_section(_CREDIT_NOTE, bank_accounts=_BANKS))
    assert "XX_SELECTINV" in html


def _es():
    i18n.set_lang("es")


def test_fulfill_confirm_has_opening_question_mark_in_es():
    """Red statement: the confirm was built as the button label plus "?", so Spanish read
    "Marcar como enviado?" with no opening mark."""
    import re
    from ui.routes.documents import _li_bulk_toolbar
    _es()
    html = to_xml(_li_bulk_toolbar("doc:1", False, show_fulfill=True))
    confirm = re.search(r'id="li-bulk-fulfill-btn"[^>]*hx-confirm="([^"]*)"', html) \
        or re.search(r'hx-confirm="([^"]*)"[^>]*id="li-bulk-fulfill-btn"', html)
    assert confirm, html
    assert confirm.group(1).startswith("¿") and confirm.group(1).endswith("?")


def test_file_delete_confirm_is_a_catalog_sentence():
    """Red statement: the confirm was "Eliminar archivo: name?", a label glued to a name."""
    from ui.components.files import files_section
    _es()
    html = to_xml(files_section("doc", "doc:1", [{"id": "f1", "filename": "plano.pdf",
                                                  "size": 10, "uploaded_at": "2026-01-01"}]))
    assert "¿Eliminar el archivo plano.pdf?" in html


def test_document_line_unit_displays_translated_and_saves_stored_value():
    """Red statement: the unit cell showed the stored name ("piece"), and the save script
    read the cell's text, so translating the text would have saved the translation."""
    from ui.routes.documents import _doc_detail
    _es()
    doc = {"id": "doc:u1", "entity_id": "doc:u1", "doc_type": "invoice", "status": "draft",
           "line_items": [{"sku": "U-1", "description": "Ring", "quantity": 3, "unit": "piece",
                           "unit_price": 10}]}
    html = to_xml(_doc_detail(doc))
    assert 'data-unit="piece"' in html
    assert '>unidad</span>' in html
    assert "unitEl.dataset.unit" in html
    assert "unitEl.textContent || ''" not in html


def test_picker_payload_carries_unit_label():
    """Red statement: the picker sent only the stored sell_by, so a picked line showed it raw."""
    from ui.routes.documents import _picker_item
    _es()
    out = _picker_item({"sku": "C-1", "name": "Stone", "sell_by": "carat", "weight": 2,
                        "weight_unit": "carat"}, 5, {})
    assert out["sell_by"] == "carat" and out["sell_by_label"] == "quilate"
    assert out["weight_unit_label"] == "quilate"


def test_line_action_rows_show_the_unit_in_the_users_language():
    """Red statement: Receive and Return rows printed the stored unit name next to the
    quantity field."""
    from ui.routes.documents import _receive_rows
    _es()
    html = to_xml(_receive_rows([{"sku": "R-1", "quantity": 2, "unit": "piece", "line_id": "l1"}], {}))
    assert ">unidad</span>" in html and ">piece</span>" not in html
