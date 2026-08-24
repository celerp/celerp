# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared data-table component.

table.py builds cell labels, column headers, the Excel-style filter popups
(client-side JS reading data-i18n-* attributes), enum/status display labels,
and pagination chrome by calling ``t()`` / ``display_enum()`` at render time,
so a request in a non-English language gets translated output. These tests
register a sentinel language ``xx`` (via the module i18n seam) and assert the
sentinel text reaches the rendered output while ``xx`` is the active
language. They are red against a tree that resolves any of these strings at
import time or hardcodes English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.table import (
    _display_val,
    display_enum,
    filter_th,
    pagination,
    add_new_option,
)

# Sentinel catalog: one unmistakable value per key/mechanism this file renders.
_XX = {
    "settings.yes": "XX_YES",
    "label.filter_by": "XX_FILTER_BY {label}",
    "table.select_all_option": "XX_SELECT_ALL",
    "table.search_ellipsis": "XX_SEARCH_DOTS",
    "enum.item_status.archived": "XX_ARCHIVED",
    "table.records_count": "XX_RECORDS {n}",
    "label._add_new": "XX_ADD_NEW",
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


def test_bool_cell_module_dict_label_translates():
    # R1: _display_val's bool branch resolves settings.yes/settings.no via t()
    # at render time, never a frozen constant.
    out = to_xml(_display_val(True, "bool"))
    assert "XX_YES" in out


def test_filter_th_header_title_translates():
    # Table header: filter_th's funnel button title/aria-label resolve
    # label.filter_by at render time.
    out = to_xml(filter_th("Name", 0))
    assert "XX_FILTER_BY" in out


def test_filter_th_js_data_attr_translates():
    # R2: filter_th hands the Excel-style filter popup's strings to
    # COLUMN_FILTER_JS via data-i18n-* attributes rather than splicing
    # translated text into JS source.
    out = to_xml(filter_th("Name", 0))
    assert "XX_SELECT_ALL" in out
    assert "XX_SEARCH_DOTS" in out


def test_display_enum_translates_status_label():
    # R3: raw enum value stays canonical; only the display label translates.
    assert display_enum("archived", "item_status") == "XX_ARCHIVED"
    # The raw value itself is untouched (never translated for comparisons).
    assert display_enum("archived", "item_status") != "archived"


def test_pagination_records_count_translates():
    out = to_xml(pagination(1, 5, 25, "/items"))
    assert "XX_RECORDS" in out


def test_add_new_option_label_translates():
    option, _js = add_new_option()
    out = to_xml(option)
    assert "XX_ADD_NEW" in out
