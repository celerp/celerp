# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the dashboard route.

The dashboard builds every KPI-card label, quick-link label/description, and
KPI sub-label by calling ``t()`` at render time, so a request in a non-English
language gets translated output. These tests prove that by registering a
sentinel language ``xx`` (via the module i18n seam) and asserting the sentinel
text reaches the rendered output while ``xx`` is the active language. They are
red against a tree that resolves any of these strings at import time or
hardcodes English in the module-level vertical configs.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.dashboard import _dl, _kpi_card, _kpi_values, _quick_links

# Sentinel catalog: one unmistakable value per key exercised below. Covers each
# conversion mechanism used in the file - a module-dict config label, an
# interpolated sub-label, and a quick-link description.
_XX = {
    "dashboard.stock_value": "XX_STOCK_VALUE",
    "dashboard.all_inventory": "XX_ALL_INV",
    "dashboard.view_and_manage_stock": "XX_VIEW_MANAGE",
    "dashboard.sub.active_items": "XX_ITEMS {n}",
    "dashboard.sub.past_due": "XX_PAST_DUE",
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


def test_dl_resolves_config_label_at_render():
    # The value->key map resolves through t() at call time, not import time.
    assert _dl("All inventory") == "XX_ALL_INV"


def test_kpi_card_label_translates():
    spec = {"key": "sv", "label": "Stock Value", "value_fn": "retail_total", "sub_fn": None}
    html = to_xml(_kpi_card(spec, {"retail_total": "$1"}))
    assert "XX_STOCK_VALUE" in html


def test_kpi_sub_label_interpolates_and_translates():
    values = _kpi_values({}, {"active_item_count": 5}, {}, {}, {}, {}, "USD")
    assert values["active_items_sub"] == "XX_ITEMS 5"
    assert values["past_due_sub"] == "XX_PAST_DUE"


def test_quick_link_description_translates():
    cfg = {"quick_links": [("/inventory", "Inventory", "View and manage stock")]}
    html = to_xml(_quick_links(cfg))
    assert "XX_VIEW_MANAGE" in html
