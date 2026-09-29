# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Two users selecting different customers: the stale contact reprice is rejected.

User A selects customer A (Wholesale), user B then selects customer B (Retail). A's
reprice is pinned to the version A's own patch returned, so it is refused with 409 and
the document or List ends with customer B at customer B's prices.
"""
from __future__ import annotations

import uuid

import pytest


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client) -> tuple[dict, str, str, str]:
    r = await client.post("/auth/register", json={
        "company_name": "Interleave Co",
        "email": f"interleave-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    h = _h(r.json()["access_token"])
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": "RACE-1", "name": "Race", "quantity": 10,
        "sell_by": "piece", "retail_price": 100, "wholesale_price": 80,
    })
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    contacts = []
    for name, price_list in (("Customer A", "Wholesale"), ("Customer B", "Retail")):
        r = await client.post("/crm/contacts", headers=h, json={
            "name": name, "contact_type": "customer", "price_list": price_list,
        })
        assert r.status_code == 200, r.text
        contacts.append(r.json()["id"])
    return h, item_id, contacts[0], contacts[1]


def _line(item_id: str) -> dict:
    return {"item_id": item_id, "sku": "RACE-1", "description": "Race", "quantity": 1,
            "unit_price": 100, "line_total": 100}


async def _interleave(client, h: dict, resource: str, entity_id: str, contact_a: str, contact_b: str):
    async def select(contact_id: str) -> int:
        r = await client.patch(f"/{resource}/{entity_id}", headers=h, json={
            "fields_changed": {"contact_id": {"old": None, "new": contact_id}},
        })
        assert r.status_code == 200, r.text
        return r.json()["version"]

    version_a = await select(contact_a)
    assert (await client.get(f"/{resource}/{entity_id}", headers=h)).json()["version"] == version_a
    version_b = await select(contact_b)
    assert version_b != version_a

    stale = await client.post(f"/{resource}/{entity_id}/reprice", headers=h,
                              json={"price_list": "Wholesale", "expected_version": version_a})
    assert stale.status_code == 409, stale.text
    fresh = await client.post(f"/{resource}/{entity_id}/reprice", headers=h,
                              json={"price_list": "Retail", "expected_version": version_b})
    assert fresh.status_code == 200, fresh.text

    state = (await client.get(f"/{resource}/{entity_id}", headers=h)).json()
    assert state["contact_id"] == contact_b
    assert state["price_list"] == "Retail"
    assert state["line_items"][0]["unit_price"] == 100


@pytest.mark.asyncio
async def test_document_stale_contact_reprice_is_rejected(client):
    h, item_id, contact_a, contact_b = await _setup(client)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "price_list": "Retail", "currency": "USD", "line_items": [_line(item_id)],
    })
    assert r.status_code == 200, r.text
    await _interleave(client, h, "docs", r.json()["id"], contact_a, contact_b)


@pytest.mark.asyncio
async def test_list_stale_contact_reprice_is_rejected(client):
    h, item_id, contact_a, contact_b = await _setup(client)
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "price_list": "Retail", "currency": "USD", "line_items": [_line(item_id)],
    })
    assert r.status_code == 200, r.text
    await _interleave(client, h, "lists", r.json()["id"], contact_a, contact_b)


@pytest.mark.asyncio
async def test_document_patch_without_changes_returns_current_version(client):
    h, item_id, contact_a, _ = await _setup(client)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "price_list": "Retail", "currency": "USD", "line_items": [_line(item_id)],
    })
    doc_id = r.json()["id"]
    body = {"fields_changed": {"contact_id": {"old": None, "new": contact_a}}}
    first = await client.patch(f"/docs/{doc_id}", headers=h, json=body)
    again = await client.patch(f"/docs/{doc_id}", headers=h, json=body)
    assert again.status_code == 200, again.text
    assert again.json() == {"event_id": None, "version": first.json()["version"]}
