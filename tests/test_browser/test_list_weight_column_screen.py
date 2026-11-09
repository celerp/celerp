# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: the Lists table's Weight column shows what the printed List shows.

Gemstone lots sold by the carat weigh their quantity in carats, and a line saved the way the
line-item picker saves it (the item's weight, no unit of its own) weighs in the item's unit.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ui.components.table import display_unit

from .conftest import wait_for_topbar
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser


def _weight_cell(page: Page, ui_server: str, ref: str):
    page.goto(f"{ui_server}/lists?status=draft")
    wait_for_topbar(page)
    headers = [h.strip() for h in page.locator("#list-table th").all_text_contents()]
    row = page.locator("#list-table tr.data-row", has=page.get_by_role("link", name=ref, exact=True))
    expect(row).to_be_visible(timeout=15000)
    return row.locator("td").nth(headers.index("Weight"))


def test_the_weight_column_shows_the_printed_weight(page: Page, ui_server, fresh_company):
    api = fresh_company
    _seed_demo_items(api, "gemstones")
    items = api.get("/items", params={"limit": 200}).json()["items"]
    lots = [i for i in items if i.get("sell_by") == "carat" and float(i.get("quantity") or 0) > 0][:2]
    carats = sum(float(i["quantity"]) for i in lots)
    r = api.post("/lists", json={"list_type": "quotation", "line_items": [
        {"item_id": i["id"], "sku": i["sku"], "name": i["name"], "quantity": float(i["quantity"]), "unit_price": 10}
        for i in lots]})
    assert r.status_code == 200, r.text
    gems = api.get(f"/lists/{r.json()['id']}").json()["ref_id"]

    piece = next(i for i in items if i.get("sell_by") not in ("carat", None) and float(i.get("quantity") or 0) >= 1)
    r = api.patch(f"/items/{piece['id']}", json={"fields_changed": {"weight": {"new": 2.5}, "weight_unit": {"new": "kg"}}})
    assert r.status_code == 200, r.text
    r = api.post("/lists", json={"list_type": "quotation", "line_items": [
        {"item_id": piece["id"], "sku": piece["sku"], "name": piece["name"], "quantity": 1, "unit_price": 10,
         "weight": 2.5}]})
    assert r.status_code == 200, r.text
    picked = api.get(f"/lists/{r.json()['id']}").json()["ref_id"]

    expect(_weight_cell(page, ui_server, gems)).to_have_text(f"{carats:g} {display_unit('carat')}")
    expect(_weight_cell(page, ui_server, picked)).to_have_text(f"2.5 {display_unit('kg')}")
