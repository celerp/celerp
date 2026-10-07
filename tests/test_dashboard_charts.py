# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Render tests for the dashboard chart empty-states and the activity 'See All' link.

The dashboard is the first page a customer lands on, so every chart must read as
complete even with no data yet (empty-state copy) instead of a blank/missing chart.
Pure render — no server.
"""
from __future__ import annotations

import json
import re

import pytest
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


_TAB_LABEL = re.compile(r'class="category-tab ?[^"]*"[^>]*>([^<(]+?) \(\d+\)</a>')


@pytest.mark.asyncio
async def test_inventory_by_category_chart_uses_the_inventory_tab_labels(owner_ui):
    """The chart's bars carry the same category names as the inventory page's tabs
    ("Colored Stone"), never the schema keys ("colored_stone")."""
    assert (await owner_ui.api.post("/companies/me/business-type", json={"vertical": "gemstones"})).status_code == 200
    assert (await owner_ui.api.post("/companies/me/demo/reseed")).status_code == 200
    names = (await owner_ui.api.get("/companies/me/category-display-names")).json()
    inv = await owner_ui.get("/inventory")
    tabs = [s for s in _TAB_LABEL.findall(inv.text) if s != "All"]
    assert "Colored Stone" in tabs, tabs
    dash = await owner_ui.get("/dashboard")
    assert dash.status_code == 200
    m = re.search(r"labels: (\[[^\]]*\]), datasets: \[\{ label: \"Items\", data: \[[^\]]*\], backgroundColor: colors\[0\]", dash.text)
    assert m, "the category chart is on the dashboard"
    labels = json.loads(m.group(1))
    assert set(tabs) <= set(labels), (tabs, labels)
    assert not set(names) & set(labels), labels
    de = await owner_ui.get("/dashboard", headers={"Accept-Language": "de"})
    assert 'datasets: [{ label: "Artikel", data:' in de.text, "the bar tooltip label is translated"
