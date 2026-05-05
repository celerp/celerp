# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Integration tests for monetary precision - verifies that the full computation
chain (create doc → store → pay) produces correctly rounded values at every step."""
import pytest
from httpx import AsyncClient


import uuid


def _h(token):
    return {"Authorization": f"Bearer {token}"}


async def _register(client):
    r = await client.post("/auth/register", json={
        "email": f"money-{uuid.uuid4().hex[:8]}@test.com",
        "name": "Money Test",
        "password": "pw",
        "company_name": "MoneyTest",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


async def _create_doc(client, token, line_items, currency="USD", tax=0.0):
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "currency": currency,
        "tax": tax,
        "line_items": line_items,
    })
    assert r.status_code == 200
    return r.json()["id"]


async def _get_doc(client, token, doc_id):
    r = await client.get(f"/docs/{doc_id}", headers=_h(token))
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_line_total_rounded_on_create(client):
    """38.6 × 38.86 must store as 1500.00, not 1499.996."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86},
    ])
    doc = await _get_doc(client, token, doc_id)
    li = doc["line_items"][0]
    assert li["line_total"] == pytest.approx(1500.00, abs=0.001)


@pytest.mark.asyncio
async def test_tax_rounded_on_create(client):
    """Tax on 2 lines at 7% must be 182.01, not 182.01092..."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
        {"description": "Ruby", "quantity": 64, "unit_price": 17.19,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
    ])
    doc = await _get_doc(client, token, doc_id)
    # Tax stored in line item tax amounts
    tax_sum = sum(
        sum(t["amount"] for t in li.get("taxes", []))
        for li in doc["line_items"]
    )
    assert tax_sum == pytest.approx(182.01, abs=0.001), f"Tax sum: {tax_sum}"


@pytest.mark.asyncio
async def test_total_rounded_on_create(client):
    """Full total chain: 2600.16 subtotal + 182.01 tax = 2782.17."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
        {"description": "Ruby", "quantity": 64, "unit_price": 17.19,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
    ])
    doc = await _get_doc(client, token, doc_id)
    assert doc["total"] == pytest.approx(2782.17, abs=0.001), f"Total: {doc['total']}"


@pytest.mark.asyncio
async def test_amount_outstanding_equals_total_exactly(client):
    """amount_outstanding must be an exact copy of total on a new unpaid doc."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
        {"description": "Ruby", "quantity": 64, "unit_price": 17.19,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
    ])
    doc = await _get_doc(client, token, doc_id)
    assert doc["total"] == doc["amount_outstanding"], (
        f"total {doc['total']} != amount_outstanding {doc['amount_outstanding']}"
    )


@pytest.mark.asyncio
async def test_payment_of_displayed_amount_accepted(client):
    """Paying the displayed rounded total must return 200, not 409."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
        {"description": "Ruby", "quantity": 64, "unit_price": 17.19,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
    ])
    # Finalize then send so payment endpoint accepts it
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r.status_code == 200
    r = await client.post(f"/docs/{doc_id}/send", headers=_h(token), json={})
    assert r.status_code == 200
    doc = await _get_doc(client, token, doc_id)
    displayed_total = round(doc["total"], 2)  # what the UI shows
    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(token), json={
        "amount": displayed_total,
        "payment_date": "2026-05-04",
        "bank_account": "1111",
    })
    assert r.status_code == 200, f"Payment of displayed total failed: {r.text}"


@pytest.mark.asyncio
async def test_partial_payment_outstanding_exact(client):
    """After partial payment, outstanding = total - payment, exact."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Item", "quantity": 1, "unit_price": 1000.00},
    ])
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r.status_code == 200
    r = await client.post(f"/docs/{doc_id}/send", headers=_h(token), json={})
    assert r.status_code == 200
    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(token), json={
        "amount": 400.00,
        "payment_date": "2026-05-04",
        "bank_account": "1111",
    })
    assert r.status_code == 200
    doc = await _get_doc(client, token, doc_id)
    assert doc["amount_outstanding"] == pytest.approx(600.00, abs=0.001)
    assert doc["amount_paid"] == pytest.approx(400.00, abs=0.001)


@pytest.mark.asyncio
async def test_tax_sum_equals_pct_of_subtotal(client):
    """Audit consistency: per-line tax sum == round(subtotal * rate, currency_dp)."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Tourmaline", "quantity": 38.6, "unit_price": 38.86,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
        {"description": "Ruby", "quantity": 64, "unit_price": 17.19,
         "taxes": [{"code": "VAT", "rate": 7, "order": 0, "is_compound": False, "label": ""}]},
    ])
    doc = await _get_doc(client, token, doc_id)
    subtotal = doc["subtotal"]
    per_line_tax = sum(
        sum(t["amount"] for t in li.get("taxes", []))
        for li in doc["line_items"]
    )
    subtotal_based_tax = round(subtotal * 0.07, 2)
    assert per_line_tax == pytest.approx(subtotal_based_tax, abs=0.01), (
        f"Per-line tax {per_line_tax} != subtotal-based {subtotal_based_tax}. "
        f"Audit inconsistency detected."
    )


@pytest.mark.asyncio
async def test_kwd_three_dp_precision(client):
    """KWD line total rounds to 3 decimal places."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Item", "quantity": 1, "unit_price": 10.1234},
    ], currency="KWD")
    doc = await _get_doc(client, token, doc_id)
    li = doc["line_items"][0]
    # 10.1234 rounded to 3dp = 10.123
    assert li["line_total"] == pytest.approx(10.123, abs=0.0001)


@pytest.mark.asyncio
async def test_jpy_zero_dp_precision(client):
    """JPY line total rounds to 0 decimal places (integer)."""
    token = await _register(client)
    doc_id = await _create_doc(client, token, [
        {"description": "Item", "quantity": 3, "unit_price": 333.4},
    ], currency="JPY")
    doc = await _get_doc(client, token, doc_id)
    li = doc["line_items"][0]
    # 3 * 333.4 = 1000.2, rounded to 0dp = 1000
    assert li["line_total"] == pytest.approx(1000.0, abs=0.1)
    assert li["line_total"] == int(li["line_total"]), "JPY line_total must be integer"
