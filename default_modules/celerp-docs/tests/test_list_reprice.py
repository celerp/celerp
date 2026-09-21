# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Whole-List repricing preserves identity, concurrency, and document snapshots."""
from __future__ import annotations

import uuid

import pytest


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Reprice Co",
        "email": f"reprice-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _item(client, token: str, *, sku: str, retail: float, wholesale: float | None = None) -> str:
    body = {
        "status": "available",
        "sku": sku,
        "name": sku,
        "quantity": 10,
        "sell_by": "piece",
        "retail_price": retail,
    }
    if wholesale is not None:
        body["wholesale_price"] = wholesale
    r = await client.post("/items", headers=_h(token), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _quotation(client, token: str, lines: list[dict]) -> tuple[str, int]:
    r = await client.post("/lists", headers=_h(token), json={
        "list_type": "quote",
        "price_list": "Retail",
        "currency": "USD",
        "line_items": lines,
    })
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    state = (await client.get(f"/lists/{entity_id}", headers=_h(token))).json()
    return entity_id, state["version"]


@pytest.mark.asyncio
async def test_reprice_updates_exact_catalog_lines_and_preserves_manual_lines(client):
    token = await _register(client)
    item_id = await _item(client, token, sku="SAME-SKU", retail=100, wholesale=80)
    list_id, version = await _quotation(client, token, [
        {
            "item_id": item_id, "sku": "SAME-SKU", "description": "Catalog",
            "quantity": 2, "unit_price": 100, "discount_pct": 10, "tax_rate": 10,
            "line_total": 180,
        },
        {
            # Same SKU deliberately: no item id means manual/free-text, not a catalog lookup.
            "sku": "SAME-SKU", "description": "Manual",
            "quantity": 1, "unit_price": 777, "line_total": 777,
        },
    ])

    r = await client.post(f"/lists/{list_id}/reprice", headers=_h(token), json={
        "price_list": "Wholesale", "expected_version": version,
    })
    assert r.status_code == 200, r.text
    assert r.json()["repriced"] == 1
    assert r.json()["skipped"] == []

    state = (await client.get(f"/lists/{list_id}", headers=_h(token))).json()
    catalog, manual = state["line_items"]
    assert catalog["unit_price"] == 80.0
    assert catalog["line_total"] == 144.0
    assert manual["unit_price"] == 777
    assert manual["line_total"] == 777
    assert state["price_list"] == "Wholesale"
    assert state["subtotal"] == pytest.approx(921.0)
    assert state["tax_amount"] == pytest.approx(14.4)
    assert state["total"] == pytest.approx(935.4)


@pytest.mark.asyncio
async def test_reprice_missing_link_keeps_snapshot_and_never_falls_back_to_same_sku(client):
    token = await _register(client)
    h = _h(token)
    missing_id = await _item(client, token, sku="REUSED-SKU", retail=125, wholesale=95)
    live_id = await _item(client, token, sku="LIVE-SKU", retail=60, wholesale=45)
    list_id, version = await _quotation(client, token, [
        {
            "item_id": missing_id, "sku": "REUSED-SKU", "description": "Deleted original",
            "quantity": 2, "unit_price": 125, "line_total": 250,
        },
        {
            "item_id": live_id, "sku": "LIVE-SKU", "description": "Still live",
            "quantity": 1, "unit_price": 60, "line_total": 60,
        },
        {
            "description": "Manual service", "quantity": 1, "unit_price": 33, "line_total": 33,
        },
    ])

    deleted = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": [missing_id]})
    assert deleted.status_code == 200, deleted.text
    replacement_id = await _item(client, token, sku="REUSED-SKU", retail=999, wholesale=888)
    assert replacement_id != missing_id

    r = await client.post(f"/lists/{list_id}/reprice", headers=h, json={
        "price_list": "Wholesale", "expected_version": version,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repriced"] == 1
    assert body["skipped"] == [{"item_id": missing_id, "reason": "item_not_found"}]

    state = (await client.get(f"/lists/{list_id}", headers=h)).json()
    missing, live, manual = state["line_items"]
    assert missing["item_id"] == missing_id
    assert missing["unit_price"] == 125
    assert missing["line_total"] == 250
    assert live["unit_price"] == 45.0
    assert live["line_total"] == 45.0
    assert manual["unit_price"] == 33
    assert state["subtotal"] == pytest.approx(328.0)
    assert state["total"] == pytest.approx(328.0)


@pytest.mark.asyncio
async def test_reprice_resolves_derived_price_list(client):
    token = await _register(client)
    h = _h(token)
    r = await client.patch("/companies/me/price-lists", headers=h, json={
        "price_lists": [
            {"name": "Retail"},
            {"name": "Trade", "multiplier": 0.7, "rounding": 5},
            {"name": "Cost"},
        ],
        "base_price_list": "Retail",
    })
    assert r.status_code == 200, r.text
    item_id = await _item(client, token, sku="DERIVED-1", retail=719)
    list_id, version = await _quotation(client, token, [{
        "item_id": item_id, "sku": "DERIVED-1",
        "quantity": 1, "unit_price": 719, "line_total": 719,
    }])

    r = await client.post(f"/lists/{list_id}/reprice", headers=h, json={
        "price_list": "Trade", "expected_version": version,
    })
    assert r.status_code == 200, r.text
    state = (await client.get(f"/lists/{list_id}", headers=h)).json()
    assert state["line_items"][0]["unit_price"] == 505.0
    assert state["line_items"][0]["line_total"] == 505.0


@pytest.mark.asyncio
async def test_reprice_rejects_stale_version_without_mutation(client):
    token = await _register(client)
    h = _h(token)
    item_id = await _item(client, token, sku="STALE-1", retail=100, wholesale=80)
    list_id, stale_version = await _quotation(client, token, [{
        "item_id": item_id, "sku": "STALE-1",
        "quantity": 1, "unit_price": 100, "line_total": 100,
    }])

    bump = await client.patch(f"/lists/{list_id}", headers=h, json={
        "fields_changed": {"reference": {"new": "other edit"}},
    })
    assert bump.status_code == 200, bump.text

    r = await client.post(f"/lists/{list_id}/reprice", headers=h, json={
        "price_list": "Wholesale", "expected_version": stale_version,
    })
    assert r.status_code == 409, r.text
    state = (await client.get(f"/lists/{list_id}", headers=h)).json()
    assert state["price_list"] == "Retail"
    assert state["line_items"][0]["unit_price"] == 100


@pytest.mark.asyncio
async def test_reprice_rejects_unknown_list_and_requires_version(client):
    token = await _register(client)
    h = _h(token)
    item_id = await _item(client, token, sku="BAD-PL", retail=100)
    list_id, version = await _quotation(client, token, [{
        "item_id": item_id, "sku": "BAD-PL",
        "quantity": 1, "unit_price": 100, "line_total": 100,
    }])

    missing = await client.post(
        f"/lists/{list_id}/reprice", headers=h, json={"price_list": "Retail"},
    )
    assert missing.status_code == 422

    unknown = await client.post(f"/lists/{list_id}/reprice", headers=h, json={
        "price_list": "Does not exist", "expected_version": version,
    })
    assert unknown.status_code == 422
