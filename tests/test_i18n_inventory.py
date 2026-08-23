# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the inventory routes.

The inventory page builds its user-facing labels, table headers, JS
configuration values, and dynamic enum labels by calling ``t()`` (and
``display_enum()``) at render time, so a request in a non-English language gets
translated output. These tests register a sentinel language ``xx`` and assert
the sentinel text reaches the rendered output while ``xx`` is active. They are
red against a tree that resolves any of these strings at import time or
hardcodes English.

One test per conversion MECHANISM used in ui/routes/inventory.py:
  - module-dict / tuple label     (_inventory_type_tabs, _labor_kind_labels)
  - table header                  (_workflow_section head row)
  - JS data-attr value            (_workflow_section data-total-label)
  - enum display label            (display_enum / _inventory_type_tabs tabs)
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.table import display_enum
from ui.routes.inventory import (
    _inventory_type_tabs,
    _labor_kind_labels,
    _workflow_section,
    _fmt_minutes,
)

# Sentinel catalog: one unmistakable value per key each mechanism renders.
_XX = {
    # enum display labels (display_enum -> enum.<domain>.<raw>)
    "enum.inventory_type.stocked": "XX_TYPE_STOCKED",
    "enum.labor_kind.hourly": "XX_KIND_HOURLY",
    # module-dict / tuple label
    "inventory.all_types": "XX_ALL_TYPES",
    # table header
    "inventory.th_station": "XX_TH_STATION",
    # JS data-attr value
    "inventory.total_time_label": "XX_TOTAL_TIME",
    # module string (duration helper)
    "inventory.dur_h_min": "XX {h}H {m}M",
}


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


def test_enum_display_label_translates():
    # enum display label mechanism: raw value stays canonical, only the label is
    # translated through enum.<domain>.<raw>.
    assert display_enum("stocked", domain="inventory_type") == "XX_TYPE_STOCKED"


def test_module_tuple_label_and_enum_reach_type_tabs():
    # module tuple label ("All Types") and the per-type enum labels both render
    # through t()/display_enum at render time.
    html = to_xml(_inventory_type_tabs({}))
    assert "XX_ALL_TYPES" in html
    assert "XX_TYPE_STOCKED" in html


def test_module_dict_label_translates_labor_kinds():
    # module-dict label mechanism: {raw: display_enum(raw, domain="labor_kind")}.
    labels = _labor_kind_labels()
    assert labels["hourly"] == "XX_KIND_HOURLY"


def test_workflow_table_header_translates():
    # table-header mechanism: headers rendered through filter_th(t(...)).
    item = {"workflow": {"steps": [{"id": "s1", "time_minutes": 90}]}, "files": []}
    html = to_xml(_workflow_section("e1", item))
    assert "XX_TH_STATION" in html


def test_workflow_js_data_attr_translates():
    # JS data-attr mechanism: the total-time label is handed to the client via a
    # data-* attribute, never spliced into JS source.
    item = {"workflow": {"steps": [{"id": "s1", "time_minutes": 90}]}, "files": []}
    html = to_xml(_workflow_section("e1", item))
    assert 'data-total-label="XX_TOTAL_TIME"' in html


def test_duration_helper_translates():
    # module string with interpolation reaches output at render time.
    assert _fmt_minutes(90) == "XX 1H 30M"
