# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for https://github.com/celerp/celerp/issues/202.

An OPTIONAL field that an item can exist without must be clearable back to None. Today, once set, a
field can't be returned to empty: a blank value is rejected (barcode), crashes (cost), or is stored
as "" / null instead of being unset (prices, category, text). A "clear" (empty `new`) must unset the
field — removing it (None/absent), not storing an empty string.

These PATCH the data API with an empty value and assert the field reads back unset. They FAIL today
(barcode 422, cost ValueError, price/category keep a value) and pass once "clear → None → unset" works.
"""

from __future__ import annotations

import uuid

import pytest


async def _token(client) -> str:
    email = f"clear-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _seed(client, headers, **fields) -> str:
    payload = {"sku": "CLR-1", "name": "Item", "quantity": 1, "sell_by": "piece", "category": "book"}
    payload.update(fields)
    r = await client.post("/items", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get(client, headers, item_id: str) -> dict:
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _clear(client, headers, item_id: str, field: str):
    """Clear a field the way the UI does — PATCH with an empty `new`. Must succeed (200)."""
    r = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {field: {"old": None, "new": ""}}},
        headers=headers,
    )
    assert r.status_code == 200, f"clearing {field} should succeed, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_clear_retail_price(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    iid = await _seed(client, h, retail_price=10)
    assert (await _get(client, h, iid)).get("retail_price") == 10  # sanity: set
    await _clear(client, h, iid, "retail_price")
    assert (await _get(client, h, iid)).get("retail_price") is None


@pytest.mark.asyncio
async def test_clear_cost_total(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    iid = await _seed(client, h, cost_total=5)
    assert (await _get(client, h, iid)).get("cost_total") == 5  # sanity: set
    await _clear(client, h, iid, "cost_total")
    assert (await _get(client, h, iid)).get("cost_total") is None


@pytest.mark.asyncio
async def test_clear_barcode(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    iid = await _seed(client, h, barcode="12345")
    assert (await _get(client, h, iid)).get("barcode") == "12345"  # sanity: set
    await _clear(client, h, iid, "barcode")
    assert (await _get(client, h, iid)).get("barcode") is None


@pytest.mark.asyncio
async def test_clear_category(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    iid = await _seed(client, h, category="book")
    assert (await _get(client, h, iid)).get("category") == "book"  # sanity: set
    await _clear(client, h, iid, "category")
    assert (await _get(client, h, iid)).get("category") is None


@pytest.mark.asyncio
async def test_clear_select_field(client):
    """A select-typed (dropdown) field must also be clearable back to None."""
    h = {"Authorization": f"Bearer {await _token(client)}"}
    # Define a select attribute on the `book` category.
    r = await client.patch(
        "/companies/me/category-schema/book",
        json={"fields": [
            {"key": "format", "label": "Format", "type": "select",
             "options": ["Hardcover", "Paperback"], "required": False},
        ]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    iid = await _seed(client, h, category="book")
    # Set the select value, then confirm it's set.
    r = await client.patch(
        f"/items/{iid}",
        json={"fields_changed": {"format": {"old": None, "new": "Hardcover"}}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (await _get(client, h, iid)).get("format") == "Hardcover"  # sanity: set
    # Clear it.
    await _clear(client, h, iid, "format")
    assert (await _get(client, h, iid)).get("format") is None
