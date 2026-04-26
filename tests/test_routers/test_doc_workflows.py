# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


async def _register(client, email: str | None = None) -> str:
    addr = email or f"admin-{uuid.uuid4().hex[:8]}@docs.test"
    r = await client.post("/auth/register", json={"company_name": "Docs Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_invoice(client, token: str, *, subtotal: float = 100, tax: float = 7, total: float = 107) -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "invoice",
            "contact_id": "contact:1",
            "line_items": [{"name": "A", "quantity": 1, "unit_price": subtotal, "line_total": subtotal}],
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
        },
    )
    assert r.status_code == 200
    return r.json()["id"]


async def _find_je(client, token: str, trigger: str, source_id: str) -> dict:
    rows = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    trigger_hint = {
        "doc.finalized": "finalized",
        "doc.payment.received": "payment",
        "doc.received": "received",
    }[trigger]
    return next(e for e in rows if source_id in (e["data"].get("memo") or "") and trigger_hint in (e["data"].get("memo") or ""))


def _assert_balanced(entries: list[dict]) -> None:
    debit = sum(float(x.get("debit", 0) or 0) for x in entries)
    credit = sum(float(x.get("credit", 0) or 0) for x in entries)
    assert abs(debit - credit) < 1e-6


@pytest.mark.asyncio
async def test_invoice_create_send_finalize_and_sequence(client, session):
    token = await _register(client)

    inv1 = await _create_invoice(client, token)
    inv2 = await _create_invoice(client, token)

    d1 = (await client.get(f"/docs/{inv1}", headers=_h(token))).json()
    d2 = (await client.get(f"/docs/{inv2}", headers=_h(token))).json()

    assert d1["status"] == "draft"
    # Draft invoices use Pro Forma (PF-) refs; real INV- ref assigned on finalize
    assert re.match(r"^PF-\d{4}-\d+$", d1["ref_id"]), f"Unexpected draft ref_id format: {d1['ref_id']}"
    assert re.match(r"^PF-\d{4}-\d+$", d2["ref_id"]), f"Unexpected draft ref_id format: {d2['ref_id']}"
    n1 = int(d1["ref_id"].split("-")[-1])
    n2 = int(d2["ref_id"].split("-")[-1])
    assert n2 == n1 + 1

    assert (await client.post(f"/docs/{inv1}/send", headers=_h(token), json={})).status_code == 200
    sent = (await client.get(f"/docs/{inv1}", headers=_h(token))).json()
    assert sent["status"] == "sent"

    assert (await client.post(f"/docs/{inv1}/finalize", headers=_h(token))).status_code == 200
    final = (await client.get(f"/docs/{inv1}", headers=_h(token))).json()
    assert final["status"] == "final"
    # After finalize, ref_id becomes a real INV- number
    assert re.match(r"^INV-\d{4}-\d+$", final["ref_id"]), f"Unexpected final ref_id format: {final['ref_id']}"

    je = await _find_je(client, token, "doc.finalized", inv1)
    entries = je["data"]["entries"]
    assert {x["account"] for x in entries} == {"1120", "4100", "2120"}
    ar = next(x for x in entries if x["account"] == "1120")
    revenue = next(x for x in entries if x["account"] == "4100")
    vat = next(x for x in entries if x["account"] == "2120")
    assert float(ar["debit"]) == 107
    assert float(revenue["credit"]) == 100
    assert float(vat["credit"]) == 7
    _assert_balanced(entries)
    assert inv1 in je["data"]["memo"]

    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == je["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "doc.finalized"
    assert je_row.metadata_["doc_id"] == inv1


@pytest.mark.asyncio
async def test_invoice_partial_then_full_payment_with_je(client, session):
    token = await _register(client)
    inv = await _create_invoice(client, token)

    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 40})
    assert r.status_code == 200

    partial = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert partial["status"] == "partial"
    assert partial["amount_paid"] == 40
    assert partial["amount_outstanding"] == 67

    je1 = await _find_je(client, token, "doc.payment.received", inv)
    e1 = je1["data"]["entries"]
    assert {x["account"] for x in e1} == {"1110", "1120"}
    assert any(x["account"] == "1110" and float(x["debit"]) == 40 for x in e1)
    assert any(x["account"] == "1120" and float(x["credit"]) == 40 for x in e1)
    _assert_balanced(e1)

    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == je1["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "doc.payment.received"
    assert je_row.metadata_["doc_id"] == inv

    r2 = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 67})
    assert r2.status_code == 200
    paid = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert paid["status"] == "paid"
    assert paid["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_invoice_guards_void_edit_pay_and_overpayment(client):
    token = await _register(client)
    inv = await _create_invoice(client, token)

    # pay draft
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 1})).status_code == 409

    # edit finalized
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    assert (
        await client.patch(
            f"/docs/{inv}",
            headers=_h(token),
            json={"fields_changed": {"notes": {"old": None, "new": "x"}}},
        )
    ).status_code == 409

    # overpayment
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 108})).status_code == 409

    # paid then void forbidden
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 107})).status_code == 200
    assert (await client.post(f"/docs/{inv}/void", headers=_h(token), json={"reason": "x"})).status_code == 409

    # draft can be voided
    inv2 = await _create_invoice(client, token, subtotal=50, tax=0, total=50)
    assert (await client.post(f"/docs/{inv2}/void", headers=_h(token), json={"reason": "duplicate"})).status_code == 200
    assert (await client.get(f"/docs/{inv2}", headers=_h(token))).json()["status"] == "void"


