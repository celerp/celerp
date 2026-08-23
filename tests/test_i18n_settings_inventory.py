# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for Settings -> Inventory.

The inventory-settings route builds its UI chrome, headings, hints, table
headers, enum display labels (unit type, stock-cutting method, vertical tag
group), and click-to-edit confirms by calling ``t()`` at render time, so a
request in a non-English language gets translated output. These tests prove
that by registering a sentinel language ``xx`` (via the module i18n seam) and
asserting the sentinel text reaches the rendered output while ``xx`` is the
active language. They are red against a tree that hardcodes the English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_inventory import (
    _unit_type_select,
    _units_tab,
    _reorder_tab,
    _categories_tab,
)

# Sentinel catalog: one unmistakable value per conversion MECHANISM used in the
# route - an enum display label, a render-time table header, a module-level
# plain string, and an interpolated click-to-edit confirm.
_XX = {
    # enum display labels (R3): unit type, inventory method, vertical tag group
    "enum.unit_type.weight": "XX_UNIT_WEIGHT",
    "enum.inventory_method.fifo": "XX_METHOD_FIFO",
    "enum.vertical_tag.electronics": "XX_VERT_ELECTRONICS",
    # table header resolved at render time
    "th.type": "XX_TH_TYPE",
    # module-level plain strings
    "settings_inventory.unit_name_hint": "XX_UNIT_HINT",
    "settings_inventory.stock_cutting_method": "XX_STOCK_CUTTING",
    # interpolated confirm text on an hx_confirm attribute
    "settings_inventory.delete_unit_confirm": "XX_DEL_UNIT {name}",
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


def test_unit_type_select_enum_label_translates():
    """Enum display label mechanism: the unit_type dropdown resolves its option
    text through t("enum.unit_type.<raw>") while the raw value stays canonical."""
    html = to_xml(_unit_type_select("value", selected="weight"))
    assert "XX_UNIT_WEIGHT" in html
    # Raw value stays canonical (never translated).
    assert 'value="weight"' in html


def test_units_tab_header_hint_and_confirm_translate():
    """Table header, module-level hint, and an interpolated hx_confirm all render
    in the request language."""
    html = to_xml(_units_tab([
        {"name": "kg", "label": "Kg", "decimals": 2, "unit_type": "weight"},
    ]))
    assert "XX_TH_TYPE" in html      # Th(t("th.type"))
    assert "XX_UNIT_HINT" in html    # settings_inventory.unit_name_hint
    assert "XX_DEL_UNIT" in html     # delete_unit_confirm, interpolated with the unit name


def test_reorder_tab_method_label_and_heading_translate():
    """Inventory-method enum label and a section heading render in the request
    language."""
    html = to_xml(_reorder_tab({}))
    assert "XX_METHOD_FIFO" in html
    assert "XX_STOCK_CUTTING" in html


def test_categories_tab_vertical_tag_group_label_translates():
    """Module-dict label mechanism (R1): the vertical-tag group heading resolves
    its key through _tag_label at render time."""
    html = to_xml(_categories_tab(
        {}, {},
        [{"name": "laptop", "display_name": "Laptop", "vertical_tags": ["electronics"]}],
        [], "", {},
    ))
    assert "XX_VERT_ELECTRONICS" in html
