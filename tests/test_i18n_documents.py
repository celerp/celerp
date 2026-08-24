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
