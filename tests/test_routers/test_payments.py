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
# P&L: JE with missing ts must not be silently excluded by date filter
# ---------------------------------------------------------------------------


def test_build_balances_includes_je_with_missing_ts():
    """_build_balances must include JEs that have no ts in their projection state.

    Guards against old JE projections (created before payment_date was required)
    being silently excluded from date-filtered P&L / trial balance reports.
    A missing ts is treated as 'include always', not 'exclude'.
    """
    from decimal import Decimal
    from types import SimpleNamespace
    from celerp_accounting.routes import _build_balances

    # Simulate a posted JE projection with no ts (old data)
    undated_je = SimpleNamespace(state={
        "status": "posted",
        "entries": [
            {"account": "1120", "debit": 100.0, "credit": 0.0},
            {"account": "4100", "debit": 0.0, "credit": 100.0},
        ],
        # ts intentionally absent
    })

    # With a date range that would exclude it if ts were treated as ""
    balances = _build_balances([undated_je], date_from="2026-01-01", date_to="2026-12-31")

    assert balances.get("4100") == Decimal("-100"), (
        f"JE with missing ts was silently excluded from P&L: {balances}"
    )
    assert balances.get("1120") == Decimal("100")