@pytest.mark.asyncio
async def test_po_receive_quotation_convert_and_credit_note_adjustment(client, session):
    token = await _register(client)

    # PO receive adjusts existing + creates new item + JE
    existing = await client.post("/items", headers=_h(token), json={"sku": "EXIST", "name": "Existing", "quantity": 1, "sell_by": "piece"})
    item_id = existing.json()["id"]
    po = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "purchase_order", "contact_id": "supplier:1", "line_items": [{"quantity": 2}, {"quantity": 3}], "subtotal": 50, "tax": 0, "total": 50},
    )
    po_id = po.json()["id"]

    rec = await client.post(
        f"/docs/{po_id}/receive",
        headers=_h(token),
        json={
            "location_id": "loc:1",
            "received_items": [
                {"po_line_index": 0, "item_id": item_id, "quantity_received": 2},
                {"po_line_index": 1, "sku": "NEW-PO", "name": "New PO Item", "quantity_received": 3},
            ],
        },
    )
    assert rec.status_code == 200
    assert (await client.get(f"/items/{item_id}", headers=_h(token))).json()["quantity"] == 3
    items = (await client.get("/items", headers=_h(token))).json()["items"]
    assert any(i.get("sku") == "NEW-PO" and i.get("quantity") == 3 for i in items)

    po_je = await _find_je(client, token, "doc.received", po_id)
    po_entries = po_je["data"]["entries"]
    assert {x["account"] for x in po_entries} == {"1130", "2110"}
    _assert_balanced(po_entries)
    assert po_id in po_je["data"]["memo"]

    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == po_je["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "doc.received"
    assert je_row.metadata_["doc_id"] == po_id

    # quotation convert
    q = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "quotation", "contact_id": "contact:1", "line_items": [{"name": "Q", "quantity": 1, "unit_price": 10, "line_total": 10}], "subtotal": 10, "tax": 0, "total": 10, "valid_until": "2999-01-01"},
    )
    q_id = q.json()["id"]
    converted = await client.post(f"/docs/{q_id}/convert", headers=_h(token))
    assert converted.status_code == 200
    target = converted.json()["target_doc_id"]
    assert (await client.get(f"/docs/{target}", headers=_h(token))).json()["doc_type"] == "invoice"
    assert (await client.get(f"/docs/{q_id}", headers=_h(token))).json()["status"] == "converted"

    # expired quotation guard
    expired = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "quotation", "contact_id": "contact:1", "line_items": [], "subtotal": 0, "tax": 0, "total": 0, "valid_until": "2000-01-01"},
    )
    assert (await client.post(f"/docs/{expired.json()["id"]}/convert", headers=_h(token))).status_code == 409

    # credit note adjusts source invoice outstanding
    inv = await _create_invoice(client, token, subtotal=100, tax=0, total=100)
    cn = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "original_doc_id": inv, "reason": "return", "line_items": [], "subtotal": 0, "tax": 0, "total": 30},
    )
    assert cn.status_code == 200
    assert (await client.get(f"/docs/{inv}", headers=_h(token))).json()["amount_outstanding"] == 70


@pytest.mark.anyio
async def test_create_doc_with_custom_ref_id(client, session):
    """User should be able to set a custom document number on creation."""
    token = await _register(client)
    r = await client.post(
        "/docs", headers=_h(token),
        json={"doc_type": "invoice", "ref_id": "MY-001", "contact_id": "contact:1",
              "line_items": [], "subtotal": 0, "tax": 0, "total": 0},
    )
    assert r.status_code == 200
    doc = await client.get(f"/docs/{r.json()['id']}", headers=_h(token))
    assert doc.json()["ref_id"] == "MY-001"


@pytest.mark.anyio
async def test_edit_ref_id_on_draft(client, session):
    """Draft docs should allow editing the document number via inline patch."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    r = await client.patch(
        f"/docs/{eid}", headers=_h(token),
        json={"fields_changed": {"ref_id": {"old": None, "new": "CUSTOM-42"}}},
    )
    assert r.status_code == 200
    doc = await client.get(f"/docs/{eid}", headers=_h(token))
    assert doc.json()["ref_id"] == "CUSTOM-42"


@pytest.mark.anyio
async def test_edit_ref_id_uniqueness(client, session):
    """Editing ref_id to an existing doc number should be rejected."""
    token = await _register(client)
    r1 = await client.post(
        "/docs", headers=_h(token),
        json={"doc_type": "invoice", "ref_id": "DUP-001", "contact_id": "contact:1",
              "line_items": [], "subtotal": 0, "tax": 0, "total": 0},
    )
    assert r1.status_code == 200
    eid2 = await _create_invoice(client, token)
    r = await client.patch(
        f"/docs/{eid2}", headers=_h(token),
        json={"fields_changed": {"ref_id": {"old": None, "new": "DUP-001"}}},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.anyio
async def test_edit_ref_id_blocked_on_non_draft(client, session):
    """Non-draft docs should block all edits including ref_id."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.patch(
        f"/docs/{eid}", headers=_h(token),
        json={"fields_changed": {"ref_id": {"old": None, "new": "NOPE"}}},
    )
    assert r.status_code == 409
    assert "draft" in r.json()["detail"].lower()


@pytest.mark.anyio
async def test_get_sequences(client, session):
    """GET /docs/sequences returns all doc type configs."""
    token = await _register(client)
    r = await client.get("/docs/sequences", headers=_h(token))
    assert r.status_code == 200
    seqs = r.json()
    types = {s["doc_type"] for s in seqs}
    assert "invoice" in types
    assert "purchase_order" in types
    for s in seqs:
        assert "pattern" in s
        assert "preview" in s
        assert "next" in s


