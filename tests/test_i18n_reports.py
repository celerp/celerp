# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the reports routes.

Every user-facing label on the AR/AP aging, sales/purchases, and expiring
reports is built by calling ``t()`` at render time, so a request in a
non-English language gets translated output. These tests register a sentinel
language ``xx`` and assert its unmistakable values reach the rendered output
while ``xx`` is active. They are red against a tree that hardcodes the English
strings (the sentinel never appears when the source emits a literal).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import page_title
from ui.routes.reports import (
    _aging_view,
    _summary_bar,
    _sales_view_columns,
    _expiring_view,
    _group_by_filter,
    _date_presets,
    _date_filter_bar,
)

# Sentinel catalog: one unmistakable value per key the reports surfaces render.
_XX = {
    "page.ar_aging": "XX_AR_AGING",
    "th.contact": "XX_CONTACT",
    "th.total": "XX_TOTAL",
    "reports.aging_current": "XX_CURRENT",
    "reports.no_data_period": "XX_NO_DATA",
    "reports.total_revenue": "XX_TOTAL_REV",
    "reports.margin_pct": "XX_MARGIN",
    "reports.supplier": "XX_SUPPLIER",
    "reports.num_orders": "XX_NUM_ORDERS",
    "reports.spend": "XX_SPEND",
    "reports.n_days": "XX_NDAYS {n}",
    "reports.expiring_summary": "XX_EXPSUM {count}/{days}",
    "reports.no_expiring": "XX_NO_EXPIRING",
    "reports.related_settings": "XX_RELATED",
    "filter.custom": "XX_CUSTOM",
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


def test_page_title_translates():
    # Mechanism: browser <title> via the shared page_title helper (R5).
    assert page_title("page.ar_aging") == "XX_AR_AGING - Celerp"


def test_aging_headers_translate():
    # Mechanism: table headers built at render time.
    data = {"lines": [{"customer_name": "Acme", "outstanding": 100, "current": 100}],
            "buckets": {}, "as_of": "2026-08-23"}
    html = to_xml(_aging_view(data, "AR"))
    assert "XX_CONTACT" in html
    assert "XX_CURRENT" in html
    assert "XX_TOTAL" in html


def test_aging_empty_state_translates():
    # Mechanism: empty-state CTA copy.
    html = to_xml(_aging_view({"lines": []}, "AR"))
    assert "XX_NO_DATA" in html


def test_summary_bar_kpis_translate():
    # Mechanism: KPI summary labels.
    data = {"total_revenue": 1000, "total_cost": 400, "gross_profit": 600}
    html = to_xml(_summary_bar(data, "USD", "customer", show_margin=True))
    assert "XX_TOTAL_REV" in html
    assert "XX_MARGIN" in html


def test_sales_columns_translate():
    # Mechanism: dynamically assembled report column headers.
    headers = [h for h, _, _ in _sales_view_columns("supplier", is_purchases=True)]
    assert "XX_SUPPLIER" in headers
    assert "XX_NUM_ORDERS" in headers
    assert "XX_SPEND" in headers


def test_expiring_view_interpolated_strings_translate():
    # Mechanism: count-bearing interpolated strings ({n}, {count}/{days}).
    data = {"count": 5, "days_threshold": 30,
            "lines": [{"sku": "S1", "name": "N1", "status": "available",
                       "days_remaining": 3, "expires_at": "2026-09-01", "item_id": "i1"}]}
    html = to_xml(_expiring_view(data, days=30))
    assert "XX_NDAYS 7" in html
    assert "XX_EXPSUM 5/30" in html


def test_expiring_empty_state_translates():
    html = to_xml(_expiring_view({"count": 0, "days_threshold": 30, "lines": []}, days=30))
    assert "XX_NO_EXPIRING" in html


def test_group_by_filter_options_translate():
    # Mechanism: dropdown option labels (raw value stays canonical in value=).
    html = to_xml(_group_by_filter("customer", "/reports/purchases", first_option="supplier"))
    assert "XX_SUPPLIER" in html
    assert 'value="supplier"' in html  # raw value preserved (R3)


def test_date_presets_custom_translates():
    labels = [label for _, label in _date_presets("xx")]
    assert "XX_CUSTOM" in labels


def test_date_filter_bar_settings_gear_translates():
    # Mechanism: title attribute on the related-settings gear.
    html = to_xml(_date_filter_bar("/reports/ar-aging", "", "", "this_fy",
                                   settings_link="/settings/sales?tab=terms", lang="xx"))
    assert "XX_RELATED" in html
