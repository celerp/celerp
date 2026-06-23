# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Render tests for the dashboard chart empty-states and the activity 'See All' link.

The dashboard is the first page a customer lands on, so every chart must read as
complete even with no data yet (empty-state copy) instead of a blank/missing chart.
Pure render — no server.
"""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.components.activity import activity_table
from ui.routes.dashboard import _charts_section

_ALL_CHARTS = {"charts": ["ar_aging", "inventory_cat"]}


def test_charts_show_empty_states_when_no_data():
    xml = to_xml(_charts_section(
        _ALL_CHARTS, {"category_counts": {}}, {"buckets": {}},
        revenue_trend=[], currency="USD",
    ))
    # Each chart shows its empty-state copy instead of a blank canvas.
    assert "No revenue in the last 6 months" in xml
    assert "No outstanding receivables" in xml
    assert "No inventory yet" in xml
    # The canvas elements themselves are NOT rendered when empty (no blank charts).
    assert 'id="chart-revenue-trend"' not in xml
    assert 'id="chart-ar-aging"' not in xml
    assert 'id="chart-inventory-cat"' not in xml


def test_charts_render_real_charts_when_data_present():
    xml = to_xml(_charts_section(
        {"charts": ["ar_aging", "inventory_cat"]},
        {"category_counts": {"Rings": 4}},
        {"buckets": {"Current": 500.0, "31-60": 100.0}},
        revenue_trend=[{"month": "2026-01", "total": 1000.0}], currency="USD",
    ))
    assert 'id="chart-revenue-trend"' in xml
    assert 'id="chart-ar-aging"' in xml
    assert 'id="chart-inventory-cat"' in xml
    assert "No revenue" not in xml and "No outstanding receivables" not in xml


def test_ar_all_zero_buckets_is_treated_as_empty():
    # Buckets present but all zero (no real receivables) -> empty state, not a chart.
    xml = to_xml(_charts_section(
        {"charts": ["ar_aging"]}, {}, {"buckets": {"Current": 0, "31-60": 0}},
        revenue_trend=[], currency="USD",
    ))
    assert "No outstanding receivables" in xml
    assert 'id="chart-ar-aging"' not in xml


def test_activity_footer_has_see_all_link():
    rows = [
        {"event_type": "item.created", "entity_type": "item", "entity_id": str(i),
         "created_at": "2026-06-01T00:00:00Z", "data": {}}
        for i in range(20)
    ]
    xml = to_xml(activity_table(rows, max_display=15, history_url="/history"))
    assert "Showing last 15 events - " in xml
    assert ">See All<" in xml
    assert 'href="/history"' in xml


def test_activity_footer_no_see_all_without_history_url():
    rows = [
        {"event_type": "item.created", "entity_type": "item", "entity_id": str(i),
         "created_at": "2026-06-01T00:00:00Z", "data": {}}
        for i in range(20)
    ]
    xml = to_xml(activity_table(rows, max_display=15))
    assert "Showing last 15 events" in xml
    assert "See All" not in xml
