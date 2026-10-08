# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared setup for the line-action tests: a company with books, real lots, and
documents and Lists built and driven through the API the way a user drives them."""
from __future__ import annotations

import uuid

import pytest_asyncio

from test_helpers import company_auth


@pytest_asyncio.fixture
async def h(session) -> dict:
    """Request headers for the admin of a fresh company with its books."""
    return (await company_auth(session, uuid.uuid4(), uuid.uuid4()))["headers"]


async def lot(client, h, sku: str, qty: float, *, cost: float = 0, sell_by: str = "piece") -> str:
    """Create an available lot of ``sku`` and return its id."""
    data = {"sku": sku, "name": sku, "quantity": qty, "sell_by": sell_by, "status": "available"}
    if cost:
        data["cost_total"] = cost * qty
    r = await client.post("/items", headers=h, json=data)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def item(client, h, item_id: str) -> dict:
    r = await client.get(f"/items/{item_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def line(item_id: str | None, qty: float, *, price: float = 10.0, sku: str = "", **extra) -> dict:
    out = {"item_id": item_id, "sku": sku, "description": sku or "Line",
           "quantity": qty, "unit_price": price, "sell_by": "piece"}
    out.update(extra)
    return out


async def doc(client, h, lines: list[dict], *, doc_type: str = "invoice", finalize: bool = True) -> str:
    total = sum(float(li.get("quantity") or 0) * float(li.get("unit_price") or 0) for li in lines)
    r = await client.post("/docs", headers=h, json={"doc_type": doc_type, "line_items": lines, "total": total})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    if finalize:
        r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
        assert r.status_code == 200, r.text
    return doc_id


async def state(client, h, entity_id: str) -> dict:
    path = "lists" if entity_id.startswith("list:") else "docs"
    r = await client.get(f"/{path}/{entity_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def line_ids(client, h, entity_id: str) -> list[str]:
    return [li.get("line_id") for li in (await state(client, h, entity_id))["line_items"]]


async def quotation(client, h, lines: list[dict] | None = None) -> str:
    r = await client.post("/lists", headers=h, json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]
    if lines is not None:
        await set_list_lines(client, h, list_id, lines)
    return list_id


async def set_list_lines(client, h, list_id: str, lines: list[dict]):
    v = (await state(client, h, list_id))["version"]
    return await client.patch(f"/lists/{list_id}", headers=h, json={
        "fields_changed": {"line_items": {"new": lines}}, "expected_version": v})
