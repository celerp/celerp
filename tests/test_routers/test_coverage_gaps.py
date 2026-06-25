# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Coverage gap closers targeting uncovered branches across:
  - routers/docs.py     (filters, pdf, credit_note, doc_taxes, send-void, receive, convert, csv-export)
  - routers/reports.py  (AR/AP aging buckets, invoice/PO group_by=price_range, expiring items)
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client, suffix: str = "") -> str:
    addr = f"cov-{suffix or uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post("/auth/register", json={"company_name": "GapCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _doc(client, tok, doc_type="invoice", total=100, **extra):
    body = {"doc_type": doc_type, "contact_id": "c:1",
            "line_items": [{"description": "Item", "quantity": 1, "unit_price": total, "line_total": total}],
            "subtotal": total, "tax": 0, "total": total}
    body.update(extra)
    r = await client.post("/docs", headers=_h(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ===========================================================================
# routers/docs.py gaps
# ===========================================================================


@pytest.mark.asyncio
async def test_docs_list_filters_exclude_status_date_q_offset_limit(client):
    """Covers list_docs: exclude_status, date_from, date_to, q, offset, limit."""
    tok = await _reg(client)
    inv_id = await _doc(client, tok, total=50)

    # exclude_status: exclude draft → shouldn't return our draft invoice
    r = await client.get("/docs?exclude_status=draft", headers=_h(tok))
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["items"]]
    assert inv_id not in ids

    # date_from / date_to: past dates → 0 results
    r2 = await client.get("/docs?date_from=2000-01-01&date_to=2001-01-01", headers=_h(tok))
    assert r2.status_code == 200

    # q filter: match on contact_id
    r3 = await client.get("/docs?q=c%3A1", headers=_h(tok))
    assert r3.status_code == 200
    assert len(r3.json()["items"]) >= 1

    # offset + limit
    r4 = await client.get("/docs?offset=0&limit=1", headers=_h(tok))
    assert r4.status_code == 200
    assert len(r4.json()["items"]) == 1

    # offset beyond results → empty
    r5 = await client.get("/docs?offset=9999", headers=_h(tok))
    assert r5.status_code == 200
    assert r5.json()["items"] == []


@pytest.mark.asyncio
async def test_docs_pdf_endpoint(client):
    """Covers GET /{entity_id}/pdf — generates a PDF response."""
    tok = await _reg(client)
    inv_id = await _doc(client, tok, total=100)

    r = await client.get(f"/docs/{inv_id}/pdf", headers=_h(tok))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")


@pytest.mark.asyncio
async def test_docs_credit_note_validation(client):
    """Covers credit_note: total exceeds original → 409; valid CN → 200."""
    tok = await _reg(client)

    # Create original invoice
    inv_id = await _doc(client, tok, total=50)

    # Credit note without original_doc_id is allowed (link can be added later)
    r1 = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "credit_note", "contact_id": "c:1", "line_items": [], "subtotal": 10, "tax": 0, "total": 10,
    })
    assert r1.status_code == 200

    # Credit note with total > original → 409
    r2 = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "credit_note", "original_doc_id": inv_id, "contact_id": "c:1",
        "line_items": [], "subtotal": 999, "tax": 0, "total": 999,
    })
    assert r2.status_code == 409

    # Valid credit note (≤ original)
    r3 = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "credit_note", "original_doc_id": inv_id, "contact_id": "c:1",
        "line_items": [], "subtotal": 50, "tax": 0, "total": 50,
    })
    assert r3.status_code == 200


@pytest.mark.asyncio
async def test_docs_create_with_doc_taxes(client):
    """Covers auto-compute total path + doc_taxes compound branch (lines 268-290)."""
    tok = await _reg(client)

    # doc_taxes path: total=0, line_items set, doc_taxes list provided
    # TaxApplication: code (str), rate (float in %), is_compound (bool)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice",
        "contact_id": "c:1",
        "line_items": [{"name": "Widget", "quantity": 2, "unit_price": 50}],
        "subtotal": 0,
        "tax": 0,
        "total": 0,
        "doc_taxes": [{"code": "VAT", "rate": 7, "is_compound": False}],
    })
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_h(tok))).json()
    # total should be line sum (100) + 7% = 107
    assert abs(float(doc.get("total", 0)) - 107.0) < 0.01


