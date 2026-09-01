# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A closed memo must leave the unfulfilled_only doc-list view.

Closing a memo does not touch fulfillment_status (it stays "partial"), so the
unfulfilled_only filter has to exclude status=="closed" explicitly or a closed
memo keeps reading as outstanding fulfillment work in the list.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memofilter.test"
    r = await client.post("/auth/register", json={"company_name": "MemoFilter Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_unfulfilled_filter_excludes_closed(client):
    token = await _register(client)
    h = _h(token)
    a = (await client.post("/items", headers=h, json={"status": "available", "sku": "UF-A", "name": "A", "quantity": 1, "sell_by": "piece"})).json()["id"]
    memo = (await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": [
        {"entity_id": a, "sku": "UF-A", "name": "A", "quantity": 1, "unit_price": 10, "sell_by": "piece"}]})).json()["id"]
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    # Ship then return the line: fulfillment_status becomes "partial", nothing left memo_out.
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200

    # Before close: it appears in the unfulfilled list.
    before = (await client.get("/docs", params={"unfulfilled_only": "true"}, headers=h)).json()
    assert any(x.get("id") == memo for x in before["items"]), before

    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200

    # After close: it is gone from the unfulfilled list.
    after = (await client.get("/docs", params={"unfulfilled_only": "true"}, headers=h)).json()
    assert not any(x.get("id") == memo for x in after["items"]), after
