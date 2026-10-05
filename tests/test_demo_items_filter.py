# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GET /items?filter=demo lists setup's samples that are still removable: seeded by
setup and never edited or used since, the same set a first product import removes.
The dashboard's "Remove demo items" link opens that list and offers Delete on it, so
an edited sample, a renamed one, one used on a document and the owner's own item
named "[DEMO] ..." must never be on it."""
from __future__ import annotations

import uuid

import pytest

from ui.components.demo_items import DEMO_ITEMS_FILTER


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "DemoCo", "email": f"demo-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=h)
    assert r.status_code == 200, r.text
    return h


async def _skus(client, h, **params) -> dict[str, dict]:
    r = await client.get("/items", headers=h, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return {i["sku"]: i for i in r.json()["items"]}


@pytest.mark.asyncio
async def test_demo_filter_lists_only_untouched_samples_and_delete_keeps_the_rest(client):
    """Red statement: before the change ?filter=demo is ignored and the link searched
    name:[DEMO], so the list held the edited and used samples and MINE-1, and Delete
    on it removed them with their history."""
    h = await _owner(client)
    demo = {s: i for s, i in (await _skus(client, h)).items() if s.startswith("DEMO-AGR-")}
    assert len(demo) == 5, sorted(demo)
    edited, renamed, used = demo["DEMO-AGR-001"], demo["DEMO-AGR-005"], demo["DEMO-AGR-002"]
    r = await client.patch(f"/items/{edited['id']}", headers=h, json={
        "fields_changed": {"quantity": {"old": edited.get("quantity"), "new": 999}}})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/items/{renamed['id']}", headers=h, json={
        "fields_changed": {"name": {"old": renamed["name"], "new": "House cattle feed"}}})
    assert r.status_code == 200, r.text
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c:1", "contact_name": "Buyer",
        "line_items": [{"item_id": used["id"], "sku": used["sku"], "description": "Tomatoes",
                        "quantity": 1, "unit_price": 5, "line_total": 5}],
    })
    assert r.status_code == 200, r.text
    r = await client.post("/items", headers=h, json={
        "sku": "MINE-1", "name": "[DEMO] My own showroom piece", "sell_by": "piece", "quantity": 1})
    assert r.status_code == 200, r.text

    listed = await _skus(client, h, filter=DEMO_ITEMS_FILTER)
    assert set(listed) == {"DEMO-AGR-003", "DEMO-AGR-004"}, sorted(listed)

    # Select-all plus Delete on that list: exactly the listed ids.
    r = await client.post("/items/bulk/delete", headers=h, json={"entity_ids": [i["id"] for i in listed.values()]})
    assert r.status_code == 200, r.text
    left = await _skus(client, h, status="all")
    assert {"DEMO-AGR-001", "DEMO-AGR-002", "DEMO-AGR-005", "MINE-1"} <= set(left)
    assert left["DEMO-AGR-001"]["quantity"] == 999
    assert not {"DEMO-AGR-003", "DEMO-AGR-004"} & set(left)
    assert await _skus(client, h, filter=DEMO_ITEMS_FILTER) == {}
