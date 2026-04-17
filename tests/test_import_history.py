# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for import history (ImportBatch model + list/undo endpoints)."""

from __future__ import annotations

import uuid

import pytest

from celerp.events.types import EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client) -> str:
    """Register a fresh company and return an access token."""
    email = f"hist-{uuid.uuid4().hex[:10]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "History Co", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _import_items(client, token: str, count: int = 3) -> dict:
    """Import `count` items via batch import and return the result."""
    records = [
        {
            "entity_id": f"item:hist-{uuid.uuid4()}",
            "entity_type": "item",
            "event_type": EventType.ITEM_CREATED,
            "data": {
                "sku": f"HIST-{i:04d}",
                "name": f"History Item {i}",
                "category": "Test",
                "quantity": 1,
                "cost_price": 1.00,
                "wholesale_price": 1.50,
                "retail_price": 2.00,
                "status": "available",
            },
            "idempotency_key": f"test:hist:{uuid.uuid4()}",
            "source": "csv_import",
            "source_ts": None,
        }
        for i in range(count)
    ]
    r = await client.post(
        "/items/import/batch",
        headers=_h(token),
        json={"records": records, "filename": "history_test.csv"},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# List batches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_batches_empty(client):
    token = await _register(client)
    r = await client.get("/items/import/batches", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert "batches" in data
    assert isinstance(data["batches"], list)


@pytest.mark.asyncio
async def test_list_batches_after_import(client):
    token = await _register(client)
    result = await _import_items(client, token, count=5)
    batch_id = result.get("batch_id")
    assert batch_id is not None

    r = await client.get("/items/import/batches", headers=_h(token))
    assert r.status_code == 200
    batches = r.json()["batches"]
    assert len(batches) >= 1
    b = next((b for b in batches if b["id"] == batch_id), None)
    assert b is not None
    assert b["entity_type"] == "item"
    assert b["row_count"] == 5
    assert b["filename"] == "history_test.csv"
    assert b["status"] == "active"
    assert b["undone_at"] is None


@pytest.mark.asyncio
async def test_list_batches_requires_auth(client):
    r = await client.get("/items/import/batches")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_batches_isolated_per_company(client):
    # Register first company
    email = f"hist-a-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={"company_name": "Hist Co A", "email": email, "name": "Admin", "password": "pw"})
    token_a = r.json()["access_token"]
    # Create second company via the API (register is locked after bootstrap)
    r2 = await client.post("/companies", json={"name": "Hist Co B"}, headers=_h(token_a))
    assert r2.status_code == 200
    token_b = r2.json()["access_token"]

    await _import_items(client, token_a, count=2)

    r = await client.get("/items/import/batches", headers=_h(token_b))
    assert r.status_code == 200
    # Company B should see no batches from Company A
    batches = r.json()["batches"]
    assert len(batches) == 0


@pytest.mark.asyncio
async def test_multiple_imports_all_listed(client):
    token = await _register(client)
    r1 = await _import_items(client, token, count=2)
    r2 = await _import_items(client, token, count=3)

    batches = (await client.get("/items/import/batches", headers=_h(token))).json()["batches"]
    ids = {b["id"] for b in batches}
    assert r1["batch_id"] in ids
    assert r2["batch_id"] in ids


# ---------------------------------------------------------------------------
# Undo batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_batch_removes_items(client):
    token = await _register(client)
    result = await _import_items(client, token, count=3)
    batch_id = result["batch_id"]

    # Verify items exist
    items_before = (await client.get("/items", headers=_h(token))).json()["items"]
    imported = [i for i in items_before if "History Item" in i.get("name", "")]
    assert len(imported) == 3

    # Undo
    r = await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["removed"] == 3
    assert isinstance(data["modified_items"], list)

    # Verify items gone
    items_after = (await client.get("/items", headers=_h(token))).json()["items"]
    after_names = {i.get("name", "") for i in items_after}
    for i_name in ["History Item 0", "History Item 1", "History Item 2"]:
        assert i_name not in after_names


@pytest.mark.asyncio
async def test_undo_batch_marks_status_undone(client):
    token = await _register(client)
    result = await _import_items(client, token, count=2)
    batch_id = result["batch_id"]

    await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token))

    batches = (await client.get("/items/import/batches", headers=_h(token))).json()["batches"]
    b = next((b for b in batches if b["id"] == batch_id), None)
    assert b is not None
    assert b["status"] == "undone"
    assert b["undone_at"] is not None


@pytest.mark.asyncio
async def test_undo_batch_twice_returns_409(client):
    token = await _register(client)
    result = await _import_items(client, token, count=1)
    batch_id = result["batch_id"]

    await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token))
    r2 = await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token))
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_undo_batch_not_found(client):
    token = await _register(client)
    r = await client.post(
        f"/items/import/batches/{uuid.uuid4()}/undo",
        headers=_h(token),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_undo_batch_cross_company_forbidden(client):
    # Register first company
    email = f"hist-x-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={"company_name": "Hist X A", "email": email, "name": "Admin", "password": "pw"})
    token_a = r.json()["access_token"]
    # Create second company via the API
    r2 = await client.post("/companies", json={"name": "Hist X B"}, headers=_h(token_a))
    token_b = r2.json()["access_token"]

    result = await _import_items(client, token_a, count=2)
    batch_id = result["batch_id"]

    # Company B cannot undo Company A's batch
    r = await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token_b))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_undo_batch_requires_auth(client):
    r = await client.post(f"/items/import/batches/{uuid.uuid4()}/undo")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_undo_purges_idempotency_keys_allowing_reimport(client):
    """After undo, the same idempotency keys can be re-imported."""
    token = await _register(client)
    ikey = f"test:reimport:{uuid.uuid4()}"
    entity_id = f"item:reimport-{uuid.uuid4()}"
    records = [{
        "entity_id": entity_id,
        "entity_type": "item",
        "event_type": EventType.ITEM_CREATED,
        "data": {"sku": "REIMP-001", "name": "Reimportable", "category": "Test",
                 "quantity": 1, "cost_price": 1.0, "wholesale_price": 1.5, "retail_price": 2.0, "status": "available"},
        "idempotency_key": ikey,
        "source": "csv_import",
        "source_ts": None,
    }]

    r1 = await client.post("/items/import/batch", headers=_h(token),
                           json={"records": records, "filename": "test.csv"})
    batch_id = r1.json()["batch_id"]

    # Undo
    await client.post(f"/items/import/batches/{batch_id}/undo", headers=_h(token))

    # Re-import same key with new entity_id
    records[0]["entity_id"] = f"item:reimport-{uuid.uuid4()}"
    r2 = await client.post("/items/import/batch", headers=_h(token),
                           json={"records": records, "filename": "test.csv"})
    assert r2.status_code == 200
    assert r2.json()["created"] == 1
