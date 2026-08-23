# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Sales Documents settings routes.

Every user-facing string in ``ui/routes/settings_sales.py`` (doc-type labels,
tab labels, the numbering hint, click-to-edit chrome, and the line-item
identifier option labels) is built by calling ``t()`` at render time, so a
request in a non-English language gets translated output. These tests prove
that by registering a sentinel language ``xx`` and asserting the sentinel text
reaches the rendered output while ``xx`` is active. They are red against a tree
that hardcodes the English literals.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_sales import (
    _doc_type_label,
    _sales_tabs,
    _numbering_tab,
    _line_items_tab,
)

# Sentinel catalog: one unmistakable value per key these routes render. Covers a
# module-dict label (doc-type), a tab label, the numbering hint, the
# click-to-edit chrome, and enum-style display labels for the identifier options.
_XX = {
    "settings.doc_type_invoice": "XX_INVOICE",
    "settings_sales.doc_type_quotation": "XX_QUOTATION",
    "settings_sales.tab_numbering": "XX_NUMBERING",
    "settings_sales.numbering_hint": "XX_NUMHINT {PREFIX}",
    "settings.click_to_edit": "XX_CLICKEDIT",
    "field.sku": "XX_SKU",
    "settings_sales.line_ident_barcode_sku": "XX_BCSKU",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the
    registry and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_doc_type_label_module_dict_translates():
    # Module-level raw->key map resolved at render time (R1/R3).
    assert _doc_type_label("invoice") == "XX_INVOICE"
    assert _doc_type_label("quotation") == "XX_QUOTATION"


def test_doc_type_label_unknown_falls_back_to_titlecase():
    # An unmapped raw value degrades to a title-cased form, never a crash.
    assert _doc_type_label("some_new_type") == "Some New Type"


def test_sales_tabs_translate():
    html = to_xml(_sales_tabs("numbering", lang="xx"))
    assert "XX_NUMBERING" in html


def test_numbering_tab_translates_label_hint_and_chrome():
    seq = {"doc_type": "invoice", "prefix": "INV", "pattern": "{PREFIX}",
           "next": 1, "preview": "INV-0001"}
    html = to_xml(_numbering_tab([seq]))
    assert "XX_INVOICE" in html      # module-dict doc-type label
    assert "XX_NUMHINT" in html      # hint text (literal {PREFIX} braces preserved)
    assert "XX_CLICKEDIT" in html    # click-to-edit chrome (title attribute)


def test_line_items_options_translate():
    html = to_xml(_line_items_tab({"line_item_identifier": "sku"}, lang="xx"))
    assert "XX_SKU" in html          # reused field.sku display label
    assert "XX_BCSKU" in html        # minted barcode+sku display label
