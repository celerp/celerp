# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The document list can be narrowed to an explicit set of ids.

A bulk confirm in the assistant creates a batch of drafts; the tally links to
exactly that batch so it can be reviewed or deleted together.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@idsfilter.test"
    r = await client.post("/auth/register", json={"company_name": "IdsFilter Co", "email": addr, "name": "A", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_ids_filter_returns_exactly_those_documents(client):
    h = _h(await _register(client))
    ids = []
    for n in range(3):
        r = await client.post("/docs", headers=h, json={"doc_type": "bill", "line_items": [
            {"sku": f"IDS-{n}", "name": f"Line {n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}]})
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["id"])

    picked = (await client.get("/docs", params={"ids": ",".join(ids[:2]), "status": "draft"}, headers=h)).json()
    assert sorted(x["id"] for x in picked["items"]) == sorted(ids[:2])
    assert picked["total"] == 2

    junk = (await client.get("/docs", params={"ids": "doc:nope", "status": "draft"}, headers=h)).json()
    assert junk["items"] == [] and junk["total"] == 0

    r = await client.get("/docs", params={"ids": ",".join(f"doc:{i}" for i in range(501))}, headers=h)
    assert r.status_code == 422
