# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

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
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 80.0, "method": "transfer", "bank_account": "1111"})
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

    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 50.0, "bank_account": "1111"})
    await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_void_payment_partial_to_paid_lifecycle(client):
    """Pay fully, void one payment -> partial, void remaining -> final."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 60.0, "bank_account": "1111"})
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 40.0, "bank_account": "1111"})

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
        "date": "2026-01-15", "amount": 50.0, "method": "transfer", "bank_account": "1111", "reference": "REF-001",
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

    r = await client.post(f"/docs/{cn}/cn-refund", headers=_h(token), json={"date": "2026-01-15", "amount": 100.0})
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

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={"payment_date": "2026-01-15", 
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

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={"payment_date": "2026-01-15", 
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

    r = await client.post("/docs/bulk-payment", headers=_h(token), json={"payment_date": "2026-01-15", 
        "doc_ids": [draft_id, inv], "amount": 100.0, "bank_account": "1111",
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

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", 
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
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 50.0, "bank_account": "1111"})

    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={})
    assert r.status_code == 409
    assert "payments" in r.json()["detail"].lower() or "void" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_void_allowed_after_all_payments_voided(client):
    """After voiding all payments, doc can be voided."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})

    # Void the payment
    await client.post(f"/docs/{inv}/void-payment", headers=_h(token), json={"payment_index": 0})

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "final"

    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Issue 1: bank_account required on all payment endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_requires_bank_account(client):
    """record_payment must reject missing bank_account."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0})
    assert r.status_code == 422
    assert "bank_account" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_bulk_payment_requires_bank_account(client):
    """bulk_payment must reject missing bank_account."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post("/docs/bulk-payment", headers=_h(token),
                          json={"doc_ids": [inv], "amount": 100.0, "payment_date": "2026-01-15"})
    assert r.status_code == 422
    assert "bank_account" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_cn_refund_requires_bank_account(client):
    """cn-refund must reject missing bank_account."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)
    cn = await _create_and_finalize_cn(client, token, inv, 50.0)
    r = await client.post(f"/docs/{cn}/cn-refund", headers=_h(token),
                          json={"date": "2026-01-15", "amount": 50.0})
    assert r.status_code == 422
    assert "bank_account" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_payment_projection_stores_bank_account_not_default(client):
    """Payment projection must store exactly the specified account, never fall back to 1110."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["payments"][0]["bank_account"] == "1111"
    assert doc["payments"][0]["bank_account"] != "1110"


# ---------------------------------------------------------------------------
# Issue 3: conversion_rate stored on payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_conversion_rate_stored(client):
    """conversion_rate is persisted in the payment projection."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0,
                                "bank_account": "1111", "conversion_rate": 35.5})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["payments"][0]["conversion_rate"] == 35.5


@pytest.mark.asyncio
async def test_payment_conversion_rate_optional(client):
    """Omitting conversion_rate succeeds and stores None."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["payments"][0].get("conversion_rate") is None


# ---------------------------------------------------------------------------
# Bug: MissingGreenlet on void-then-repay (row.state accessed after flush)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_void_then_repay_no_greenlet_error(client):
    """Voiding a payment and then recording a new one must not raise MissingGreenlet.

    Previously, record_payment accessed row.state *after* emit_event() which
    flushes the session and expires the ORM object, causing a synchronous
    lazy-load in an async context (sqlalchemy.exc.MissingGreenlet).
    """
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    # First payment
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    # Void it
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 0})
    assert r.status_code == 200

    # Re-pay - this is what was crashing with MissingGreenlet
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-16", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# P&L: revenue must appear after invoice finalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_shows_revenue_after_finalize(client):
    """Profit & Loss must include revenue from a finalized invoice.

    Tests the full stack: finalize -> auto JE created -> P&L query returns
    non-zero revenue. Guards against ts-missing JE projections being excluded
    by the date filter in _build_balances.
    """
    from datetime import date as _date
    token = await _register(client)
    await _create_and_finalize_invoice(client, token, 150.0)

    today = _date.today().isoformat()
    r = await client.get(
        f"/accounting/pnl?date_from=2000-01-01&date_to={today}",
        headers=_h(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["revenue"]["total"] == pytest.approx(150.0, abs=0.01), (
        f"Expected revenue=150 but got {data['revenue']['total']}. "
        "Finalization JE may be missing ts or account 4100 not in report."
    )
    assert data["net_profit"] > 0


# ---------------------------------------------------------------------------
# Problem 1: payment_index as JE key (void + repay must not collide)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_void_and_repay_creates_distinct_je_ids(client):
    """After voiding payment[0] and recording a new payment, the new payment
    must use je:auto:{doc}:pay:1 (index=1), NOT je:auto:{doc}:pay:0 (index=0).
    This means the idempotency key no longer collides with the original payment.
    """
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    # First payment -> should create JE with index=0
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    # Void payment[0]
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 0})
    assert r.status_code == 200

    # Second payment -> must succeed (previously would 409 due to idempotency collision on cumulative cents)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-20", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200, f"Second payment after void failed: {r.text}"

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert doc["amount_paid"] == pytest.approx(100.0, abs=0.01)
    # Two payment rows: index 0 voided, index 1 active
    assert len(doc["payments"]) == 2
    assert doc["payments"][0]["status"] == "voided"
    assert doc["payments"][1]["status"] == "active"
    assert doc["payments"][1]["index"] == 1


