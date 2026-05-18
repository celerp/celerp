# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

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
    token, _ = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
    return user_id, token


async def _setup(session) -> tuple[uuid.UUID, uuid.UUID, str]:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="UpsertCo", slug=f"upsertco-{company_id.hex[:8]}"))
    session.add(User(
        id=user_id,
        email=f"admin-{user_id.hex[:8]}@test.co", name="Admin",
        auth_hash="x", is_active=True,
    ))
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()
    token, _ = create_access_token(subject=str(user_id), company_id=str(company_id), role="admin")
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


# ---------------------------------------------------------------------------
# Cross-company idempotency scoping (Bug 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_idempotency_key_allowed_for_different_companies(client, session):
    """Same idempotency_key from two different companies must both succeed (Bug 1 fix)."""
    company_a_id = uuid.uuid4()
    company_b_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User
    from celerp.services.auth import create_access_token

    for cid, uid, name in [
        (company_a_id, user_a_id, "CompanyA"),
        (company_b_id, user_b_id, "CompanyB"),
    ]:
        session.add(Company(id=cid, name=name, slug=f"co-{cid.hex[:8]}"))
        session.add(User(id=uid, email=f"admin-{uid.hex[:8]}@xco.test", name="Admin", auth_hash="x", is_active=True))
        session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()

    token_a, _ = create_access_token(subject=str(user_a_id), company_id=str(company_a_id), role="admin")
    token_b, _ = create_access_token(subject=str(user_b_id), company_id=str(company_b_id), role="admin")

    shared_idem = f"csv:item:shared-key-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": f"item:{uuid.uuid4().hex}",
        "event_type": "item.created",
        "data": {"sku": f"XSK-{shared_idem[-6:]}", "name": "Cross Co Item", "quantity": 1},
        "source": "csv_import",
        "idempotency_key": shared_idem,
    }

    # Company A import
    ra = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"records": [record]})
    assert ra.status_code == 200, ra.text
    assert ra.json()["created"] == 1

    # Company B import with same key and same entity_id - should ALSO create (different scope)
    record_b = {**record, "entity_id": f"item:{uuid.uuid4().hex}"}
    rb = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"records": [record_b]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["created"] == 1, f"Expected created=1, got {rb.json()}"
