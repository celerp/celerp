# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Playwright audit: vertical-specific inventory columns.

Verifies that when a category tab is selected (e.g. 'Colored Stone'),
the column manager shows category-specific fields (stone_type, clarity, etc.)
and those columns appear in the table header.

Steps:
1. Apply the gemstones preset via API
2. Seed two items: one with category='Colored Stone', one generic
3. Navigate to /inventory
4. Assert 'Colored Stone' tab appears
5. Click 'Colored Stone' tab
6. Open column manager → assert gemstone field labels listed
7. Enable stone_type + clarity columns → apply
8. Assert Stone Type + Clarity appear in table header
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# 'carat' was removed from the gemstone category schema in the weight-unit
# refactor (it's now the universal weight/weight_unit item field), so use
# 'clarity' as the representative gemstone category field instead.
_GEMSTONE_FIELD_LABELS = ["Stone Type", "Clarity", "Origin"]
_GEMSTONE_CATEGORY = "Colored Stone"


@pytest.fixture(scope="module")
def gemstone_items(api):
    """Apply gemstones preset and seed two items."""
    # Apply preset (seeds category schemas)
    r = api.post("/companies/me/apply-preset?vertical=gemstones")
    assert r.status_code == 200, f"apply-preset failed: {r.text}"

    # Seed items
    item1 = api.post("/items", json={
        "name": "Ruby Cabochon",
        "sku": "GEM-001",
        "sell_by": "piece",
        "category": _GEMSTONE_CATEGORY,
        "quantity": 1,
        "cost": 5000,
        "attributes": {"stone_type": "Ruby", "clarity": "VS", "origin": "Myanmar"},
    })
    assert item1.status_code == 200, f"item1 create failed: {item1.text}"

    item2 = api.post("/items", json={
        "name": "Generic Widget",
        "sku": "GEN-001",
        "sell_by": "piece",
        "category": "General",
        "quantity": 10,
        "cost": 100,
    })
    assert item2.status_code == 200, f"item2 create failed: {item2.text}"

    yield {
        "item1_id": item1.json().get("entity_id") or item1.json().get("id"),
        "item2_id": item2.json().get("entity_id") or item2.json().get("id"),
    }


def test_category_tab_appears(page, gemstone_items):
    """Colored Stone category tab must be visible on /inventory."""
    page.goto("/inventory")
    page.wait_for_selector(".category-tabs", timeout=5000)
    tab_text = page.inner_text(".category-tabs")
    assert _GEMSTONE_CATEGORY in tab_text, (
        f"Expected '{_GEMSTONE_CATEGORY}' tab but got tabs: {tab_text!r}"
    )


def test_column_manager_shows_gemstone_field_labels_when_category_active(page, gemstone_items):
    """When Colored Stone tab is active, column manager must list gemstone field labels."""
    page.goto("/inventory")
    page.wait_for_selector(".category-tabs", timeout=5000)

    # Click the Colored Stone tab
    tab = page.locator(".category-tab", has_text=_GEMSTONE_CATEGORY).first
    assert tab.count() > 0, f"No tab found for '{_GEMSTONE_CATEGORY}'"
    tab.click()
    page.wait_for_url("**/inventory**category**", timeout=5000)

    # Open column manager
    page.locator("#col-mgr-btn").click()  # column-manager toggle button
    page.wait_for_selector(".column-menu", timeout=3000)

    column_menu_text = page.inner_text(".column-menu")
    missing = [label for label in _GEMSTONE_FIELD_LABELS if label not in column_menu_text]
    assert not missing, (
        f"Column manager missing gemstone field labels {missing} when category='{_GEMSTONE_CATEGORY}'. "
        f"Got: {column_menu_text!r}"
    )


def test_gemstone_columns_appear_in_table_after_enabling(page, gemstone_items):
    """Enable Stone Type + Clarity → they must appear as table headers."""
    page.goto(f"/inventory?category={_GEMSTONE_CATEGORY}")
    page.wait_for_selector(".data-table", timeout=5000)

    # Open column manager
    page.locator("#col-mgr-btn").click()  # column-manager toggle button
    page.wait_for_selector(".column-menu", timeout=3000)

    # Check stone_type and clarity boxes if not already checked
    for field in ("stone_type", "clarity"):
        cb = page.locator(f'input[name="cols"][value="{field}"]')
        if cb.count() > 0 and not cb.is_checked():
            cb.check()

    # Checking a box immediately shows the column (JS toggle + localStorage);
    # there is no separate apply/submit button.
    page.wait_for_selector("th:has-text('Stone Type')", timeout=5000)

    headers = page.inner_text("thead").upper()
    for label in ("STONE TYPE", "CLARITY"):
        assert label in headers, (
            f"Expected column header '{label}' not found in table headers: {headers!r}"
        )


def test_all_tab_does_not_show_gemstone_cols_by_default(page, gemstone_items):
    """All-items view must NOT show gemstone cols by default (too noisy)."""
    page.goto("/inventory")
    page.wait_for_selector(".data-table", timeout=5000)
    # Column prefs persist (localStorage + server) and leak across tests through
    # the shared session context, so reset to defaults to test the *default* view.
    page.evaluate("""async () => {
        localStorage.removeItem('celerp_cols_inventory');
        localStorage.removeItem('celerp_col_order_inventory');
        localStorage.removeItem('celerp_col_widths_inventory');
        await fetch('/inventory/columns', {method: 'POST', body: new FormData()});
    }""")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".data-table", timeout=5000)

    headers = page.inner_text("thead").upper()
    # In 'All' view, gemstone columns should be hidden by default (show_in_table=False)
    assert "STONE TYPE" not in headers, (
        f"'Stone Type' column should be hidden in All-items view but found in headers: {headers!r}"
    )


def test_cat_schemas_key_matches_item_category(api, gemstone_items):
    """
    Direct API audit: cat_schemas keys must match item category values for seeded items.
    Ignores pre-existing categories that have no schema (they're fine - generic items).
    """
    # Get category schemas (keys should be display_names like 'Colored Stone')
    r = api.get("/companies/me/category-schemas")
    assert r.status_code == 200
    cat_schemas = r.json()
    schema_keys = set(cat_schemas.keys())

    # Verify 'Colored Stone' schema was seeded with gemstone fields
    assert _GEMSTONE_CATEGORY in schema_keys, (
        f"'{_GEMSTONE_CATEGORY}' not in cat_schema keys: {sorted(schema_keys)}"
    )
    cs_fields = {f["key"] for f in cat_schemas[_GEMSTONE_CATEGORY]}
    for expected_field in ("stone_type", "clarity", "origin"):
        assert expected_field in cs_fields, (
            f"Field '{expected_field}' missing from '{_GEMSTONE_CATEGORY}' schema. Got: {cs_fields}"
        )

    # Verify item was stored with the matching category value
    r2 = api.get("/items?limit=100")
    assert r2.status_code == 200
    items = r2.json().get("items", r2.json()) if isinstance(r2.json(), dict) else r2.json()
    gemstone_item = next((i for i in items if i.get("sku") == "GEM-001"), None)
    assert gemstone_item is not None, "Seeded GEM-001 item not found"
    assert gemstone_item.get("category") == _GEMSTONE_CATEGORY, (
        f"GEM-001 category={gemstone_item.get('category')!r}, expected {_GEMSTONE_CATEGORY!r}"
    )
