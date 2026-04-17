# Copyright (c) 2026 Noah Severs. All Rights Reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for stock receipt creation and confirmation."""

from __future__ import annotations

import pytest
pytest.importorskip("celerp_warehousing")

import uuid

import pytest
import pytest_asyncio

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.auth import create_access_token


@pytest.fixture
def _sr_ids():
    return {"company_id": uuid.uuid4(), "user_id": uuid.uuid4()}


@pytest_asyncio.fixture
async def sr_auth(session, _sr_ids):
    cid = _sr_ids["company_id"]
    uid = _sr_ids["user_id"]
    session.add(Company(id=cid, name="ReceiptCo", slug="receiptco", settings={"currency": "USD"}))
    session.add(User(id=uid, company_id=cid, email="admin@receipt.test", name="Admin", auth_hash="x", role="admin", is_active=True))
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = create_access_token(subject=str(uid), company_id=str(cid), role="admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": str(uid)}


async def _create_and_finalize_po(client, auth):
    """Create and finalize a purchase order (converts to bill)."""
    ref = f"WH-PO-{uuid.uuid4().hex[:6]}"
    payload = {
        "doc_type": "purchase_order",
        "ref_id": ref,
        "line_items": [
            {"sku": "PO-ITEM-A", "name": "Item A", "quantity": 10, "unit_price": 5.0},
            {"sku": "PO-ITEM-B", "name": "Item B", "quantity": 5, "unit_price": 8.0},
        ],
        "total": 90.0,
    }
    r = await client.post("/docs", headers=auth["headers"], json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r2.status_code == 200, r2.text
    return doc_id


@pytest.mark.asyncio
async def test_stock_receipts_list_empty(client, session, sr_auth):
    """GET /warehousing/stock-receipts returns empty list initially."""
    r = await client.get("/warehousing/stock-receipts", headers=sr_auth["headers"])
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_create_stock_receipt_directly(client, session, sr_auth):
    """Directly create a stock receipt via the service."""
    from celerp_warehousing.stock_receipts import create_stock_receipt

    doc_state = {
        "ref_id": "PO-001",
        "line_items": [
            {"sku": "RCPT-A", "name": "Item A", "quantity": 10, "unit_price": 5.0},
        ],
    }
    result = await create_stock_receipt(
        session,
        source_doc_id="doc:fake-po-001",
        source_doc_state=doc_state,
        company_id=sr_auth["company_id"],
        user_id=sr_auth["user_id"],
    )
    await session.commit()

    sr_id = result["stock_receipt_id"]
    assert sr_id is not None

    from celerp.models.projections import Projection
    row = await session.get(Projection, {"company_id": sr_auth["company_id"], "entity_id": sr_id})
    assert row is not None
    assert row.state["list_type"] == "stock_receipt"
    assert row.state["source_doc_id"] == "doc:fake-po-001"
    assert len(row.state["line_items"]) == 1
    assert row.state["line_items"][0]["sku"] == "RCPT-A"
    assert row.state["line_items"][0]["expected_quantity"] == 10


@pytest.mark.asyncio
async def test_confirm_stock_receipt_full(client, session, sr_auth):
    """POST /warehousing/stock-receipts/{id}/confirm creates inventory items."""
    from celerp_warehousing.stock_receipts import create_stock_receipt

    doc_state = {
        "ref_id": "PO-FULL",
        "line_items": [
            {"sku": "FULL-A", "name": "Full Item A", "quantity": 5, "unit_price": 10.0},
        ],
    }
    result = await create_stock_receipt(
        session,
        source_doc_id="doc:fake-full-po",
        source_doc_state=doc_state,
        company_id=sr_auth["company_id"],
        user_id=sr_auth["user_id"],
    )
    await session.commit()
    sr_id = result["stock_receipt_id"]

    r = await client.post(
        f"/warehousing/stock-receipts/{sr_id}/confirm",
        headers=sr_auth["headers"],
        json={
            "received_lines": [
                {"sku": "FULL-A", "received_quantity": 5, "unit_price": 10.0, "name": "Full Item A"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["completed"] is True
    assert len(data["created_items"]) == 1
    assert data["created_items"][0]["sku"] == "FULL-A"
    assert data["created_items"][0]["quantity"] == 5


@pytest.mark.asyncio
async def test_confirm_stock_receipt_partial(client, session, sr_auth):
    """Partial confirmation leaves receipt open."""
    from celerp_warehousing.stock_receipts import create_stock_receipt

    doc_state = {
        "ref_id": "PO-PARTIAL",
        "line_items": [
            {"sku": "PARTIAL-A", "name": "Part A", "quantity": 10, "unit_price": 5.0},
        ],
    }
    result = await create_stock_receipt(
        session,
        source_doc_id="doc:fake-partial-po",
        source_doc_state=doc_state,
        company_id=sr_auth["company_id"],
        user_id=sr_auth["user_id"],
    )
    await session.commit()
    sr_id = result["stock_receipt_id"]

    r = await client.post(
        f"/warehousing/stock-receipts/{sr_id}/confirm",
        headers=sr_auth["headers"],
        json={
            "received_lines": [
                {"sku": "PARTIAL-A", "received_quantity": 6, "unit_price": 5.0},
            ]
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["completed"] is False
    assert len(data["created_items"]) == 1
    assert data["created_items"][0]["quantity"] == 6

    # Receipt should still be in draft
    r2 = await client.get(f"/warehousing/stock-receipts/{sr_id}", headers=sr_auth["headers"])
    assert r2.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_confirm_already_completed_receipt(client, session, sr_auth):
    """Cannot confirm an already-completed stock receipt."""
    from celerp_warehousing.stock_receipts import create_stock_receipt

    doc_state = {
        "ref_id": "PO-DUP",
        "line_items": [{"sku": "DUP-A", "name": "Dup A", "quantity": 3, "unit_price": 5.0}],
    }
    result = await create_stock_receipt(
        session, source_doc_id="doc:dup-po",
        source_doc_state=doc_state, company_id=sr_auth["company_id"], user_id=sr_auth["user_id"],
    )
    await session.commit()
    sr_id = result["stock_receipt_id"]

    r = await client.post(
        f"/warehousing/stock-receipts/{sr_id}/confirm",
        headers=sr_auth["headers"],
        json={"received_lines": [{"sku": "DUP-A", "received_quantity": 3, "unit_price": 5.0}]},
    )
    assert r.status_code == 200

    r2 = await client.post(
        f"/warehousing/stock-receipts/{sr_id}/confirm",
        headers=sr_auth["headers"],
        json={"received_lines": [{"sku": "DUP-A", "received_quantity": 3, "unit_price": 5.0}]},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_stock_receipt(client, session, sr_auth):
    """GET /warehousing/stock-receipts/{id} returns receipt detail."""
    from celerp_warehousing.stock_receipts import create_stock_receipt

    doc_state = {
        "ref_id": "PO-GET",
        "line_items": [{"sku": "GET-A", "name": "Get A", "quantity": 2, "unit_price": 5.0}],
    }
    result = await create_stock_receipt(
        session, source_doc_id="doc:get-po",
        source_doc_state=doc_state, company_id=sr_auth["company_id"], user_id=sr_auth["user_id"],
    )
    await session.commit()
    sr_id = result["stock_receipt_id"]

    r = await client.get(f"/warehousing/stock-receipts/{sr_id}", headers=sr_auth["headers"])
    assert r.status_code == 200
    data = r.json()
    assert data["list_type"] == "stock_receipt"
    assert data["source_doc_id"] == "doc:get-po"


@pytest.mark.asyncio
async def test_stock_receipt_not_found(client, session, sr_auth):
    """GET /warehousing/stock-receipts/{id} returns 404 for unknown id."""
    r = await client.get("/warehousing/stock-receipts/list:nonexistent", headers=sr_auth["headers"])
    assert r.status_code == 404
