# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for the upsert=True toggle on batch import endpoints."""

from __future__ import annotations

import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(company_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    user_id = uuid.uuid4()
    token = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return user_id, token


async def _setup(session) -> tuple[uuid.UUID, uuid.UUID, str]:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="UpsertCo", slug=f"upsertco-{company_id.hex[:8]}"))
    session.add(User(
        id=user_id, company_id=company_id,
        email=f"admin-{user_id.hex[:8]}@test.co", name="Admin",
        auth_hash="x", role="admin", is_active=True,
    ))
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return company_id, user_id, token


# ---------------------------------------------------------------------------
# Items upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    payload = {"records": [record]}

    r1 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["created"] == 1
    assert r1.json()["skipped"] == 0
    assert r1.json()["updated"] == 0

    # Second call without upsert — should skip
    r2 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json()["created"] == 0
    assert r2.json()["skipped"] == 1
    assert r2.json()["updated"] == 0


@pytest.mark.asyncio
async def test_items_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    # First import creates
    r1 = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    # Second import with upsert=True — should update
    r2 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0

    # Third call with upsert=True — should skip (upsert key already exists)
    r3 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["updated"] == 0
    assert body3["skipped"] == 1


# ---------------------------------------------------------------------------
# Docs upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docs_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_docs_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0


# ---------------------------------------------------------------------------
# Lists upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_lists_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0
