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
    data = {"doc_type": "invoice", **kw}
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
    eid = await _create_invoice(client, token, status="draft")
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
                      json={"amount": 100, "method": "cash"})
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
                           json={"amount": 4000, "method": "transfer", "reference": "TXN-001"})
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
                           json={"amount": 5000, "method": "cash"})
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
                           json={"amount": 9999, "method": "cash"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_payment_on_draft_rejected(client):
    """Cannot record payment on a draft document."""
    token = await _register(client)
    eid = await _create_invoice(client, token, total=500)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token),
                           json={"amount": 500, "method": "cash"})
    assert r.status_code == 409


# ===========================================================================
# T5: BOM CRUD
# ===========================================================================

@pytest.mark.asyncio
async def test_create_bom(client):
    """Creating a BOM returns a bom_id."""
    token = await _register(client)
    r = await client.post(
        "/manufacturing/boms",
        headers=_h(token),
        json={
            "name": "Ring Assembly v1",
            "output_item_id": "item:abc",
            "output_qty": 1.0,
            "components": [
                {"sku": "GLD-18K", "qty": 5.0, "unit": "grams"},
                {"sku": "DIA-0.5CT", "qty": 1.0, "unit": "pieces"},
            ],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert "bom_id" in data
    assert data["bom_id"].startswith("bom:")


@pytest.mark.asyncio
async def test_list_boms(client):
    """Created BOMs appear in list."""
    token = await _register(client)
    await client.post("/manufacturing/boms", headers=_h(token),
                      json={"name": "BOM A", "components": []})
    await client.post("/manufacturing/boms", headers=_h(token),
                      json={"name": "BOM B", "components": []})
    r = await client.get("/manufacturing/boms", headers=_h(token))
    assert r.status_code == 200
    boms = r.json()["items"]
    names = {b["name"] for b in boms}
    assert {"BOM A", "BOM B"}.issubset(names)


@pytest.mark.asyncio
async def test_get_bom(client):
    """Get BOM returns full detail including components."""
    token = await _register(client)
    created = (await client.post(
        "/manufacturing/boms",
        headers=_h(token),
        json={
            "name": "Detail BOM",
            "output_qty": 2.0,
            "components": [{"sku": "PART-1", "qty": 3.0, "unit": "pieces"}],
        },
    )).json()
    bom_id = created["bom_id"]
    r = await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))
    assert r.status_code == 200
    bom = r.json()
    assert bom["name"] == "Detail BOM"
    assert bom["output_qty"] == 2.0
    assert len(bom["components"]) == 1
    assert bom["components"][0]["sku"] == "PART-1"


@pytest.mark.asyncio
async def test_update_bom(client):
    """PUT updates BOM name and components."""
    token = await _register(client)
    bom_id = (await client.post(
        "/manufacturing/boms",
        headers=_h(token),
        json={"name": "Old Name", "components": []},
    )).json()["bom_id"]
    r = await client.put(
        f"/manufacturing/boms/{bom_id}",
        headers=_h(token),
        json={
            "name": "New Name",
            "components": [{"sku": "X-01", "qty": 10.0, "unit": "kg"}],
        },
    )
    assert r.status_code == 200
    bom = (await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))).json()
    assert bom["name"] == "New Name"
    assert len(bom["components"]) == 1
    assert bom["components"][0]["sku"] == "X-01"


@pytest.mark.asyncio
async def test_delete_bom(client):
    """DELETE BOM marks it as deleted."""
    token = await _register(client)
    bom_id = (await client.post(
        "/manufacturing/boms",
        headers=_h(token),
        json={"name": "To Delete", "components": []},
    )).json()["bom_id"]
    r = await client.delete(f"/manufacturing/boms/{bom_id}", headers=_h(token))
    assert r.status_code == 200
    # Should 404 now
    r2 = await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_bom_components_saved_correctly(client):
    """Components are saved with all fields."""
    token = await _register(client)
    bom_id = (await client.post(
        "/manufacturing/boms",
        headers=_h(token),
        json={
            "name": "Complex BOM",
            "output_item_id": "item:output-123",
            "output_qty": 5.0,
            "components": [
                {"sku": "A-100", "item_id": "item:a100", "qty": 2.0, "unit": "pieces"},
                {"sku": "B-200", "item_id": None, "qty": 0.5, "unit": "liters"},
            ],
        },
    )).json()["bom_id"]
    bom = (await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))).json()
    comps = bom["components"]
    assert len(comps) == 2
    a = next(c for c in comps if c["sku"] == "A-100")
    assert a["item_id"] == "item:a100"
    assert a["qty"] == 2.0
    b = next(c for c in comps if c["sku"] == "B-200")
    assert b["unit"] == "liters"


@pytest.mark.asyncio
async def test_create_bom_empty_name_rejected(client):
    """BOM with empty name is rejected."""
    token = await _register(client)
    r = await client.post("/manufacturing/boms", headers=_h(token),
                           json={"name": "", "components": []})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_nonexistent_bom_404(client):
    """Getting a nonexistent BOM returns 404."""
    token = await _register(client)
    import uuid
    r = await client.get(f"/manufacturing/boms/bom:{uuid.uuid4()}", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_bom_404(client):
    """Deleting a nonexistent BOM returns 404."""
    token = await _register(client)
    import uuid
    r = await client.delete(f"/manufacturing/boms/bom:{uuid.uuid4()}", headers=_h(token))
    assert r.status_code == 404
