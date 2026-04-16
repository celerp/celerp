# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for the payment system: void-payment, apply-to-invoice, CN refund, bulk-payment."""

from __future__ import annotations

import uuid

import pytest


async def _register(client, email: str | None = None) -> str:
    addr = email or f"pay-{uuid.uuid4().hex[:8]}@test.local"
    r = await client.post("/auth/register", json={"company_name": "PayCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_and_finalize_invoice(client, token: str, total: float = 100.0, contact_id: str | None = None) -> str:
    data = {"doc_type": "invoice", "line_items": [{"name": "X", "quantity": 1, "unit_price": total, "line_total": total}], "total": total}
    if contact_id:
        data["contact_id"] = contact_id
    r = await client.post("/docs", headers=_h(token), json=data)
    assert r.status_code == 200
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r.status_code == 200
    return doc_id


async def _create_and_finalize_cn(client, token: str, original_doc_id: str, total: float = 50.0, contact_id: str | None = None) -> str:
    data = {
        "doc_type": "credit_note", "original_doc_id": original_doc_id,
        "line_items": [{"name": "CN", "quantity": 1, "unit_price": total, "line_total": total}], "total": total,
    }
    if contact_id:
        data["contact_id"] = contact_id
    r = await client.post("/docs", headers=_h(token), json=data)
    assert r.status_code == 200
    cn_id = r.json()["id"]
    r = await client.post(f"/docs/{cn_id}/finalize", headers=_h(token))
    assert r.status_code == 200
    return cn_id


# ---------------------------------------------------------------------------
# Void payment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_void_payment_restores_outstanding(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)

    # Record payment
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 80.0, "method": "transfer", "bank_account": "1111"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["amount_paid"] == 80.0
    assert doc["status"] == "partial"
    assert len(doc["payments"]) == 1
    assert doc["payments"][0]["status"] == "active"

    # Void the payment
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0, "void_reason": "Mistake"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["amount_paid"] == 0.0
    assert doc["amount_outstanding"] == 200.0
    assert doc["status"] == "final"
    assert doc["payments"][0]["status"] == "voided"
    assert doc["payments"][0]["void_reason"] == "Mistake"


@pytest.mark.asyncio
async def test_void_payment_invalid_index(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token)

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})
    assert r.status_code == 422  # no payments exist