@pytest.mark.asyncio
async def test_docs_create_line_tax_amounts(client):
    """Covers per-line taxes path in create_doc (lines 276-279)."""
    tok = await _reg(client)

    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice",
        "contact_id": "c:1",
        "line_items": [{
            "name": "Widget",
            "quantity": 1,
            "unit_price": 100,
            "taxes": [{"code": "GST", "rate": 10, "is_compound": False}],
        }],
        "subtotal": 0,
        "tax": 0,
        "total": 0,
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_docs_send_void_guard(client):
    """Covers send_doc void guard (line 350)."""
    tok = await _reg(client)
    inv_id = await _doc(client, tok, total=100)

    # Void the invoice
    await client.post(f"/docs/{inv_id}/void", headers=_h(tok), json={"reason": "test void"})
    # Attempt to send a void doc → 409
    r = await client.post(f"/docs/{inv_id}/send", headers=_h(tok), json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_docs_receive_po_item_not_found_and_sku_required(client):
    """Covers receive_po: item_id not found (404), no sku+name guard (422)."""
    tok = await _reg(client)
    po_id = await _doc(client, tok, doc_type="purchase_order", total=50)

    # item_id points to nonexistent item → 404
    # ReceivedItem requires po_line_index (int) field
    r1 = await client.post(f"/docs/{po_id}/receive", headers=_h(tok), json={
        "location_id": str(uuid.uuid4()),
        "received_items": [{"po_line_index": 0, "item_id": "item:nonexistent", "quantity_received": 1}],
    })
    assert r1.status_code == 404

    # No item_id + no sku/name → 422
    r2 = await client.post(f"/docs/{po_id}/receive", headers=_h(tok), json={
        "location_id": str(uuid.uuid4()),
        "received_items": [{"po_line_index": 0, "quantity_received": 1}],
    })
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_docs_convert_expired_quotation(client):
    """Covers convert: expired quotation → 409 (line 506)."""
    tok = await _reg(client)

    # Create a quotation with a past valid_until
    r1 = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "quotation", "contact_id": "c:1", "line_items": [],
        "subtotal": 10, "tax": 0, "total": 10, "valid_until": "2000-01-01",
    })
    assert r1.status_code == 200
    q_id = r1.json()["id"]

    r_expired = await client.post(f"/docs/{q_id}/convert", headers=_h(tok))
    assert r_expired.status_code == 409
    assert "expired" in r_expired.json()["detail"].lower()


@pytest.mark.asyncio
async def test_docs_csv_export(client):
    """Covers GET /docs/export/csv (lines 673-717)."""
    tok = await _reg(client)
    await _doc(client, tok, doc_type="invoice", total=42)
    await _doc(client, tok, doc_type="purchase_order", total=20)

    # Full export
    r = await client.get("/docs/export/csv", headers=_h(tok))
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "entity_id" in body  # header row

    # Filtered by doc_type
    r2 = await client.get("/docs/export/csv?doc_type=invoice", headers=_h(tok))
    assert r2.status_code == 200

    # Filtered by status (no match → only header)
    r3 = await client.get("/docs/export/csv?status=void", headers=_h(tok))
    assert r3.status_code == 200

    # q filter
    r4 = await client.get("/docs/export/csv?q=c%3A1", headers=_h(tok))
    assert r4.status_code == 200


# ===========================================================================
# routers/reports.py gaps
# ===========================================================================


@pytest.mark.asyncio
async def test_reports_ar_aging_all_buckets(client):
    """Covers AR aging: zero-outstanding skip (line 79), invalid date ValueError (87-88), d90plus bucket (101)."""
    tok = await _reg(client)

    # Invoice with future due_date → current bucket (days_overdue <= 0)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c:1",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100, "due_date": "2099-12-31",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    # Invoice with invalid due_date → ValueError path, falls back to today
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c:2",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 50, "line_total": 50}],
        "subtotal": 50, "tax": 0, "total": 50, "due_date": "not-a-date",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    # Invoice with very old due_date → d90plus bucket
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_id": "c:3",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 200, "line_total": 200}],
        "subtotal": 200, "tax": 0, "total": 200, "due_date": "2000-01-01",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    r = await client.get("/reports/ar-aging", headers=_h(tok))
    assert r.status_code == 200
    lines = r.json()["lines"]
    # d90plus customer should appear
    d90p_total = sum(float(l.get("d90plus", 0)) for l in lines)
    assert d90p_total > 0