# ---------------------------------------------------------------------------
# Problem 2: refund_date stored on voided payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_void_payment_stores_refund_date(client):
    """Voiding with a refund_date must store it on the payment row."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 0, "void_reason": "Customer refund", "refund_date": "2026-02-01"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    p = doc["payments"][0]
    assert p["status"] == "voided"
    assert p["void_reason"] == "Customer refund"
    assert p["refund_date"] == "2026-02-01"


@pytest.mark.asyncio
async def test_void_payment_without_refund_date_works(client):
    """Voiding without a refund_date must still work (refund_date is optional)."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 0})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["payments"][0]["status"] == "voided"
    assert doc["payments"][0].get("refund_date") is None


# ---------------------------------------------------------------------------
# Problem 3: delete-payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_payment_tombstones_row_and_voids_je(client):
    """DELETE /docs/{id}/payments/{index} tombstones the payment in place
    and restores amount_outstanding to its pre-payment value."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert len(doc["payments"]) == 1

    # Delete the payment (no body needed - delete_reason is optional)
    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert len(doc["payments"]) == 1
    assert doc["payments"][0]["status"] == "deleted"
    assert doc["amount_paid"] == 0.0
    assert doc["amount_outstanding"] == pytest.approx(100.0, abs=0.01)
    assert doc["status"] == "final"


@pytest.mark.asyncio
async def test_delete_payment_keeps_indices_stable(client):
    """Payment indices are identity (journal-entry ids and idempotency keys
    embed them), so deleting payment[0] must not shift payment[1]."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 80.0, "bank_account": "1111"})
    assert r.status_code == 200
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-20", "amount": 60.0, "bank_account": "1111"})
    assert r.status_code == 200

    # Delete the first payment
    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert len(doc["payments"]) == 2
    assert doc["payments"][0]["status"] == "deleted"
    assert doc["payments"][1]["status"] == "active"
    assert doc["payments"][1]["index"] == 1
    assert doc["payments"][1]["amount"] == pytest.approx(60.0, abs=0.01)
    assert doc["amount_paid"] == pytest.approx(60.0, abs=0.01)