@pytest.mark.asyncio
async def test_void_payment_already_voided(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token)

    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 50.0})
    await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_void_payment_partial_to_paid_lifecycle(client):
    """Pay fully, void one payment -> partial, void remaining -> final."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 60.0})
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 40.0})

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert len(doc["payments"]) == 2

    # Void first payment
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "partial"
    assert doc["amount_paid"] == 40.0

    # Void second payment
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 1})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "final"
    assert doc["amount_paid"] == 0.0


# ---------------------------------------------------------------------------
# Credit note application tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cn_to_invoice(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0, contact_id="contact:acme")
    cn = await _create_and_finalize_cn(client, token, inv, 50.0, contact_id="contact:acme")

    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=_h(token), json={
        "target_doc_id": inv, "amount": 50.0, "date": "2026-03-28",
    })
    assert r.status_code == 200

    inv_doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert inv_doc["amount_paid"] == 50.0
    assert inv_doc["amount_outstanding"] == 150.0
    assert len(inv_doc["payments"]) == 1
    assert inv_doc["payments"][0]["method"] == "credit_note"
    assert inv_doc["payments"][0]["source_doc_id"] == cn

    cn_doc = (await client.get(f"/docs/{cn}", headers=_h(token))).json()
    assert cn_doc["amount_paid"] == 50.0
    assert len(cn_doc["payments"]) == 1
    assert cn_doc["payments"][0]["method"] == "applied"
    assert cn_doc["payments"][0]["target_doc_id"] == inv


@pytest.mark.asyncio
async def test_apply_cn_different_contact_rejected(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0, contact_id="contact:acme")
    cn = await _create_and_finalize_cn(client, token, inv, 50.0, contact_id="contact:other")

    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=_h(token), json={
        "target_doc_id": inv, "amount": 50.0,
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_void_cn_application_voids_both_sides(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0, contact_id="contact:acme")
    cn = await _create_and_finalize_cn(client, token, inv, 50.0, contact_id="contact:acme")

    await client.post(f"/docs/{cn}/apply-to-invoice", headers=_h(token), json={
        "target_doc_id": inv, "amount": 50.0,
    })

    # Void from the invoice side
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})
    assert r.status_code == 200

    inv_doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert inv_doc["amount_paid"] == 0.0
    assert inv_doc["payments"][0]["status"] == "voided"

    cn_doc = (await client.get(f"/docs/{cn}", headers=_h(token))).json()
    assert cn_doc["amount_paid"] == 0.0
    assert cn_doc["payments"][0]["status"] == "voided"


# ---------------------------------------------------------------------------
# CN refund tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cn_refund(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)
    cn = await _create_and_finalize_cn(client, token, inv, 50.0)

    r = await client.post(f"/docs/{cn}/cn-refund", headers=_h(token), json={
        "amount": 50.0, "method": "transfer", "bank_account": "1111", "reference": "REF-001",
    })
    assert r.status_code == 200

    cn_doc = (await client.get(f"/docs/{cn}", headers=_h(token))).json()
    assert cn_doc["amount_paid"] == 50.0
    assert cn_doc["status"] == "paid"
    assert cn_doc["payments"][0]["method"] == "refund"


@pytest.mark.asyncio
async def test_cn_refund_exceeds_balance(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)
    cn = await _create_and_finalize_cn(client, token, inv, 50.0)

    r = await client.post(f"/docs/{cn}/cn-refund", headers=_h(token), json={"amount": 100.0})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Bulk payment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_payment_allocates_oldest_first(client):
    token = await _register(client)
    inv1 = await _create_and_finalize_invoice(client, token, 100.0, contact_id="contact:bulk")
    inv2 = await _create_and_finalize_invoice(client, token, 200.0, contact_id="contact:bulk")

    # Patch due dates so inv1 is older
    await client.patch(f"/docs/{inv1}", headers=_h(token), json={"fields_changed": {"due_date": {"old": None, "new": "2026-03-01"}}})
    await client.patch(f"/docs/{inv2}", headers=_h(token), json={"fields_changed": {"due_date": {"old": None, "new": "2026-03-15"}}})

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={
        "doc_ids": [inv1, inv2],
        "amount": 150.0,
        "method": "transfer",
        "bank_account": "1111",
    })
    assert r.status_code == 200
    result = r.json()
    assert len(result["allocations"]) == 2
    assert result["allocations"][0]["doc_id"] == inv1
    assert result["allocations"][0]["amount"] == 100.0
    assert result["allocations"][1]["doc_id"] == inv2
    assert result["allocations"][1]["amount"] == 50.0

    doc1 = (await client.get(f"/docs/{inv1}", headers=_h(token))).json()
    assert doc1["status"] == "paid"
    doc2 = (await client.get(f"/docs/{inv2}", headers=_h(token))).json()
    assert doc2["status"] == "partial"
    assert doc2["amount_outstanding"] == 150.0


@pytest.mark.asyncio
async def test_bulk_payment_different_contacts_rejected(client):
    token = await _register(client)
    inv1 = await _create_and_finalize_invoice(client, token, 100.0, contact_id="contact:a")
    inv2 = await _create_and_finalize_invoice(client, token, 100.0, contact_id="contact:b")

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={
        "doc_ids": [inv1, inv2], "amount": 100.0,
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bulk_payment_skips_non_payable(client):
    token = await _register(client)
    # Draft invoice (not finalized, not payable)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "line_items": [{"name": "X", "quantity": 1, "unit_price": 50, "line_total": 50}], "total": 50,
        "contact_id": "contact:c",
    })
    draft_id = r.json()["id"]

    inv = await _create_and_finalize_invoice(client, token, 100.0, contact_id="contact:c")

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={
        "doc_ids": [draft_id, inv], "amount": 100.0,
    })
    assert r.status_code == 200
    result = r.json()
    assert len(result["allocations"]) == 1
    assert result["allocations"][0]["doc_id"] == inv


# ---------------------------------------------------------------------------
# Payment event data tests (backward compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_stores_new_fields(client):
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={
        "amount": 50.0, "method": "transfer", "bank_account": "1111",
        "payment_date": "2026-03-28", "reference": "TRF-001",
    })
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    p = doc["payments"][0]
    assert p["bank_account"] == "1111"
    assert p["payment_date"] == "2026-03-28"
    assert p["reference"] == "TRF-001"
    assert p["method"] == "transfer"
    assert p["status"] == "active"


# ---------------------------------------------------------------------------
# Void invoice guard (Component 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_void_blocked_with_active_payments(client):
    """Cannot void a doc that has active payments."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 50.0})

    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={})
    assert r.status_code == 409
    assert "payments" in r.json()["detail"].lower() or "void" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_void_allowed_after_all_payments_voided(client):
    """After voiding all payments, doc can be voided."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 100.0})

    # Void the payment
    await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "final"

    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={})
    assert r.status_code == 200
