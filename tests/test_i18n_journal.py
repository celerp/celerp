# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the journal component.

`ui/components/journal.py` was already fully wired for i18n before this run: every
user-facing string is resolved via `t()` inside the render/helper function, never at
module import, and its one module-level label map (`_WITHHELD_NOTES`) stores keys and
resolves them at render, per R1. No new strings needed converting and no new keys were
minted (see `keymaps/journal.json`).

These tests prove the render-time wiring by registering a sentinel language ``xx``
(via the module i18n seam) and asserting the sentinel text reaches the rendered
output while ``xx`` is the active language, covering each conversion mechanism the
file uses: a module-level label dict resolved at render, a table header, a search-bar
attribute value, an interpolated message, and a bulk-toolbar action label. They are
red against any tree that resolves these strings at import time or hardcodes English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.journal import (
    _headers,
    _item_or_note,
    journal_bulk_toolbar,
    journal_filter_bar,
    journal_filter_words,
    journal_table,
    journal_void_toast,
)

# Sentinel catalog: one unmistakable value per key the journal renders.
_XX = {
    "acct.filtered_totals": "XX_FILTERED_TOTALS",
    "label.search": "XX_SEARCH",
    "acct.journal_search": "XX_SEARCH_PLACEHOLDER",
    "th.date": "XX_DATE",
    "th.debit": "XX_DEBIT",
    "th.credit": "XX_CREDIT",
    "acct.bulk_void": "XX_BULK_VOID",
    "acct.bulk_void_result": "XX_VOID_RESULT {n}",
    "acct.items_untied": "XX_ITEMS_UNTIED",
    "acct.no_journal_entries": "XX_NO_ENTRIES",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_filter_words_translate_labels():
    out = journal_filter_words({"q": "coffee"})
    assert "XX_FILTERED_TOTALS" in out
    assert "XX_SEARCH" in out


def test_filter_bar_translates_placeholder_attribute():
    html = to_xml(journal_filter_bar("/accounting/journal", {}, {}))
    assert "XX_SEARCH_PLACEHOLDER" in html


def test_headers_translate():
    html = to_xml(_headers(items=False, has_fx=False, select=False))
    assert "XX_DATE" in html
    assert "XX_DEBIT" in html
    assert "XX_CREDIT" in html


def test_empty_state_translates():
    html = to_xml(journal_table([], items=False))
    assert "XX_NO_ENTRIES" in html


def test_bulk_toolbar_translates_action_label():
    html = to_xml(journal_bulk_toolbar({}, {}))
    assert "XX_BULK_VOID" in html


def test_void_toast_translates_interpolated_message():
    """`journal_void_toast` returns an HX-Trigger header dict, not an FT element, so
    the sentinel is asserted directly against its message rather than via `to_xml`."""
    result = {"results": [{"status": "void"}]}
    toast = journal_void_toast(result)
    assert "XX_VOID_RESULT" in toast["HX-Trigger"]


def test_item_or_note_translates_module_dict_value():
    """The `_WITHHELD_NOTES` module-level dict stores keys, resolved via `t()` in
    `_item_or_note` at render time (R1), never frozen at import."""
    note = _item_or_note({"items_status": "untied"})
    html = to_xml(note)
    assert "XX_ITEMS_UNTIED" in html
