# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression: repricing a memo must reprice each line against the exact inventory
lot it was added from, not the first item found by a SKU lookup.

Bug: both reprice paths (the price_list field-edit and the /reprice endpoint) looked
up the current price by `list_items(token, {"sku": sku, "limit": 1})` and used
`items[0]`, ignoring the `item_id` already stored on the line. When two lots share
a SKU with different prices, reprice can silently swap in a sibling lot's price.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.browser


@pytest.fixture()
def duplicate_sku_memo(fresh_company):
    """Two items sharing one SKU with different prices, and a memo whose single
    line references the SECOND item (the one list_items would NOT return first)."""
    api = fresh_company
    r = api.patch("/companies/me/price-lists", json={
        "price_lists": [{"name": "Retail"}, {"name": "Wholesale"}],
        "base_price_list": "Retail",
    })
    assert r.status_code in {200, 201}, r.text

    sku = f"DUP-{uuid.uuid4().hex[:6]}"
    r_a = api.post("/items", json={"sku": sku, "name": "Lot A", "sell_by": "piece",
                                   "quantity": 1, "retail_price": 100.0, "wholesale_price": 50.0})
    assert r_a.status_code == 200, r_a.text
    r_b = api.post("/items", json={"sku": sku, "name": "Lot B", "sell_by": "piece",
                                   "quantity": 1, "retail_price": 200.0, "wholesale_price": 150.0})
    assert r_b.status_code == 200, r_b.text
    item_b_id = r_b.json()["id"]

    item_a_id = r_a.json()["id"]
    r = api.post("/items/bulk/make-available", json={"entity_ids": [item_a_id, item_b_id]})
    assert r.status_code == 200, r.text

    # list_items (unsorted) defaults to most-recently-updated-first: touch Lot A last so
    # it sorts ahead of Lot B, reproducing the exact bug - a plain sku lookup's items[0]
    # lands on the WRONG lot (A) even though this memo's line points at Lot B.
    r = api.patch(f"/items/{item_a_id}", json={"location_name": "Main"})
    assert r.status_code == 200, r.text

    r = api.post("/docs", json={
        "doc_type": "memo",
        "status": "draft",
        "price_list": "Retail",
        "line_items": [
            {"sku": sku, "item_id": item_b_id, "name": "Lot B", "quantity": 1,
             "unit_price": 200.0, "line_total": 200.0},
        ],
        "total": 200.0,
    })
    assert r.status_code in {200, 201}, r.text
    return api, r.json()["id"], item_b_id


def _line_items(api, doc_id):
    r = api.get(f"/docs/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_reprice_via_price_list_field_uses_lines_own_lot(ui_server, duplicate_sku_memo):
    api, doc_id, item_b_id = duplicate_sku_memo
    token = api.headers["Authorization"].removeprefix("Bearer ")

    with httpx.Client(base_url=ui_server, cookies={"celerp_token": token}, timeout=10) as ui:
        r = ui.patch(f"/docs/{doc_id}/field/price_list", data={"value": "Wholesale"})
        assert r.status_code in (200, 204), r.text

    doc = _line_items(api, doc_id)
    line = doc["line_items"][0]
    assert line["item_id"] == item_b_id
    assert line["unit_price"] == 150.0, (
        f"reprice picked the wrong lot's price: got {line['unit_price']}, "
        f"expected Lot B's wholesale_price (150.0), not Lot A's (50.0)"
    )


def test_reprice_endpoint_uses_lines_own_lot(ui_server, duplicate_sku_memo):
    api, doc_id, item_b_id = duplicate_sku_memo
    token = api.headers["Authorization"].removeprefix("Bearer ")

    with httpx.Client(base_url=ui_server, cookies={"celerp_token": token}, timeout=10) as ui:
        r = ui.post(f"/docs/{doc_id}/reprice", json={"price_list": "Wholesale"})
        assert r.status_code == 200, r.text

    doc = _line_items(api, doc_id)
    line = doc["line_items"][0]
    assert line["item_id"] == item_b_id
    assert line["unit_price"] == 150.0, (
        f"reprice picked the wrong lot's price: got {line['unit_price']}, "
        f"expected Lot B's wholesale_price (150.0), not Lot A's (50.0)"
    )
