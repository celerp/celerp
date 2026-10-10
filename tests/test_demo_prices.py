# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Demo item prices land on the items, and demo items change only through the loaded
Inventory module."""
import pytest
from unittest.mock import patch as mock_patch

async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Acme", "email": "admin@acme.example", "name": "Admin", "password": "pwvalid1"
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

@pytest.mark.asyncio
async def test_prices_with_handler_loaded(client):
    """Normal case: inventory module loaded, prices should work."""
    h = await _headers(client)
    r = await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=h)
    assert r.status_code == 200, r.text
    
    items = (await client.get("/items", headers=h)).json()["items"]
    assert len(items) >= 3
    for it in items[:3]:
        rp = it.get("retail_price")
        print(f"  {it['sku']}: retail={rp}")
        assert rp is not None and rp > 0, f"{it['sku']} missing retail_price!"

@pytest.mark.asyncio
async def test_a_business_type_set_without_inventory_loaded_leaves_the_items_alone(client):
    """With the Inventory module not loaded, setting the business type writes no item
    change (nothing could apply one): the items stay as registration seeded them."""
    h = await _headers(client)
    before = (await client.get("/items", headers=h)).json()["items"]

    with mock_patch("celerp.projections.engine._get_module_handlers", return_value={}):
        r = await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=h)
        assert r.status_code == 200, r.text

    assert r.json()["changes"]["demo_items_replaced"] == 0
    after = (await client.get("/items", headers=h)).json()["items"]
    assert sorted(i["sku"] for i in after) == sorted(i["sku"] for i in before)
    assert all((i.get("retail_price") or 0) > 0 for i in after), after

@pytest.mark.asyncio
async def test_ui_table_renders_demo_prices(client):
    """The reseeded demo items render their prices in the inventory table."""
    h = await _headers(client)

    r = await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=h)
    assert r.status_code == 200, r.text
    
    schema = (await client.get("/companies/me/item-schema", headers=h)).json()
    items = (await client.get("/items", headers=h)).json()["items"]
    
    from ui.components.table import data_table
    from fasthtml.common import to_xml
    import re
    
    visible = [f["key"] for f in schema if f.get("show_in_table", True)]
    html = to_xml(data_table(schema, items, entity_type="inventory", show_cols=visible, currency="THB"))
    
    money_cells = re.findall(r'class="cell-money">([^<]+)', html)
    real_money = [c for c in money_cells if c.strip() != "--"]
    empty_money = [c for c in money_cells if c.strip() == "--"]
    
    print(f"\nUI table: {len(real_money)} real prices, {len(empty_money)} empty")
    print(f"Sample: {real_money[:6]}")
    
    assert len(real_money) >= 10, f"Expected at least 10 real prices, got {len(real_money)}"
