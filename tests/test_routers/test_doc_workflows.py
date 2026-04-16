# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
    assert "non-draft" in r.json()["detail"].lower()


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
