# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the settings routes.

The settings module builds every user-facing label, table header, enum display
value, and click-to-edit chrome by calling ``t()`` at render time, so a request
in a non-English language gets translated output. These tests prove that by
registering a sentinel language ``xx`` (via the module i18n seam) and asserting
the sentinel text reaches the rendered output while ``xx`` is the active
language. They are red against a tree that resolves any of these strings at
import time or hardcodes English.

Each conversion mechanism used in the file is covered at least once:
  - R1 module-level label dict resolved at render (fiscal month, doc type)
  - R3 enum display-label layer (location type)
  - a table header built from new keys (item-schema category header)
  - plain labels plus the em-dash-fixed warning copy (factory-reset card)
  - a translated value carried in an HTML attribute (click-to-edit title)
  - interpolated copy (per-page preference)
"""

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings import (
    _company_display_cell,
    _location_display_cell,
    _tc_display_cell,
    _schema_tab,
    _factory_reset_card,
    _preference_display_cell,
)

# Sentinel catalog: one unmistakable value per settings key exercised below.
_XX = {
    "settings.fiscal_month_mar": "XX_FISCAL_MAR",
    "settings.loc_type_warehouse": "XX_WAREHOUSE",
    "settings.doc_type_invoice": "XX_INVOICE",
    "settings.th_order": "XX_ORDER",
    "settings.reset_all_data": "XX_RESET_ALL",
    "settings.skip_continue": "XX_SKIP_CONTINUE",
    "settings.factory_reset_warning": "XX_FACTORY_WARNING",
    "settings.click_to_edit": "XX_CLICK_EDIT",
    "settings.per_page": "XX_PERPAGE {n}",
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


def test_module_dict_fiscal_month_translates():
    # R1: value -> key dict resolved at render; also the click-to-edit title attr.
    html = to_xml(_company_display_cell("fiscal_year_start", "03-01"))
    assert "XX_FISCAL_MAR" in html
    assert "XX_CLICK_EDIT" in html


def test_enum_display_location_type_translates():
    # R3: raw enum value "warehouse" mapped to a display label only.
    html = to_xml(_location_display_cell("loc-1", "type", "warehouse"))
    assert "XX_WAREHOUSE" in html


def test_module_dict_doc_type_translates():
    # R1: T&C doc-type labels resolved from the value->key dict at render.
    html = to_xml(_tc_display_cell(0, "doc_types", {"doc_types": ["invoice"]}))
    assert "XX_INVOICE" in html


def test_schema_table_header_translates():
    # Table header built from new keys (category item-schema header).
    html = to_xml(_schema_tab([], {"Widgets": []}, "Widgets"))
    assert "XX_ORDER" in html


def test_factory_reset_card_labels_translate_and_no_em_dash():
    # Plain labels plus the em-dash-fixed warning copy.
    html = to_xml(_factory_reset_card())
    assert "XX_RESET_ALL" in html
    assert "XX_SKIP_CONTINUE" in html
    assert "XX_FACTORY_WARNING" in html
    # No em dash (U+2014) survives in the user-facing render.
    assert "\u2014" not in html


def test_preference_per_page_interpolates():
    # Interpolated copy resolved at render.
    html = to_xml(_preference_display_cell("default_per_page", "50", "xx"))
    assert "XX_PERPAGE 50" in html
