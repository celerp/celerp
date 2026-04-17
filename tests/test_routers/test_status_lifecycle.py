# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for Phase 1 status lifecycle: revert-to-draft, unvoid, void guards."""

from __future__ import annotations

import uuid

import pytest


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@lifecycle.test"
    r = await client.post("/auth/register", json={"company_name": "LC Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_invoice(client, token: str, total: float = 100.0) -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "invoice", "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}], "total": total},
    )
    assert r.status_code == 200
    return r.json()["id"]


async def _create_po(client, token: str, total: float = 50.0) -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "purchase_order", "line_items": [{"name": "Part", "quantity": 1, "unit_price": total, "line_total": total}], "total": total},
    )
    assert r.status_code == 200
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Revert-to-draft tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revert_to_draft_from_final(client):
    """Finalized invoice can be reverted back to draft."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "final"

    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 200

    reverted = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert reverted["status"] == "draft"


@pytest.mark.asyncio
async def test_revert_to_draft_from_sent(client):
    """Sent invoice can be reverted back to draft."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/send", headers=_h(token), json={})
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "sent"

    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 200

    reverted = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert reverted["status"] == "draft"


@pytest.mark.asyncio
async def test_revert_blocked_from_wrong_status(client):
    """Revert-to-draft is blocked from statuses other than final/sent."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    # draft -> cannot revert
    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_revert_blocked_when_payments_exist(client):
    """Revert-to-draft is blocked when the document has existing payments (becomes partial)."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 10})

    # After partial payment the status is 'partial', which is not revertable
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "partial"
    assert float(doc["amount_paid"]) > 0

    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 409
    # Guard fires on status (partial) before payments check - either error is acceptable
    assert "revert" in r.json()["detail"].lower() or "payment" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_revert_blocked_when_items_received(client):
    """Revert-to-draft is blocked when a PO/bill has received items."""
    token = await _register(client)
    po = await _create_po(client, token)

    # Finalize (converts to bill with awaiting_payment status)
    await client.post(f"/docs/{po}/finalize", headers=_h(token))

    # Receive items (status -> received)
    await client.post(f"/docs/{po}/receive", headers=_h(token), json={
        "location_id": str(uuid.uuid4()),
        "received_items": [{"po_line_index": 0, "quantity_received": 1, "sku": "P1", "name": "Part"}],
    })

    doc = (await client.get(f"/docs/{po}", headers=_h(token))).json()
    # Status is 'received' after goods receipt
    assert doc["status"] == "received"

    r = await client.post(f"/docs/{po}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_po_to_bill_revert_restores_doc_type(client):
    """Reverting a bill (converted from PO) restores doc_type to purchase_order and PO ref_id."""
    token = await _register(client)
    po = await _create_po(client, token)

    po_doc = (await client.get(f"/docs/{po}", headers=_h(token))).json()
    original_ref = po_doc["ref_id"]
    assert po_doc["doc_type"] == "purchase_order"

    # Finalize converts PO -> bill
    await client.post(f"/docs/{po}/finalize", headers=_h(token))
    bill = (await client.get(f"/docs/{po}", headers=_h(token))).json()
    assert bill["doc_type"] == "bill"

    # Revert: bill -> purchase_order
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code == 200

    reverted = (await client.get(f"/docs/{po}", headers=_h(token))).json()
    assert reverted["status"] == "draft"
    assert reverted["doc_type"] == "purchase_order"
    assert reverted["ref_id"] == original_ref


# ---------------------------------------------------------------------------
# Void update tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_void_saves_pre_void_status(client):
    """Voiding a document saves the previous status as pre_void_status."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={"reason": "test"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "void"
    assert doc["pre_void_status"] == "final"


@pytest.mark.asyncio
async def test_void_blocked_from_partial_status(client):
    """Voiding a partially-paid document is blocked (must void payments first)."""
    token = await _register(client)
    inv = await _create_invoice(client, token, total=100.0)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 40})

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "partial"

    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={})
    assert r.status_code == 409
    assert "payment" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Unvoid tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unvoid_restores_previous_status(client):
    """Unvoid restores the document to its pre-void status."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    await client.post(f"/docs/{inv}/void", headers=_h(token), json={"reason": "mistake"})

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "void"
    assert doc["pre_void_status"] == "final"

    r = await client.post(f"/docs/{inv}/unvoid", headers=_h(token), json={})
    assert r.status_code == 200

    restored = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert restored["status"] == "final"
    assert "pre_void_status" not in restored
    assert "void_reason" not in restored


@pytest.mark.asyncio
async def test_unvoid_blocked_when_no_pre_void_status(client):
    """Unvoid fails if pre_void_status is not set (legacy void)."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    # Manually emit void event without pre_void_status by importing
    from celerp.models.projections import Projection
    from sqlalchemy import select

    # Create via import with status void but no pre_void_status
    await client.post("/docs/import", headers=_h(token), json={
        "entity_id": f"doc:LEGACY-{uuid.uuid4().hex[:6]}",
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "status": "void", "total": 100},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    })

    # Find the created doc
    r = await client.get("/docs?status=void", headers=_h(token))
    void_docs = r.json()["items"]
    assert void_docs

    legacy_id = void_docs[0]["id"]
    r2 = await client.post(f"/docs/{legacy_id}/unvoid", headers=_h(token), json={})
    assert r2.status_code == 409
    assert "pre_void_status" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_unvoid_blocked_from_non_void_status(client):
    """Unvoid is blocked on documents that aren't void."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    r = await client.post(f"/docs/{inv}/unvoid", headers=_h(token), json={})
    assert r.status_code == 409
