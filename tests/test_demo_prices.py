# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Test that demo item prices work even without the inventory projection handler.

Root cause: setup wizard calls reseed BEFORE restarting the API, so the
celerp-inventory module's projection handler isn't loaded. The default
fallback handler {**state, **data} doesn't interpret item.pricing.set
events correctly. Fix: include prices in attributes of item.created.
"""
import pytest
from unittest.mock import patch as mock_patch

async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}

@pytest.mark.asyncio
async def test_prices_with_handler_loaded(client):
    """Normal case: inventory module loaded, prices should work."""
    h = await _headers(client)
    await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=h)
    await client.post("/companies/me/demo/reseed?vertical=gemstones", headers=h)
    
    items = (await client.get("/items", headers=h)).json()["items"]
    assert len(items) >= 3
    for it in items[:3]:
        rp = it.get("retail_price")
        print(f"  {it['sku']}: retail={rp}")
        assert rp is not None and rp > 0, f"{it['sku']} missing retail_price!"

@pytest.mark.asyncio
async def test_prices_without_handler(client):
    """Simulates wizard flow: inventory handler NOT loaded during reseed.
    Prices should still appear via attributes promotion."""
    h = await _headers(client)
    await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=h)
    
    # Mock _get_module_handlers to return empty dict (no inventory handler)
    with mock_patch("celerp.projections.engine._get_module_handlers", return_value={}):
        r = await client.post("/companies/me/demo/reseed?vertical=gemstones", headers=h)
        assert r.status_code == 200
    
    items = (await client.get("/items", headers=h)).json()["items"]
    print(f"\nItems without handler: {len(items)}")
    for it in items[:3]:
        rp = it.get("retail_price")
        wp = it.get("wholesale_price")
        cp = it.get("cost_price")
        print(f"  {it['sku']}: retail={rp}, wholesale={wp}, cost={cp}")
        assert rp is not None and rp > 0, f"{it['sku']} missing retail_price without handler!"
        assert wp is not None and wp > 0, f"{it['sku']} missing wholesale_price!"
        assert cp is not None and cp > 0, f"{it['sku']} missing cost_price!"

@pytest.mark.asyncio
async def test_ui_table_renders_prices_without_handler(client):
    """Full simulation: reseed without handler, then render data_table."""
    h = await _headers(client)
    await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=h)
    
    with mock_patch("celerp.projections.engine._get_module_handlers", return_value={}):
        await client.post("/companies/me/demo/reseed?vertical=gemstones", headers=h)
    
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
