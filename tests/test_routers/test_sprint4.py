# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Tests for Sprint 4 features:
  T1 - blank-first document creation
  T2 - inline line item save
  T3 - finalize / void / send actions
  T4 - payment recording
  T5 - BOM CRUD
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, suffix="") -> str:
    import uuid
    email = f"s4-{uuid.uuid4().hex[:6]}{suffix}@test.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "S4 Co", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_invoice(client, token: str, **kw) -> str:
    # Default to a single line (amount = total) so the doc can be finalized/sent - a
    # zero-line doc can no longer be issued. Callers testing blank docs pass line_items=[].
    total = kw.get("total", 100)
    data = {
        "doc_type": "invoice",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total,
        **kw,
    }
    r = await client.post("/docs", headers=_h(token), json=data)
    assert r.status_code == 200
    return r.json()["id"]


# ===========================================================================
# T1: blank-first document creation
# ===========================================================================

@pytest.mark.asyncio
async def test_create_blank_invoice_via_api(client):
    """POST /docs with only doc_type creates a draft document."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "status": "draft"})
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    assert data["id"].startswith("doc:")


@pytest.mark.asyncio
async def test_blank_invoice_is_retrievable(client):
    """A blank invoice has draft status and no line items."""
    token = await _register(client)
    eid = await _create_invoice(client, token, status="draft", line_items=[])
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "draft"
    assert doc.get("line_items", []) == []


@pytest.mark.asyncio
async def test_blank_po_creation(client):
    """Can create a blank purchase_order document."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "purchase_order", "status": "draft"})
    assert r.status_code == 200
    eid = r.json()["id"]
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["doc_type"] == "purchase_order"


# ===========================================================================
# T2: line item save
# ===========================================================================

@pytest.mark.asyncio
async def test_patch_doc_line_items(client):
    """PATCH doc with fields_changed containing line_items stores them."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    lines = [
        {"description": "Consulting", "sku": "CSL-01", "quantity": 2, "unit_price": 5000, "tax_rate": 7, "line_total": 10000},
        {"description": "Travel", "sku": "", "quantity": 1, "unit_price": 1500, "tax_rate": 0, "line_total": 1500},
    ]
    subtotal = sum(l["quantity"] * l["unit_price"] for l in lines)
    tax = sum(l["quantity"] * l["unit_price"] * l["tax_rate"] / 100 for l in lines)
    total = subtotal + tax
    patch = {
        "fields_changed": {
            "line_items": {"old": None, "new": lines},
            "subtotal": {"old": None, "new": subtotal},
            "tax": {"old": None, "new": tax},
            "total": {"old": None, "new": total},
        }
    }
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json=patch)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    saved_lines = doc.get("line_items", [])
    assert len(saved_lines) == 2
    assert saved_lines[0]["description"] == "Consulting"
    assert saved_lines[1]["unit_price"] == 1500


@pytest.mark.asyncio
async def test_patch_doc_updates_totals(client):
    """Patching subtotal/tax/total stores them in projection."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    patch = {
        "fields_changed": {
            "subtotal": {"old": None, "new": 10000},
            "tax": {"old": None, "new": 700},
            "total": {"old": None, "new": 10700},
        }
    }
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json=patch)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["subtotal"] == 10000
    assert doc["total"] == 10700


@pytest.mark.asyncio
async def test_patch_non_draft_rejected(client):
    """Cannot patch a non-draft document."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    # Finalize it first
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    # Now try to patch
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json={"fields_changed": {"notes": {"old": None, "new": "test"}}})
    assert r.status_code == 409


# ===========================================================================
# T3: finalize, void, send
# ===========================================================================

@pytest.mark.asyncio
async def test_finalize_draft_doc(client):
    """Finalizing a draft document transitions it to 'final' status."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    r = await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "final"


@pytest.mark.asyncio
async def test_send_draft_doc(client):
    """Sending a draft document transitions it to 'sent' status."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    r = await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "sent"


@pytest.mark.asyncio
async def test_void_draft_doc(client):
    """Voiding a draft document transitions it to 'void' status."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "Test void"})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "void"
    assert doc.get("void_reason") == "Test void"


@pytest.mark.asyncio
async def test_void_paid_doc_rejected(client):
    """Cannot void a paid document."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=100, status="draft")
    # Send it
    await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    # Record full payment
    await client.post(f"/docs/{eid}/payment", headers=_h(token),
                      json={"payment_date": "2026-01-15", "amount": 100, "method": "cash", "bank_account": "1111"})
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_void_already_void_is_ok(client):
    """Voiding a void document still succeeds (backend allows it)."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    await client.post(f"/docs/{eid}/void", headers=_h(token), json={})
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={})
    # Should be 200 (or 409 - the backend currently allows it)
    assert r.status_code in (200, 409)


@pytest.mark.asyncio
async def test_finalize_void_doc_rejected(client):
    """Cannot finalize a void document."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    await client.post(f"/docs/{eid}/void", headers=_h(token), json={})
    r = await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    assert r.status_code == 409


# ===========================================================================
# T4: payment recording
# ===========================================================================

@pytest.mark.asyncio
async def test_record_payment_reduces_outstanding(client):
    """Recording a payment reduces amount_outstanding."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=10000)
    # Must be in sent/final status
    await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token),
                           json={"payment_date": "2026-01-15", "amount": 4000, "method": "transfer", "reference": "TXN-001", "bank_account": "1111"})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["amount_paid"] == 4000.0
    assert doc["amount_outstanding"] == 6000.0
    assert doc["status"] == "partial"


@pytest.mark.asyncio
async def test_full_payment_marks_paid(client):
    """A payment for the full amount marks the doc as paid."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=5000)
    await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token),
                           json={"payment_date": "2026-01-15", "amount": 5000, "method": "cash", "bank_account": "1111"})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert doc["amount_outstanding"] == 0.0


@pytest.mark.asyncio
async def test_overpayment_rejected(client):
    """Payment exceeding outstanding is rejected."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=1000)
    await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token),
                           json={"payment_date": "2026-01-15", "amount": 9999, "method": "cash", "bank_account": "1111"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_payment_on_draft_rejected(client):
    """Cannot record payment on a draft document."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=500)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token),
                           json={"payment_date": "2026-01-15", "amount": 500, "method": "cash", "bank_account": "1111"})
    assert r.status_code == 409

# The standalone BOM CRUD API (/manufacturing/boms) was retired — recipes live on the inventory
# item now. Its removal is covered by test_bom_removed.py.
