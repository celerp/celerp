# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Document fulfillment activity: events name the items (sku x qty), and reverting some-but-
not-all lines reads as a revert (doc.partially_reverted) rather than a partial fulfillment."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@fulact.test"
    r = await client.post("/auth/register", json={"company_name": "FulAct Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _doc_events(client, h, doc_id):
    r = await client.get("/ledger", params={"entity_id": doc_id, "limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.asyncio
async def test_partial_revert_reads_as_revert_with_items(client):
    token = await _register(client)
    h = _h(token)
    a = (await client.post("/items", headers=h, json={"sku": "ITEM-A", "name": "A", "quantity": 2, "sell_by": "piece"})).json()["id"]
    b = (await client.post("/items", headers=h, json={"sku": "ITEM-B", "name": "B", "quantity": 3, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": a, "sku": "ITEM-A", "name": "A", "quantity": 2, "unit_price": 10, "sell_by": "piece"},
        {"entity_id": b, "sku": "ITEM-B", "name": "B", "quantity": 3, "unit_price": 10, "sell_by": "piece"}],
    })).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200

    # Fulfill all → doc.fulfilled naming both items (issue 1).
    assert (await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [a, b]})).status_code == 200
    ful = [e for e in await _doc_events(client, h, doc) if e["event_type"] == "doc.fulfilled"]
    assert ful, "fulfilling all lines must emit doc.fulfilled"
    skus = {it.get("sku") for it in ful[0]["data"]["fulfilled_items"]}
    assert {"ITEM-A", "ITEM-B"} <= skus, f"doc.fulfilled must name the items; got {skus}"

    # Revert ONE line → doc.partially_reverted (NOT doc.partially_fulfilled), naming what was
    # reverted (issue 2).
    assert (await client.post(f"/docs/{doc}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    events = await _doc_events(client, h, doc)
    types = [e["event_type"] for e in events]
    assert "doc.partially_reverted" in types, f"partial revert must read as a revert; got {types}"
    assert "doc.partially_fulfilled" not in types, "a revert must not be logged as a partial fulfillment"
    pr = [e for e in events if e["event_type"] == "doc.partially_reverted"][0]
    reverted = {it.get("sku") for it in pr["data"]["reversed_items"]}
    assert reverted == {"ITEM-A"}, f"only the reverted item should be named; got {reverted}"

    # The doc itself remains partially fulfilled.
    docstate = (await client.get(f"/docs/{doc}", headers=h)).json()
    assert docstate.get("fulfillment_status") == "partial"


@pytest.mark.asyncio
async def test_full_revert_still_fulfillment_reversed(client):
    token = await _register(client)
    h = _h(token)
    a = (await client.post("/items", headers=h, json={"sku": "FR-A", "name": "A", "quantity": 2, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": a, "sku": "FR-A", "name": "A", "quantity": 2, "unit_price": 10, "sell_by": "piece"}],
    })).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/docs/{doc}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{doc}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    types = [e["event_type"] for e in await _doc_events(client, h, doc)]
    assert "doc.fulfillment_reversed" in types and "doc.partially_reverted" not in types


def test_doc_fulfillment_detail_rendering():
    from ui.components.activity import detail_from_entry, event_label
    assert event_label("doc.partially_reverted") == "Partially reverted"
    assert detail_from_entry({"fulfilled_items": [{"sku": "RING-01", "quantity": 2},
                                                  {"sku": "LOOSE-03", "quantity": 1}]}, "doc.fulfilled") == "RING-01 ×2, LOOSE-03 ×1"
    assert detail_from_entry({"reversed_items": [{"sku": "RING-01", "quantity": 2}]}, "doc.partially_reverted") == "RING-01 ×2"
    assert detail_from_entry({"fulfilled_items": [{"sku": "A", "quantity": 1}],
                              "unfulfilled_items": [{"sku": "B", "quantity": 2}]}, "doc.partially_fulfilled") == "A ×1 (pending: B ×2)"