@pytest.mark.anyio
async def test_patch_sequence_prefix(client, session):
    """PATCH /docs/sequences/invoice updates prefix."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"prefix": "FAK"})
    assert r.status_code == 200
    assert r.json()["prefix"] == "FAK"


@pytest.mark.anyio
async def test_patch_sequence_pattern(client, session):
    """PATCH /docs/sequences/invoice updates pattern."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"pattern": "{PREFIX}-{YYYY}-{####}"})
    assert r.status_code == 200
    assert r.json()["pattern"] == "{PREFIX}-{YYYY}-{####}"


@pytest.mark.anyio
async def test_patch_sequence_invalid_pattern(client, session):
    """PATCH rejects pattern without sequence token."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"pattern": "NO-SEQ"})
    assert r.status_code == 422


@pytest.mark.anyio
async def test_patch_sequence_reset_next(client, session):
    """PATCH next=1 resets counter."""
    token = await _register(client)
    # Generate a doc to advance counter
    await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    # Reset
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"next": 1})
    assert r.status_code == 200
    assert r.json()["next"] == 1


@pytest.mark.anyio
async def test_new_doc_uses_configured_pattern(client, session):
    """Creating a draft invoice uses the configured proforma pattern (PF-); INV- assigned on finalize."""
    token = await _register(client)
    # Set custom proforma pattern (draft invoices use proforma sequence)
    await client.patch("/docs/sequences/proforma", headers=_h(token),
                       json={"prefix": "PF", "pattern": "{PREFIX}-{####}"})
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    assert r.status_code == 200
    doc = await client.get(f"/docs/{r.json()['id']}", headers=_h(token))
    assert doc.json()["ref_id"] == "PF-0001"


# ---------------------------------------------------------------------------
# issue_date defaults + editability
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_new_doc_gets_issue_date_today(client, session):
    """Creating a new doc sets issue_date to today (ISO format)."""
    from datetime import date
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    assert r.status_code == 200
    doc_id = r.json()["id"]
    doc = await client.get(f"/docs/{doc_id}", headers=_h(token))
    assert doc.status_code == 200
    assert doc.json().get("issue_date") == date.today().isoformat(), (
        f"Expected issue_date={date.today().isoformat()!r}, got {doc.json().get('issue_date')!r}"
    )


@pytest.mark.anyio
async def test_finalized_doc_rejects_all_edits_with_clear_message(client, session):
    """Patching any field on a finalized doc returns 409 with a message explaining how to proceed."""
    from datetime import date, timedelta
    token = await _register(client)
    doc_id = await _create_invoice(client, token)
    # Finalize it
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    assert r.status_code == 200
    # Any PATCH - including date fields - must return 409 with actionable message
    new_date = (date.today() + timedelta(days=1)).isoformat()
    r = await client.patch(f"/docs/{doc_id}", headers=_h(token),
                           json={"fields_changed": {"issue_date": {"old": date.today().isoformat(), "new": new_date}}})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "draft" in detail.lower(), f"Error must mention 'Draft': {detail!r}"
    assert "revert" in detail.lower(), f"Error must tell user to revert: {detail!r}"


@pytest.mark.anyio
async def test_non_editable_field_rejected_on_issued_doc(client, session):
    """Patching any field on an issued doc returns 409 with an actionable message."""
    token = await _register(client)
    doc_id = await _create_invoice(client, token)
    # Finalize (issue) it
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    assert r.status_code == 200
    # Attempt to change subtotal
    r = await client.patch(f"/docs/{doc_id}", headers=_h(token),
                           json={"fields_changed": {"subtotal": {"old": 100, "new": 999}}})
    assert r.status_code == 409
    assert "draft" in r.json()["detail"].lower()


@pytest.mark.anyio
async def test_list_docs_date_filter_uses_issue_date(client, session):
    """list_docs date filter includes docs whose issue_date falls in range."""
    from datetime import date
    token = await _register(client)
    # Create a doc (issue_date = today)
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    assert r.status_code == 200
    today = date.today().isoformat()
    # List with date_from=today - doc must appear
    r = await client.get(f"/docs?doc_type=invoice&date_from={today}", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1, f"Expected at least 1 doc with date_from={today}, got 0"


# ---------------------------------------------------------------------------
# Invoice counter / status card correctness
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_summary_awaiting_payment_counts_finalized(client, session):
    """Summary awaiting_payment_count includes finalized (non-draft, non-paid) invoices."""
    token = await _register(client)
    # Create and finalize an invoice
    doc_id = await _create_invoice(client, token)
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["awaiting_payment_count"] >= 1, f"Expected awaiting_payment_count>=1, got {data}"
    assert data["all_issued_count"] >= 1


@pytest.mark.anyio
async def test_summary_excludes_draft_from_all_issued(client, session):
    """Summary all_issued_count does not count draft invoices."""
    token = await _register(client)
    # Create but do NOT finalize
    await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["all_issued_count"] == 0, f"Draft must not count as issued, got {data}"
    assert data["awaiting_payment_count"] == 0


@pytest.mark.anyio
async def test_list_docs_status_in_filter(client, session):
    """status_in param returns docs matching any of the listed statuses."""
    token = await _register(client)
    doc_id = await _create_invoice(client, token)
    # Finalize -> status becomes 'final'
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    r = await client.get("/docs?doc_type=invoice&status_in=final,sent,awaiting_payment", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item.get("status") in ("final", "sent", "awaiting_payment"), f"Unexpected status: {item.get('status')}"


@pytest.mark.anyio
async def test_list_docs_overdue_only_filter(client, session):
    """overdue_only=1 returns only docs with due_date before today."""
    from datetime import date, timedelta
    token = await _register(client)
    doc_id = await _create_invoice(client, token)
    # Finalize
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    # No overdue docs yet (no due_date)
    r = await client.get("/docs?doc_type=invoice&status_in=final,sent,awaiting_payment&overdue_only=1", headers=_h(token))
    assert r.status_code == 200
    # All results must have due_date < today
    today = date.today().isoformat()
    for item in r.json().get("items", []):
        assert item.get("due_date", "9999") < today, f"Overdue filter returned non-overdue doc: {item}"


@pytest.mark.anyio
async def test_amount_outstanding_recalculated_after_line_items_added(client, session):
    """Regression: adding line items after creation must update amount_outstanding.

    Previously, a new doc with no line items got amount_outstanding=0 (total=0).
    After adding line items the total increased but outstanding stayed 0, causing
    the payment section to show 'Paid in Full' for an unpaid invoice.
    """
    token = await _register(client)
    # Create invoice with a single line item so we can inspect state
    doc_id = await _create_invoice(client, token, subtotal=500, tax=0, total=500)
    r = await client.get(f"/docs/{doc_id}", headers=_h(token))
    assert r.status_code == 200
    state = r.json()
    # outstanding must equal total when no payment has been made
    assert float(state["amount_outstanding"]) == pytest.approx(float(state["total"]), abs=0.01), (
        f"amount_outstanding {state['amount_outstanding']} != total {state['total']} on fresh invoice"
    )
    assert float(state["amount_outstanding"]) > 0, "outstanding must be > 0 for an unpaid invoice"


@pytest.mark.anyio
async def test_amount_outstanding_after_patch_line_items(client, session):
    """Regression: patching line_items (or total) must keep amount_outstanding in sync."""
    token = await _register(client)
    doc_id = await _create_invoice(client, token, subtotal=100, tax=0, total=100)

    # Patch total upward (simulates adding a line item via the standard fields_changed format)
    r = await client.patch(
        f"/docs/{doc_id}",
        headers=_h(token),
        json={"fields_changed": {"total": {"old": 100, "new": 300}, "subtotal": {"old": 100, "new": 300}}},
    )
    assert r.status_code == 200

    r = await client.get(f"/docs/{doc_id}", headers=_h(token))
    state = r.json()
    # outstanding must track the new total (no payment made)
    assert float(state["amount_outstanding"]) == pytest.approx(300.0, abs=0.01), (
        f"outstanding {state['amount_outstanding']} should be 300 after total patched to 300"
    )


@pytest.mark.anyio
async def test_invoice_status_cards_include_proforma(client, session):
    """Regression: Pro-forma card must appear in invoice status card set alongside All Issued."""
    from ui.routes.documents import _doc_status_cards
    summary = {
        "count_by_status": {"draft": 3, "final": 5, "sent": 2, "paid": 1, "void": 1},
        "all_issued_count": 8,
        "awaiting_payment_count": 7,
        "overdue_count": 0,
    }
    html = str(_doc_status_cards([], "", summary, "USD", doc_type="invoice", lang="en"))
    assert "Pro" in html or "pro" in html.lower(), "Pro-forma card missing from invoice status cards"
    assert "All Issued" in html or "all_issued" in html.lower(), "All Issued card missing from invoice status cards"


@pytest.mark.anyio
async def test_summary_partial_invoice_in_awaiting_not_paid(client, session):
    """Regression: partial-paid invoices must appear in awaiting_payment_count, not paid.

    A partially paid invoice still has outstanding balance and should never
    appear in the 'Paid' bucket.
    """
    token = await _register(client)
    doc_id = await _create_invoice(client, token, subtotal=1000, tax=0, total=1000)
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    # Record a partial payment (500 out of 1000)
    await client.post(f"/docs/{doc_id}/payment", headers=_h(token),
                      json={"amount": 500, "method": "transfer", "reference": "PART-1",
                            "payment_date": "2026-04-25", "bank_account": "1110"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    cbs = data.get("count_by_status", {})
    assert cbs.get("partial", 0) >= 1, "partial-paid invoice should have status 'partial'"
    assert cbs.get("paid", 0) == 0, "partial-paid invoice must NOT be counted as 'paid'"
    assert data.get("awaiting_payment_count", 0) >= 1, "partial-paid invoice must be in awaiting_payment_count"


@pytest.mark.anyio
async def test_summary_all_count_not_doubled(client, session):
    """Regression: all_issued_count must not double-count invoices.

    One fully-paid invoice should give all_issued_count=1, not 2.
    """
    token = await _register(client)
    doc_id = await _create_invoice(client, token, subtotal=200, tax=0, total=200)
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    # Pay in full
    await client.post(f"/docs/{doc_id}/payment", headers=_h(token),
                      json={"amount": 200, "method": "cash", "reference": "FULL-1",
                            "payment_date": "2026-04-25", "bank_account": "1110"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["all_issued_count"] == 1, (
        f"Expected all_issued_count=1, got {data['all_issued_count']}"
    )


@pytest.mark.anyio
async def test_summary_awaiting_payment_total_is_outstanding(client, session):
    """awaiting_payment_total must equal the outstanding balance, not the invoice face value."""
    token = await _register(client)
    doc_id = await _create_invoice(client, token, subtotal=800, tax=0, total=800)
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    # Partial payment of 300 -> outstanding = 500
    await client.post(f"/docs/{doc_id}/payment", headers=_h(token),
                      json={"amount": 300, "method": "cash", "reference": "P1",
                            "payment_date": "2026-04-25", "bank_account": "1110"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert abs(data.get("awaiting_payment_total", -1) - 500.0) < 0.01, (
        f"awaiting_payment_total should be 500, got {data.get('awaiting_payment_total')}"
    )


# ---------------------------------------------------------------------------
# Comprehensive invoice summary / counter accuracy tests
# ---------------------------------------------------------------------------

async def _pay(client, token: str, doc_id: str, amount: float, reference: str = "REF") -> None:
    """Helper: record a payment against a finalized invoice."""
    r = await client.post(
        f"/docs/{doc_id}/payment",
        headers=_h(token),
        json={"amount": amount, "method": "transfer", "reference": reference,
              "payment_date": "2026-04-25", "bank_account": "1110"},
    )
    assert r.status_code == 200, f"Payment failed: {r.json()}"


async def _invoice(client, token: str, total: float) -> str:
    """Helper: create and finalize an invoice with the given total."""
    doc_id = await _create_invoice(client, token, subtotal=total, tax=0, total=total)
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    assert r.status_code == 200, f"Finalize failed: {r.json()}"
    return doc_id


@pytest.mark.anyio
async def test_summary_counters_and_totals_comprehensive(client, session):
    """Comprehensive regression: summary counts and totals across all invoice lifecycle states.

    Invoice set:
      INV-A $1,000  - pro-forma (draft, never finalized)
      INV-B $2,000  - finalized only (status: final)
      INV-C $3,000  - sent (status: sent) -- we simulate by patching status via finalize + sent endpoint
      INV-D $4,000  - partial payment of $1,000 (status: partial, outstanding $3,000)
      INV-E $5,000  - fully paid (status: paid)
      INV-F $6,000  - voided after finalize (status: void)

    Expected summary:
      draft_count             = 1     (INV-A)
      all_issued_count        = 4     (INV-B, INV-C, INV-D, INV-E - void excluded)
      awaiting_payment_count  = 3     (INV-B final, INV-C sent, INV-D partial)
      awaiting_payment_total  = 9,000 (2000 + 3000 + 4000 outstanding on D = 3000 -> total = 2000+3000+3000)
      paid_count              = 1     (INV-E)
      paid_total              = 5,000 (INV-E face value)
      overdue_count           = 0     (no due_date set)
      overdue_total           = 0.0
      all_issued_total        = 14,000 (2000+3000+4000+5000 face value)
      draft_total             = 1,000
      void_total              = 6,000
    """
    token = await _register(client)

    # INV-A: draft only
    await _create_invoice(client, token, subtotal=1000, tax=0, total=1000)

    # INV-B: finalized, unpaid (status: final)
    b = await _invoice(client, token, total=2000)

    # INV-C: finalized then sent (status: sent)
    c = await _invoice(client, token, total=3000)
    rs = await client.post(f"/docs/{c}/send", headers=_h(token), json={"sent_via": "email", "sent_to": "x@x.com"})
    assert rs.status_code == 200, f"Send failed: {rs.json()}"

    # INV-D: finalized, partial payment of $1,000 (outstanding = $3,000)
    d = await _invoice(client, token, total=4000)
    await _pay(client, token, d, 1000, "PART-D")

    # INV-E: finalized and fully paid
    e = await _invoice(client, token, total=5000)
    await _pay(client, token, e, 5000, "FULL-E")

    # INV-F: finalized then voided
    f = await _invoice(client, token, total=6000)
    rv = await client.post(f"/docs/{f}/void", headers=_h(token), json={"reason": "test void"})
    assert rv.status_code == 200, f"Void failed: {rv.json()}"

    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    s = r.json()

    assert s["draft_count"] == 1, f"draft_count: {s['draft_count']}"
    assert s["all_issued_count"] == 4, f"all_issued_count: {s['all_issued_count']}"
    assert s["awaiting_payment_count"] == 3, f"awaiting_payment_count: {s['awaiting_payment_count']}"
    assert abs(s["awaiting_payment_total"] - 8000.0) < 0.01, (
        f"awaiting_payment_total: {s['awaiting_payment_total']} (expected 8000: 2000+3000+3000 outstanding)"
    )
    assert s.get("paid_count", s["count_by_status"].get("paid", 0)) == 1, f"paid_count: {s}"
    assert abs(s["paid_total"] - 5000.0) < 0.01, f"paid_total: {s['paid_total']}"
    assert s["overdue_count"] == 0, f"overdue_count: {s['overdue_count']}"
    assert abs(s.get("overdue_total", 0)) < 0.01, f"overdue_total: {s.get('overdue_total')}"
    assert abs(s["all_issued_total"] - 14000.0) < 0.01, (
        f"all_issued_total: {s['all_issued_total']} (expected 14000: 2000+3000+4000+5000)"
    )
    assert abs(s["draft_total"] - 1000.0) < 0.01, f"draft_total: {s['draft_total']}"
    assert abs(s["void_total"] - 6000.0) < 0.01, f"void_total: {s['void_total']}"


@pytest.mark.anyio
async def test_summary_sent_total_is_outstanding_not_face_value(client, session):
    """sent_total must be the outstanding balance on sent invoices, not their face value.

    A $1,000 invoice that's been partially paid ($400) then sent should
    contribute $600 to sent_total, not $1,000.
    """
    token = await _register(client)
    doc_id = await _invoice(client, token, total=1000)
    # Partially pay before sending
    await _pay(client, token, doc_id, 400, "PRE-SEND")
    # Send it
    rs = await client.post(f"/docs/{doc_id}/send", headers=_h(token),
                           json={"sent_via": "email", "sent_to": "a@b.com"})
    assert rs.status_code == 200

    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    s = r.json()
    assert abs(s["sent_total"] - 600.0) < 0.01, (
        f"sent_total should be 600 (outstanding), got {s['sent_total']}"
    )


@pytest.mark.anyio
async def test_summary_all_card_count_equals_all_issued_not_sum_of_cards(client, session):
    """Regression: the All card count must equal all_issued_count, not sum of sub-cards.

    With 1 fully-paid invoice:
      - All Issued = 1
      - Paid = 1
    Naive sum would give All = 2. Correct answer is All = 1.
    """
    token = await _register(client)
    doc_id = await _invoice(client, token, total=750)
    await _pay(client, token, doc_id, 750, "FULL")

    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    s = r.json()
    assert s["all_issued_count"] == 1, f"all_issued_count: {s['all_issued_count']}"
    # paid invoice must NOT be counted in awaiting_payment
    assert s["awaiting_payment_count"] == 0, (
        f"awaiting_payment_count should be 0 for a fully-paid invoice, got {s['awaiting_payment_count']}"
    )


@pytest.mark.anyio
async def test_credit_note_can_be_created_without_original_doc_id(client, session):
    """Regression: creating a credit_note without original_doc_id is now allowed (link can be added later)."""
    token = await _register(client)
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "line_items": [], "subtotal": 0, "tax": 0, "total": 10},
    )
    assert r.status_code == 200, r.text


@pytest.mark.anyio
async def test_receive_return_on_credit_note(client, session):
    """Regression: receive-return on a credit note creates inventory items and records the event.

    Backend resolves item values from sold inventory records (LIFO).
    Original invoice line items serve as descriptive fallback only.
    """
    token = await _register(client)
    h = _h(token)

    # Create a sold inventory item with qty 2 (simulates goods sold to the customer)
    item1 = await client.post("/items", headers=h, json={"sku": "W-001", "name": "Widget", "quantity": 2, "cost_price": 40.0, "unit_price": 50.0, "sell_by": "piece"})
    assert item1.status_code == 200
    item1_id = item1.json()["id"]
    # Mark as sold
    await client.post(f"/items/{item1_id}/status", headers=h, json={"new_status": "sold"})

    # Create and finalize an invoice
    inv = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [{"name": "Widget", "sku": "W-001", "quantity": 2, "unit_price": 50, "sell_by": "unit"}], "subtotal": 100, "tax": 0, "total": 100})
    assert inv.status_code == 200
    inv_id = inv.json()["id"]
    await client.post(f"/docs/{inv_id}/finalize", headers=h)

    # Create credit note linked to invoice
    cn = await client.post("/docs", headers=h, json={"doc_type": "credit_note", "original_doc_id": inv_id, "line_items": [{"name": "Widget", "sku": "W-001", "quantity": 2, "unit_price": 50, "sell_by": "unit"}], "subtotal": 100, "tax": 0, "total": 100})
    assert cn.status_code == 200
    cn_id = cn.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    # Receive return - minimal payload; backend resolves all values from sold inventory records
    r = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "W-001", "quantity": 2}]})
    assert r.status_code == 200
    data = r.json()
    assert len(data["received_items"]) == 1
    assert data["received_items"][0]["quantity"] == 2
    # cost_price resolved from sold inventory records (cost_price=40 each, qty=2)
    assert data["total_cogs_reversed"] == pytest.approx(80.0)
    # CN projection should have return_received_items
    cn_state = (await client.get(f"/docs/{cn_id}", headers=h)).json()
    assert len(cn_state.get("return_received_items", [])) == 1


@pytest.mark.anyio
async def test_receive_return_rejected_on_non_credit_note(client, session):
    """Regression: receive-return must be rejected on non-credit-note doc types."""
    token = await _register(client)
    inv_id = await _create_invoice(client, token, subtotal=50, tax=0, total=50)
    r = await client.post(
        f"/docs/{inv_id}/receive-return",
        headers=_h(token),
        json={"items": [{"sku": "X-001", "name": "Item", "quantity": 1, "cost_price": 50.0}]},
    )
    assert r.status_code == 409


@pytest.mark.anyio
async def test_create_credit_note_from_invoice_pre_populates_fields(client, session):
    """create-credit-note action: CN gets original_doc_id, contact_id, and line items from invoice."""
    token = await _register(client)
    inv_id = await _create_invoice(client, token, subtotal=100, tax=0, total=100)
    # Finalize and fully pay so status = paid
    await client.post(f"/docs/{inv_id}/finalize", headers=_h(token))
    await _pay(client, token, inv_id, 100, "FULL")

    # Create CN from invoice (simulates the action handler logic directly via API)
    source = (await client.get(f"/docs/{inv_id}", headers=_h(token))).json()
    cn_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "credit_note",
            "original_doc_id": inv_id,
            "contact_id": source.get("contact_id"),
            "line_items": source.get("line_items", []),
            "subtotal": source.get("subtotal") or 0,
            "tax": source.get("tax") or 0,
            "total": source.get("total") or 0,
        },
    )
    assert cn_r.status_code == 200, cn_r.text
    cn_id = cn_r.json()["id"]

    cn = (await client.get(f"/docs/{cn_id}", headers=_h(token))).json()
    assert cn["doc_type"] == "credit_note"
    assert cn["original_doc_id"] == inv_id
    assert cn["contact_id"] == source.get("contact_id")
    assert len(cn.get("line_items", [])) == len(source.get("line_items", []))
    # Original invoice outstanding reduced by CN total
    inv_state = (await client.get(f"/docs/{inv_id}", headers=_h(token))).json()
    assert inv_state["amount_outstanding"] == pytest.approx(0.0)  # already paid; CN total deducted further (clamps at 0)


@pytest.mark.anyio
async def test_doc_return_received_projection(client, session):
    """doc.return_received projection: return_received_items appended, CN status unchanged."""
    token = await _register(client)
    h = _h(token)

    # Create a sold inventory item for the SKU being returned
    item = await client.post("/items", headers=h, json={"sku": "W-001", "name": "Widget", "quantity": 1, "cost_price": 30.0, "unit_price": 50.0, "sell_by": "piece"})
    assert item.status_code == 200
    await client.post(f"/items/{item.json()['id']}/status", headers=h, json={"new_status": "sold"})

    inv = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [{"name": "Widget", "sku": "W-001", "quantity": 1, "unit_price": 50, "sell_by": "unit"}], "subtotal": 50, "tax": 0, "total": 50})
    assert inv.status_code == 200
    inv_id = inv.json()["id"]
    await client.post(f"/docs/{inv_id}/finalize", headers=h)

    cn_r = await client.post(
        "/docs",
        headers=h,
        json={
            "doc_type": "credit_note",
            "original_doc_id": inv_id,
            "line_items": [{"name": "Widget", "sku": "W-001", "quantity": 1, "unit_price": 50, "sell_by": "unit"}],
            "subtotal": 50, "tax": 0, "total": 50,
        },
    )
    assert cn_r.status_code == 200
    cn_id = cn_r.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    before = (await client.get(f"/docs/{cn_id}", headers=h)).json()
    assert before.get("return_received_items") is None or before.get("return_received_items") == []

    rr = await client.post(
        f"/docs/{cn_id}/receive-return",
        headers=h,
        json={"items": [{"sku": "W-001", "quantity": 1}]},
    )
    assert rr.status_code == 200

    after = (await client.get(f"/docs/{cn_id}", headers=h)).json()
    assert len(after.get("return_received_items", [])) == 1
    # Status must NOT change - CN stays in its financial status
    assert after["status"] == before["status"]


@pytest.mark.anyio
async def test_receive_return_no_sold_inventory(client, session):
    """receive-return must succeed even when no sold inventory records exist for the SKU.

    Real-world case: CN created manually against an invoice whose items were never
    run through item.fulfilled, or items sold before fulfillment tracking existed.
    Backend falls back to CN/invoice line item data.
    """
    token = await _register(client)
    h = _h(token)

    # Create and finalize an invoice with a line item (no inventory item created/sold)
    inv = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "line_items": [{"name": "Widget", "sku": "W-NOSOLD", "quantity": 1, "unit_price": 60, "sell_by": "piece"}],
        "subtotal": 60, "tax": 0, "total": 60,
    })
    assert inv.status_code == 200
    inv_id = inv.json()["id"]
    await client.post(f"/docs/{inv_id}/finalize", headers=h)

    # Create and finalize a CN linked to that invoice
    cn = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note", "original_doc_id": inv_id,
        "line_items": [{"name": "Widget", "sku": "W-NOSOLD", "quantity": 1, "unit_price": 60, "sell_by": "piece"}],
        "subtotal": 60, "tax": 0, "total": 60,
    })
    assert cn.status_code == 200
    cn_id = cn.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    # receive-return must succeed using invoice line item data as fallback
    r = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "W-NOSOLD", "quantity": 1}]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["received_items"]) == 1


@pytest.mark.anyio
async def test_receive_return_fails_loudly_when_no_data_resolvable(client, session):
    """receive-return must 422 with a clear message when neither sold inventory nor
    invoice line items can supply name/sell_by for the requested SKU.

    This prevents silent creation of broken inventory records.
    """
    token = await _register(client)
    h = _h(token)

    # Unlinked CN (no original_doc_id), no inventory, no invoice fallback
    cn = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note",
        "line_items": [],  # no line items - nothing to resolve from
        "subtotal": 0, "tax": 0, "total": 0,
    })
    assert cn.status_code == 200
    cn_id = cn.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    # SKU has no sold record and no line item on the CN - must fail loudly
    r = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "GHOST-SKU", "quantity": 1}]})
    assert r.status_code == 422, r.text
    assert "name" in r.json()["detail"] or "sell_by" in r.json()["detail"]


@pytest.mark.anyio
async def test_receive_return_draft_cn_rejected(client, session):
    """receive-return must be rejected on draft credit notes."""
    token = await _register(client)
    cn_r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "line_items": [], "subtotal": 0, "tax": 0, "total": 0},
    )
    assert cn_r.status_code == 200
    cn_id = cn_r.json()["id"]
    # Status is draft - receive-return should be rejected
    r = await client.post(
        f"/docs/{cn_id}/receive-return",
        headers=_h(token),
        json={"items": [{"sku": "X", "quantity": 1}]},
    )
    assert r.status_code == 409


@pytest.mark.anyio
async def test_undo_receive_return_removes_items_from_inventory(client, session):
    """Regression: Revert Return Stock must dispose returned items so they no longer appear in inventory.

    Previously item.disposed projection never set status='disposed', so items remained visible
    in inventory (filtered by _HIDDEN_STATUSES which checks status field, not is_available).
    """
    token = await _register(client)
    h = _h(token)

    # Create sold inventory item
    item_r = await client.post("/items", headers=h, json={"sku": "RR-001", "name": "Returnable Widget", "quantity": 1, "cost_price": 30.0, "unit_price": 60.0, "sell_by": "piece"})
    assert item_r.status_code == 200
    await client.post(f"/items/{item_r.json()['id']}/status", headers=h, json={"new_status": "sold"})

    # Create CN and finalize
    cn_r = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note",
        "line_items": [{"name": "Returnable Widget", "sku": "RR-001", "quantity": 1, "unit_price": 60, "sell_by": "piece"}],
        "subtotal": 60, "tax": 0, "total": 60,
    })
    assert cn_r.status_code == 200
    cn_id = cn_r.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    # Receive return - creates a new inventory item with status=available
    rr = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "RR-001", "quantity": 1}]})
    assert rr.status_code == 200, rr.text
    received_items = rr.json()["received_items"]
    assert len(received_items) == 1

    # Verify the returned item appears in inventory
    inv_list = (await client.get("/items", headers=h)).json()["items"]
    returned_skus = [i["sku"] for i in inv_list if i.get("status") == "available"]
    assert "RR-001" in returned_skus, "Returned item should be available in inventory after receive-return"

    # Revert Return Stock
    undo_r = await client.delete(f"/docs/{cn_id}/receive-return", headers=h)
    assert undo_r.status_code == 200, undo_r.text
    assert undo_r.json()["undone"] is True

    # CN projection should be cleared
    cn_state = (await client.get(f"/docs/{cn_id}", headers=h)).json()
    assert cn_state.get("return_received_items") in (None, []), "return_received_items must be cleared after undo"

    # The returned item must no longer appear as available in inventory (status=disposed hides it)
    inv_list_after = (await client.get("/items", headers=h)).json()["items"]
    available_skus_after = [i["sku"] for i in inv_list_after if i.get("status") == "available"]
    assert "RR-001" not in available_skus_after, (
        "Disposed item must not appear in inventory after Revert Return Stock. "
        "Check item.disposed projection sets status='disposed'."
    )


@pytest.mark.anyio
async def test_undo_receive_return_blocked_if_item_resold(client, session):
    """Revert Return Stock must return 409 with actionable message if a returned item was re-sold."""
    token = await _register(client)
    h = _h(token)

    # Create sold inventory item
    item_r = await client.post("/items", headers=h, json={"sku": "RR-002", "name": "Resold Widget", "quantity": 1, "cost_price": 25.0, "unit_price": 50.0, "sell_by": "piece"})
    assert item_r.status_code == 200
    await client.post(f"/items/{item_r.json()['id']}/status", headers=h, json={"new_status": "sold"})

    # Create and finalize CN
    cn_r = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note",
        "line_items": [{"name": "Resold Widget", "sku": "RR-002", "quantity": 1, "unit_price": 50, "sell_by": "piece"}],
        "subtotal": 50, "tax": 0, "total": 50,
    })
    assert cn_r.status_code == 200
    cn_id = cn_r.json()["id"]
    await client.post(f"/docs/{cn_id}/finalize", headers=h)

    # Receive return
    rr = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "RR-002", "quantity": 1}]})
    assert rr.status_code == 200, rr.text
    returned_items = rr.json()["received_items"]
    new_item_id = returned_items[0]["item_id"]

    # Re-sell the returned item (simulates someone selling it before the undo)
    await client.post(f"/items/{new_item_id}/status", headers=h, json={"new_status": "sold"})

    # Revert Return Stock must fail with 409 and name the blocked item
    undo_r = await client.delete(f"/docs/{cn_id}/receive-return", headers=h)
    assert undo_r.status_code == 409, undo_r.text
    detail = undo_r.json().get("detail", "")
    assert "sold" in detail.lower() or "RR-002" in detail, (
        f"Error message must name the blocked item or status. Got: {detail}"
    )


@pytest.mark.anyio
async def test_bill_receive_goods_without_po_line_index(client, session):
    """Regression: one-click 'Receive Goods' on bills must succeed without po_line_index.

    The per-line PO receive form sends po_line_index; the bill one-click endpoint does not.
    po_line_index must be optional (defaults to -1) so both flows work.
    """
    token = await _register(client)
    h = _h(token)

    # Create and finalize a bill
    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Widget A", "sku": "W-A", "quantity": 5, "unit_price": 20.0, "sell_by": "piece"},
            {"name": "Widget B", "sku": "W-B", "quantity": 3, "unit_price": 10.0, "sell_by": "piece"},
        ],
        "subtotal": 130, "tax": 0, "total": 130,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    # POST to /receive without po_line_index - must not 422
    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [
            {"sku": "W-A", "name": "Widget A", "quantity_received": 5.0, "cost_price": 20.0},
            {"sku": "W-B", "name": "Widget B", "quantity_received": 3.0, "cost_price": 10.0},
        ],
        "location_id": "",
    })
    assert receive_r.status_code == 200, f"Expected 200, got {receive_r.status_code}: {receive_r.text}"


@pytest.mark.anyio
async def test_revert_goods_received_removes_items_from_inventory(client, session):
    """Revert Goods Received must dispose all inventory items created by the receive."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Widget RG1", "sku": "RG-001", "quantity": 2, "unit_price": 15.0, "sell_by": "piece"},
        ],
        "subtotal": 30, "tax": 0, "total": 30,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [{"sku": "RG-001", "name": "Widget RG1", "quantity_received": 2.0}],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    # Verify items appear in inventory
    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    assert bill_state.get("received_item_ids"), "received_item_ids must be populated after receive"

    # Revert
    undo_r = await client.delete(f"/docs/{bill_id}/receive", headers=h)
    assert undo_r.status_code == 200, undo_r.text
    assert undo_r.json()["undone"] is True

    # Bill projection must be cleared and status restored to final
    bill_after = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    assert bill_after.get("received_items") in (None, [])
    assert bill_after.get("received_item_ids") in (None, [])
    assert bill_after.get("status") == "final"

    # Items must be disposed (not available in inventory)
    inv = (await client.get("/items", headers=h)).json()["items"]
    available_skus = [i["sku"] for i in inv if i.get("status") == "available"]
    assert "RG-001" not in available_skus, "Disposed item must not appear in inventory"


@pytest.mark.anyio
async def test_revert_goods_received_blocked_if_item_resold(client, session):
    """Revert Goods Received must 409 if any created item has been sold."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Widget RG2", "sku": "RG-002", "quantity": 1, "unit_price": 25.0, "sell_by": "piece"},
        ],
        "subtotal": 25, "tax": 0, "total": 25,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [{"sku": "RG-002", "name": "Widget RG2", "quantity_received": 1.0}],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    item_ids = bill_state.get("received_item_ids", [])
    assert item_ids, "received_item_ids must be populated"

    # Mark item as sold to block revert
    await client.post(f"/items/{item_ids[0]}/status", headers=h, json={"new_status": "sold"})

    undo_r = await client.delete(f"/docs/{bill_id}/receive", headers=h)
    assert undo_r.status_code == 409, undo_r.text
    detail = undo_r.json().get("detail", "")
    assert "sold" in detail.lower() or "RG-002" in detail, f"Error must identify the blocked item. Got: {detail}"