@pytest.mark.asyncio
async def test_payment_after_deletion_gets_its_own_journal_entry(client):
    """A payment recorded after a deletion must post its own journal entry:
    the freed slot stays occupied, so the new payment's index (and therefore
    its journal-entry idempotency key) never collides with the deleted one."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200
    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 200

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-25", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["payments"][1]["index"] == 1
    assert doc["status"] == "paid"

    # The new payment's JE exists and is posted: the bank ledger shows the
    # 2026-01-25 receipt, so cash is not understated.
    ledger = (await client.get("/accounting/ledger/1111", headers=_h(token))).json()
    new_lines = [l for l in ledger["lines"] if l["date"] == "2026-01-25"]
    assert new_lines and new_lines[0]["debit"] == pytest.approx(100.0, abs=0.01)


@pytest.mark.asyncio
async def test_payment_index_skips_legacy_minted_keys(client, session):
    """Docs compacted by pre-tombstone deletions can hold fewer payments than
    the highest index a journal entry was ever minted with; a new payment must
    allocate past those keys or its JE would silently dedupe into a dead one."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from sqlalchemy import select as _sel

    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 200.0)
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 80.0, "bank_account": "1111"})
    assert r.status_code == 200

    # Simulate the legacy compaction era: the doc holds one payment at index 0,
    # but a JE idempotency key for index 1 already exists from a payment that
    # an old-style deletion removed from the list.
    doc_row = (await session.execute(_sel(Projection).where(
        Projection.entity_id == inv, Projection.entity_type == "doc"))).scalar_one()
    session.add(LedgerEntry(
        company_id=doc_row.company_id, entity_id=f"je:auto:{inv}:pay:1",
        entity_type="journal_entry", event_type="acc.journal_entry.created",
        data={"memo": "legacy", "entries": []}, actor_id=None, location_id=None,
        source="test", idempotency_key=f"je:{inv}:invoice.paid:1:c", metadata_={},
    ))
    await session.flush()

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-02-01", "amount": 120.0, "bank_account": "1111"})
    assert r.status_code == 200, r.text

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    new_pay = [p for p in doc["payments"] if p["payment_date"] == "2026-02-01"]
    assert new_pay and new_pay[0]["index"] == 2  # skipped the dead index 1

    # And its journal entry is real: the bank ledger shows the receipt.
    ledger = (await client.get("/accounting/ledger/1111", headers=_h(token))).json()
    feb = [l for l in ledger["lines"] if l["date"] == "2026-02-01"]
    assert feb and feb[0]["debit"] == pytest.approx(120.0, abs=0.01)


@pytest.mark.asyncio
async def test_void_recompute_keeps_refunds(client):
    """Voiding a payment recomputes amount_paid from the payments list; a
    prior refund must stay subtracted."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 150.0)
    for d, amt in (("2026-01-10", 100.0), ("2026-01-12", 50.0)):
        r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                              json={"payment_date": d, "amount": amt, "bank_account": "1111"})
        assert r.status_code == 200
    r = await client.post(f"/docs/{inv}/refund", headers=_h(token),
                          json={"amount": 30.0, "payment_date": "2026-01-20"})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["amount_paid"] == pytest.approx(120.0, abs=0.01)

    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 1})
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    # 100 active - 30 refunded, not the bare 100 the list alone would say
    assert doc["amount_paid"] == pytest.approx(70.0, abs=0.01)
    assert doc["amount_outstanding"] == pytest.approx(80.0, abs=0.01)


@pytest.mark.asyncio
async def test_void_cn_application_posts_no_bank_movement(client):
    """Voiding a credit-note application voids the AR transfer entry; it must
    never post a bank reversal for cash that never moved."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "credit_note",
        "line_items": [{"name": "Credit", "quantity": 1, "unit_price": 40.0, "line_total": 40.0}],
        "subtotal": 40.0, "tax": 0.0, "total": 40.0,
    })
    assert r.status_code == 200
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=_h(token))).status_code == 200
    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=_h(token),
                          json={"target_doc_id": inv, "amount": 40.0})
    assert r.status_code == 200, r.text

    bank_before = (await client.get("/accounting/ledger/1111", headers=_h(token))).json()["lines"]

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    cn_pay = next(p for p in doc["payments"] if p.get("method") == "credit_note")
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": cn_pay["index"]})
    assert r.status_code == 200, r.text

    bank_after = (await client.get("/accounting/ledger/1111", headers=_h(token))).json()["lines"]
    assert len(bank_after) == len(bank_before)  # no phantom cash reversal
    # The AR transfer entry itself is voided
    journal = (await client.get("/accounting/journal", headers=_h(token))).json()
    cnapply = [e for e in journal["entries"] if e["je_id"].startswith(f"je:auto:{inv}:cnapply:")]
    assert cnapply and cnapply[0]["status"] == "void"
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["amount_outstanding"] == pytest.approx(100.0, abs=0.01)


