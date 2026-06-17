# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: the server-backed Excel column filter (Location funnel) on the inventory list.
Proves the funnel filters the FULL dataset via a query-param reload, not just the visible page."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

SHOTS = Path("context/reviews/inventory")


def test_inventory_location_funnel(page, ui_server, api):
    SHOTS.mkdir(parents=True, exist_ok=True)
    aid = api.post("/companies/me/locations", json={"name": "Vault A", "type": "warehouse"}).json()["id"]
    bid = api.post("/companies/me/locations", json={"name": "Vault B", "type": "warehouse"}).json()["id"]
    api.post("/items", json={"sku": "LOC-AAA", "name": "In vault A", "quantity": 1, "sell_by": "piece", "location_id": aid})
    api.post("/items", json={"sku": "LOC-BBB", "name": "In vault B", "quantity": 1, "sell_by": "piece", "location_id": bid})

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector("#data-table", timeout=10000)
    body = page.locator("#data-table").inner_text()
    assert "LOC-AAA" in body and "LOC-BBB" in body, f"both items should show initially: {body[:300]}"

    # Open the Location funnel, keep only Vault A, Apply -> server reload with ?location_id=.
    funnel = page.locator(".colfilter[data-param='location_id']").first
    assert funnel.count() >= 1, "Location funnel not rendered on the inventory table"
    funnel.click()
    page.wait_for_selector(".colfilter-pop", timeout=5000)
    page.locator(".colfilter-pop .colfilter-item", has_text="Vault B").locator("input").uncheck()
    page.locator(".colfilter-pop .colfilter-foot button:has-text('Apply')").click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#data-table", timeout=10000)

    assert "location_id=" in page.url, f"filter not applied to the URL: {page.url}"
    body2 = page.locator("#data-table").inner_text()
    assert "LOC-AAA" in body2 and "LOC-BBB" not in body2, (
        f"Location funnel should leave only Vault A items: {body2[:300]}"
    )
    # The funnel shows its active state after filtering.
    assert page.locator(".colfilter[data-param='location_id'].colfilter--active").count() >= 1
    page.screenshot(path=str(SHOTS / "inventory-location-funnel.png"), full_page=True)


def test_inventory_attribute_funnel(page, ui_server, api):
    """A category-specific attribute column (stone_type) gets a server-backed Excel funnel that
    filters the full dataset by attribute value."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    assert api.post("/companies/me/apply-preset?vertical=gemstones").status_code == 200
    api.post("/items", json={"name": "Ruby", "sku": "GEM-R", "sell_by": "piece",
                             "category": "Colored Stone", "quantity": 1, "attributes": {"stone_type": "Ruby"}})
    api.post("/items", json={"name": "Sapphire", "sku": "GEM-S", "sell_by": "piece",
                             "category": "Colored Stone", "quantity": 1, "attributes": {"stone_type": "Sapphire"}})

    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{ui_server}/inventory?category=Colored+Stone&cols=sku,name,stone_type",
              wait_until="domcontentloaded")
    page.wait_for_selector("#data-table", timeout=10000)
    body = page.locator("#data-table").inner_text()
    assert "GEM-R" in body and "GEM-S" in body, f"both stones should show: {body[:300]}"

    funnel = page.locator(".colfilter[data-param='attr.stone_type']").first
    assert funnel.count() >= 1, "stone_type attribute funnel not rendered"
    funnel.click()
    page.wait_for_selector(".colfilter-pop", timeout=5000)
    page.locator(".colfilter-pop .colfilter-item", has_text="Sapphire").locator("input").uncheck()
    page.locator(".colfilter-pop .colfilter-foot button:has-text('Apply')").click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#data-table", timeout=10000)

    assert "attr.stone_type=" in page.url, f"attribute filter not in URL: {page.url}"
    body2 = page.locator("#data-table").inner_text()
    assert "GEM-R" in body2 and "GEM-S" not in body2, (
        f"stone_type funnel should leave only Ruby: {body2[:300]}"
    )
    page.screenshot(path=str(SHOTS / "inventory-attribute-funnel.png"), full_page=True)