@pytest.mark.asyncio
async def test_reports_ar_aging_zero_outstanding_skipped(client):
    """Covers AR aging: outstanding <= 0 → skip (line 79)."""
    tok = await _reg(client)

    # Invoice with zero total → skipped in AR aging
    await _doc(client, tok, doc_type="invoice", total=0)

    r = await client.get("/reports/ar-aging", headers=_h(tok))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_reports_ap_aging_all_buckets(client):
    """Covers AP aging: zero-outstanding skip (line 166), invalid date (174-175), d90plus (188)."""
    tok = await _reg(client)

    # PO with very old due_date → d90plus bucket
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order", "contact_id": "s:1",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 300, "line_total": 300}],
        "subtotal": 300, "tax": 0, "total": 300, "due_date": "2000-01-01",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    # PO with invalid due_date → ValueError path
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order", "contact_id": "s:2",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100, "due_date": "bad-date",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    # PO with future date → current bucket
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "purchase_order", "contact_id": "s:3",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 150, "line_total": 150}],
        "subtotal": 150, "tax": 0, "total": 150, "due_date": "2099-12-31",
    })
    assert r.status_code == 200
    await client.post(f"/docs/{r.json()['id']}/finalize", headers=_h(tok))

    r = await client.get("/reports/ap-aging", headers=_h(tok))
    assert r.status_code == 200
    lines = r.json()["lines"]
    d90p_total = sum(float(l.get("d90plus", 0)) for l in lines)
    assert d90p_total > 0


@pytest.mark.asyncio
async def test_reports_invoice_analysis_price_range(client):
    """Covers group_by=price_range for invoice (sales) analysis."""
    tok = await _reg(client)

    # Create invoices with actual line items so price_range grouping has data
    for unit_price in [500, 2000, 10000, 25000]:
        body = {
            "doc_type": "invoice",
            "contact_id": "c:1",
            "line_items": [{"description": f"Item {unit_price}", "quantity": 1, "unit_price": unit_price, "total": unit_price}],
            "subtotal": unit_price,
            "tax": 0,
            "total": unit_price,
        }
        r = await client.post("/docs", headers=_h(tok), json=body)
        assert r.status_code == 200
        inv_id = r.json()["id"]
        await client.post(f"/docs/{inv_id}/send", headers=_h(tok), json={})
        await client.post(f"/docs/{inv_id}/finalize", headers=_h(tok))

    r = await client.get("/reports/sales?group_by=price_range", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data
    assert len(data["lines"]) > 0


@pytest.mark.asyncio
async def test_reports_po_analysis_price_range(client):
    """Covers group_by=price_range for PO (purchases) analysis."""
    tok = await _reg(client)

    for unit_price in [800, 3000, 8000, 30000]:
        body = {
            "doc_type": "purchase_order",
            "contact_id": "c:1",
            "line_items": [{"description": f"Item {unit_price}", "quantity": 1, "unit_price": unit_price, "total": unit_price}],
            "subtotal": unit_price,
            "tax": 0,
            "total": unit_price,
            "status": "final",
        }
        r = await client.post("/docs", headers=_h(tok), json=body)
        assert r.status_code == 200

    resp = await client.get("/reports/purchases?group_by=price_range", headers=_h(tok))
    assert resp.status_code == 200
    data = resp.json()
    assert "lines" in data
    assert len(data["lines"]) > 0


@pytest.mark.asyncio
async def test_reports_expiring_items_all_branches(client):
    """Covers expiring items: ValueError skip + days_remaining > days skip (lines 535-540)."""
    tok = await _reg(client)

    # Item with invalid expiry → skipped silently (ValueError branch)
    await client.post("/items", headers=_h(tok), json={
        "name": "Bad Expiry", "sku": "EXP-BAD", "quantity": 5,
        "expires_at": "not-a-real-date", "sell_by": "piece"})

    # Item with far-future expiry → skipped (days_remaining > days)
    await client.post("/items", headers=_h(tok), json={
        "name": "Far Future", "sku": "EXP-FAR", "quantity": 5,
        "expires_at": "2099-12-31", "sell_by": "piece"})

    # Item with imminent expiry → included (within 30 days)
    await client.post("/items", headers=_h(tok), json={
        "name": "Expiring Soon", "sku": "EXP-SOON", "quantity": 5,
        "expires_at": "2026-03-06", "sell_by": "piece"})

    r = await client.get("/reports/expiring?days=30", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data

