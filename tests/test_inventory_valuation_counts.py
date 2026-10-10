# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory tabs and status cards (/items/valuation counts) describe exactly the
rows the list (/items) shows for the same filters: service, non-stocked and
consigned-in rows included, comma multi-values read as the list reads them. Only the
money figures keep to owned stocked goods."""
from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection

# sku, name, category, status, inventory_type, color, location index, retail price
_SPEC = [
    ("G1", "Ruby ring", "Gem", "available", "stocked", "red", 0, 100),
    ("G2", "Sapphire pendant", "Gem", "reserved", "stocked", "blue", 1, 200),
    ("G3", "Ring cleaning", "Gem", "available", "service", "red", 0, 400),
    ("G4", "Ruby draft ring", "Gem", "draft", "stocked", "red", 0, 800),
    ("G5", "Ruby sold ring", "Gem", "sold", "stocked", "red", 0, 1600),
    ("A1", "Gold ring", "Gold", "available", "stocked", "red", 1, 3200),
    ("A2", "Gold chain", "Gold", "available", "non_stocked", "blue", 0, 6400),
    ("A3", "Gold bar", "Gold", "archived", "stocked", "blue", 0, 12800),
    ("U1", "Loose ring", "", "available", "stocked", "red", 0, 25600),
    ("C1", "Consigned ring", "Gem", "available", "stocked", "red", 0, 51200),
]


async def _company(client, session) -> tuple[dict, list[str]]:
    r = await client.post("/auth/register", json={
        "company_name": "Counts Co", "email": f"counts-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # Registration seeds a sample; the counts here are about this file's own items.
    items = (await client.get("/items", headers=h, params={"status": "all", "limit": 500})).json()["items"]
    if items:
        r = await client.post("/items/bulk/delete", headers=h, json={
            "entity_ids": [i["id"] for i in items], "untouched_samples_only": True})
        assert r.status_code == 200, r.text
    locs = []
    for name in ("Shop A", "Shop B"):
        r = await client.post("/companies/me/locations", headers=h, json={
            "name": name, "type": "warehouse", "address": None, "is_default": False})
        assert r.status_code == 200, r.text
        locs.append(r.json()["id"])
    for sku, name, cat, status, itype, color, loc, price in _SPEC:
        body = {"sku": sku, "name": name, "sell_by": "piece", "quantity": 1, "inventory_type": itype,
                "attributes": {"color": color}, "location_id": locs[loc],
                "status": status if status in ("draft", "available") else "available", "retail_price": price}
        if cat:
            body["category"] = cat
        r = await client.post("/items", headers=h, json=body)
        assert r.status_code == 200, r.text
        # Statuses an item reaches only through later actions, and consigned-in goods
        # (received through a consignment document), are recorded as those leave them.
        recorded = {"status": status} if status not in ("draft", "available") else {}
        if sku == "C1":
            recorded["consignment_flag"] = "in"
        if recorded:
            row = (await session.execute(select(Projection).where(
                Projection.entity_id == r.json()["id"]))).scalar_one()
            row.state = {**row.state, **recorded}
            row.consignment_flag = recorded.get("consignment_flag", row.consignment_flag)
            await session.commit()
    seeded = {i["sku"]: i for i in (await client.get(
        "/items", headers=h, params={"status": "all", "limit": 500})).json()["items"]}
    assert {s: (i.get("status"), i.get("inventory_type")) for s, i in seeded.items()} == {
        s[0]: (s[3], s[4]) for s in _SPEC}
    assert seeded["C1"].get("consignment_flag") == "in", seeded["C1"]
    return h, locs


async def _rows(client, h, params) -> list[dict]:
    r = await client.get("/items", headers=h, params={**params, "limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _valuation(client, h, params) -> dict:
    r = await client.get("/items/valuation", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # 504 combinations, about 1200 requests; slower than the suite guard allows on a shared runner
async def test_counts_equal_rows_for_every_filter_combination(client, session):
    """Red statement: the valuation loop skipped service, non-stocked and consigned-in
    rows and compared status and category as single values, so ?status=available,
    reserved counted 0 over 7 rows and ?inventory_type=service 0 over 1."""
    h, (shop_a, _) = await _company(client, session)
    dims = {
        "q": [None, "ring", "name:ruby"],
        "category": [None, "Gem", "Gem,Gold"],
        "status": [None, "available", "reserved", "available,reserved", "all", "archived", "draft"],
        "location_id": [None, shop_a],
        "attr.color": [None, "red"],
        "inventory_type": [None, "service"],
    }
    bad = []
    # Category counts come from the rows the other filters leave; one fetch per such set.
    cat_counts: dict[tuple, dict[str, int]] = {}
    for combo in itertools.product(*dims.values()):
        params = {k: v for k, v in zip(dims, combo) if v}
        rows = await _rows(client, h, params)
        v = await _valuation(client, h, params)
        others = {k: x for k, x in params.items() if k != "category"}
        key = tuple(sorted(others.items()))
        if key not in cat_counts:
            cats: dict[str, int] = {}
            for i in await _rows(client, h, others):
                if i.get("category"):
                    cats[i["category"]] = cats.get(i["category"], 0) + 1
            cat_counts[key] = cats
        cats = cat_counts[key]
        statuses: dict[str, int] = {}
        for i in rows:
            statuses[i["status"]] = statuses.get(i["status"], 0) + 1
        got = (v["total_scoped_count"], v["count_by_status"], v["category_counts"])
        if got != (len(rows), statuses, cats):
            bad.append((params, got, (len(rows), statuses, cats)))
    assert not bad, f"{len(bad)} combinations disagree, first: {bad[:3]}"


@pytest.mark.asyncio
async def test_money_totals_keep_to_owned_stocked_goods(client, session):
    """The value figures still count only owned stocked goods that are not drafts:
    service, non-stocked, consigned-in and draft rows are listed and counted but carry
    no stock value. These totals are the same before and after counts moved to the
    list's rows."""
    h, (shop_a, _) = await _company(client, session)
    cases = [
        ({}, 100 + 200 + 3200 + 25600),
        ({"status": "available"}, 100 + 3200 + 25600),
        ({"category": "Gem"}, 100 + 200),
        ({"status": "all"}, 100 + 200 + 1600 + 3200 + 12800 + 25600),
        ({"q": "ring", "location_id": shop_a}, 100 + 25600),
        ({"inventory_type": "service"}, 0),
    ]
    for params, retail in cases:
        v = await _valuation(client, h, params)
        assert v["retail_total"] == retail, (params, v)
        assert v["price_totals"]["Retail"] == retail, (params, v)