@pytest.mark.asyncio
async def test_unvoid_blocked_in_locked_period(client):
    """Unvoiding an invoice restores its finalize entry on the invoice's own
    date, so a lock over that period must block the unvoid."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)
    r = await client.post(f"/docs/{inv}/void", headers=_h(token), json={"reason": "test"})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/period-lock", headers=_h(token),
                          json={"lock_date": "2099-12-31"})
    assert r.status_code == 200

    r = await client.post(f"/docs/{inv}/unvoid", headers=_h(token), json={})
    assert r.status_code == 422, r.text
    assert "locked" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_payment_invalid_index(client):
    """DELETE with an out-of-range index returns 422."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_payment_already_voided_returns_409(client):
    """Deleting a voided payment must return 409 (only active payments can be deleted)."""
    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    # Void it first
    r = await client.post(f"/docs/{inv}/void-payment", headers=_h(token),
                          json={"payment_index": 0})
    assert r.status_code == 200

    # Now try to delete the already-voided payment
    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_delete_payment_blocked_by_closed_reconciliation(client, session):
    """DELETE must return 409 when the payment JE is in a closed reconciliation session."""
    import uuid as _uuid
    from celerp_accounting.models import ReconciliationSession, BankAccount
    from sqlalchemy import select as _select

    token = await _register(client)
    inv = await _create_and_finalize_invoice(client, token, 100.0)

    r = await client.post(f"/docs/{inv}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 100.0, "bank_account": "1111"})
    assert r.status_code == 200

    je_id = f"je:auto:{inv}:pay:0"

    # Use the conftest session (same transaction the app uses in tests)
    ba = (await session.execute(_select(BankAccount).limit(1))).scalar_one_or_none()
    if ba is None:
        pytest.skip("No bank account seeded - cannot create reconciliation session")
    recon = ReconciliationSession(
        id=_uuid.uuid4(),
        company_id=ba.company_id,
        bank_account_id=ba.id,
        statement_date="2026-01-31",
        statement_balance=0.0,
        status="closed",
        reconciled_je_ids=[je_id],
    )
    session.add(recon)
    await session.flush()

    r = await client.delete(f"/docs/{inv}/payments/0", headers=_h(token))
    assert r.status_code == 409
    assert "closed period" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Bill expense on P&L - regression tests (Bug: direct bills had no JE on finalize)
# ---------------------------------------------------------------------------

async def _create_and_finalize_bill(client, token: str, total: float = 200.0) -> str:
    """Create a bill directly (not via PO) and finalize it."""
    data = {
        "doc_type": "bill",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total,
    }
    r = await client.post("/docs", headers=_h(token), json=data)
    assert r.status_code == 200
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r.status_code == 200
    return doc_id


@pytest.mark.asyncio
async def test_direct_bill_finalize_creates_expense_je(client):
    """A bill created directly (not from PO) must create an expense JE on finalize.

    Regression: finalize_doc had no branch for doc_type=='bill', so no JE was emitted.
    Result: P&L showed $0 expenses for all directly-created vendor bills.
    """
    token = await _register(client)
    await _create_and_finalize_bill(client, token, total=300.0)

    r = await client.get("/accounting/pnl", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    # Must have at least one expense line (6950 misc expense for service with no SKU)
    assert data["expenses"]["total"] > 0, "Expense total must be non-zero after finalizing a direct bill"
    expense_codes = {line["code"] for line in data["expenses"]["lines"]}
    assert "6950" in expense_codes, "Misc expense account 6950 must have balance from direct bill"


@pytest.mark.asyncio
async def test_direct_bill_payment_reduces_ap(client):
    """Paying a direct bill must debit AP (2110) and credit bank - correct trial balance sign."""
    token = await _register(client)
    doc_id = await _create_and_finalize_bill(client, token, total=150.0)

    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(token),
                          json={"payment_date": "2026-02-01", "amount": 150.0, "bank_account": "1111"})
    assert r.status_code == 200

    r = await client.get("/accounting/trial-balance", headers=_h(token))
    assert r.status_code == 200
    tb = {line["code"]: line for line in r.json()["lines"]}

    # AP 2110: finalize credits AP (+150 credit), payment debits AP (-150 debit) -> net 0
    # Bank 1111: payment credits bank (outflow) -> net credit balance
    assert "1111" in tb, "Bank account 1111 must appear on trial balance after bill payment"
    bank = tb["1111"]
    # Paying money out: bank is credited -> total_credit > 0
    assert bank["total_credit"] > 0, "Bank must have credit entries when paying a bill (outflow)"


@pytest.mark.asyncio
async def test_pnl_expense_does_not_appear_for_draft_bill(client):
    """Draft bills must not affect P&L - only finalized bills create JEs."""
    token = await _register(client)
    # Create but do NOT finalize
    data = {
        "doc_type": "bill",
        "line_items": [{"name": "Office supplies", "quantity": 1, "unit_price": 500.0, "line_total": 500.0}],
        "total": 500.0,
    }
    r = await client.post("/docs", headers=_h(token), json=data)
    assert r.status_code == 200

    r = await client.get("/accounting/pnl", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["expenses"]["total"] == 0.0, "Draft bill must not create any expense JE"


@pytest.mark.asyncio
async def test_bill_real_world_flow_blank_create_then_patch_lines(client):
    """Reproduce Nikolai's exact flow: blank bill -> patch lines -> finalize -> check P&L.

    The UI always creates a blank doc first (POST /docs with no line_items),
    then the JS line editor saves lines via PATCH with fields_changed.
    This test ensures the expense JE is emitted correctly in that flow.
    """
    token = await _register(client)
    h = _h(token)

    # Step 1: Create blank bill (exactly as create_blank_doc UI route does)
    r = await client.post("/docs", headers=h, json={"doc_type": "bill", "status": "draft"})
    assert r.status_code == 200
    bill_id = r.json()["id"]

    # Step 2: Patch line items (exactly as _celerpPersist JS does via /docs/{id}/lines UI route)
    lines = [{"description": "Service fee", "quantity": 1, "unit_price": 5885.0, "line_total": 5885.0}]
    r = await client.patch(f"/docs/{bill_id}", headers=h, json={
        "fields_changed": {
            "line_items": {"old": None, "new": lines},
            "subtotal": {"old": None, "new": 5885.0},
            "total": {"old": None, "new": 5885.0},
        }
    })
    assert r.status_code == 200

    # Verify state has line_items with line_total before finalize
    doc = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    assert doc["line_items"][0]["line_total"] == 5885.0

    # Step 3: Finalize
    r = await client.post(f"/docs/{bill_id}/finalize", headers=h)
    assert r.status_code == 200

    # Step 4: P&L must show the expense
    pnl = (await client.get("/accounting/pnl", headers=h)).json()
    assert pnl["expenses"]["total"] > 0, "Expense must appear on P&L after finalizing blank-created bill"

    # Step 5: Trial balance - 6950 must have a debit entry
    tb = (await client.get("/accounting/trial-balance", headers=h)).json()
    tb_map = {line["code"]: line for line in tb["lines"]}
    assert "6950" in tb_map, "Misc expense account 6950 must have a balance"
    assert tb_map["6950"]["total_debit"] > 0, "6950 must have debit entries from bill finalization"

    # Step 6: AP must be balanced (finalize credits AP, payment would debit it)
    assert "2110" in tb_map
    assert tb_map["2110"]["total_credit"] == pytest.approx(5885.0, abs=0.01)


@pytest.mark.asyncio
async def test_apply_cn_void_then_reapply_same_invoice(client):
    """Apply CN to invoice → void the application → re-apply to same invoice must succeed."""
    token = await _register(client)
    h = _h(token)

    inv = await _create_and_finalize_invoice(client, token, total=500.0)
    cn = await _create_and_finalize_cn(client, token, original_doc_id=inv, total=100.0)

    # First application
    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=h, json={
        "target_doc_id": inv, "amount": 100.0, "date": "2026-01-10",
    })
    assert r.status_code == 200

    # Verify CN is paid
    cn_state = (await client.get(f"/docs/{cn}", headers=h)).json()
    assert cn_state["status"] == "paid"

    # Void the application on the CN side (payment index 0)
    r = await client.post(f"/docs/{cn}/void-payment", headers=h, json={
        "payment_index": 0, "void_reason": "mistake", "refund_date": "2026-01-11",
    })
    assert r.status_code == 200

    # Both sides should be restored
    cn_state = (await client.get(f"/docs/{cn}", headers=h)).json()
    assert cn_state["status"] == "final"
    assert cn_state["amount_outstanding"] == pytest.approx(100.0, abs=0.01)

    inv_state = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert inv_state["status"] == "final"
    assert inv_state["amount_outstanding"] == pytest.approx(500.0, abs=0.01)

    # Re-apply to same invoice - must succeed
    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=h, json={
        "target_doc_id": inv, "amount": 100.0, "date": "2026-01-12",
    })
    assert r.status_code == 200, f"Re-apply failed: {r.text}"

    # Invoice outstanding must have decreased by the re-applied amount
    inv_state = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert inv_state["amount_outstanding"] == pytest.approx(400.0, abs=0.01)
    # CN outstanding must be 0 (fully applied)
    cn_state = (await client.get(f"/docs/{cn}", headers=h)).json()
    assert cn_state["amount_outstanding"] == pytest.approx(0.0, abs=0.01)
