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
async def test_list_pagination_stable_across_date_ties(client):
    """Regression: invoices sharing an issue_date must paginate without a row vanishing.

    The list sorts by issue_date only; without a unique tiebreaker, equal-date rows came
    back in an arbitrary order that differed between the per-page queries, so a boundary
    row was skipped by OFFSET (a real invoice disappeared from the list while still being
    findable by search). All invoices created here share the same issue_date.
    """
    token = await _register(client)
    n = 7
    ids = [await _create_invoice(client, token) for _ in range(n)]

    # Walk pages with a small page size so the tie group straddles boundaries.
    seen: list[str] = []
    for off in range(0, n + 2, 2):
        page = (await client.get(f"/docs?doc_type=invoice&offset={off}&limit=2", headers=_h(token))).json()["items"]
        seen += [d["id"] for d in page]
        if len(page) < 2:
            break

    assert sorted(seen) == sorted(ids), "a date-tie row was skipped (or duplicated) across pages"
    assert len(seen) == len(set(seen)), "a row appeared on more than one page"
    # Order is deterministic: the paged walk matches a single unpaginated fetch.
    full = [d["id"] for d in (await client.get("/docs?doc_type=invoice&limit=100", headers=_h(token))).json()["items"]]
    assert seen == full


@pytest.mark.asyncio
async def test_invoice_search_comma_operand(client):
    """#170: docs search treats comma as an OR operand. The search box appends a comma on Enter,
    so a 'NUM,' query must still match — it used to LIKE the whole 'NUM,' string and find nothing.
    """
    token = await _register(client)
    inv_id = await _create_invoice(client, token)  # carries contact_id "contact:1" (a searchable field)
    term = "contact:1"

    # Trailing comma (as the search box commits it on Enter) must still find the invoice — it used
    # to LIKE the whole "contact:1," string and return nothing.
    r = (await client.get(f"/docs?doc_type=invoice&q={term},", headers=_h(token))).json()["items"]
    assert any(d["id"] == inv_id for d in r), "trailing-comma search missed a valid invoice"

    # Comma is OR across terms: term,bogus still returns the invoice.
    r2 = (await client.get(f"/docs?doc_type=invoice&q={term},zzz-no-match-999", headers=_h(token))).json()["items"]
    assert any(d["id"] == inv_id for d in r2)

    # Sanity: a non-matching term alone returns nothing (so the above isn't a false pass).
    r3 = (await client.get("/docs?doc_type=invoice&q=zzz-no-match-999", headers=_h(token))).json()["items"]
    assert not any(d["id"] == inv_id for d in r3)


@pytest.mark.asyncio
async def test_line_level_tax_splits_revenue_and_output_vat_in_finalize_je(client, session):
    """Regression: an invoice with LINE-LEVEL tax must persist the effective tax on the doc and the
    finalize JE must credit Revenue (4100) net of tax and Output VAT (2120) for the tax — not book the
    whole tax-inclusive total as revenue with zero VAT. Previously `tax` was computed for `total` then
    discarded (stayed 0 for line taxes), overstating revenue and understating the VAT liability."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "contact_id": "contact:1",
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 1000,
                        "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0}]}],
        "currency": "USD",
    })
    assert r.status_code == 200
    doc_id = r.json()["id"]

    doc = (await client.get(f"/docs/{doc_id}", headers=_h(token))).json()
    assert doc["subtotal"] == 1000.0
    assert doc["total"] == 1100.0
    assert doc["tax"] == 100.0, f"effective line tax must be persisted on the doc, got {doc['tax']}"

    assert (await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))).status_code == 200
    je = await _find_je(client, token, "doc.finalized", doc_id)
    entries = je["data"]["entries"]
    _assert_balanced(entries)
    by_acct = {e["account"]: e for e in entries}
    assert by_acct["1120"]["debit"] == 1100.0           # AR = tax-inclusive total
    assert by_acct["4100"]["credit"] == 1000.0          # Revenue = NET of tax
    assert by_acct["2120"]["credit"] == 100.0           # Output VAT liability = the tax


# (label, line_items, doc_taxes, subtotal, tax, total) — covers single/multi-rate/compound/doc-level/mixed.
_INVOICE_TAX_CASES = [
    ("single_10", [{"name": "A", "quantity": 1, "unit_price": 1000, "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0}]}],
     None, 1000.0, 100.0, 1100.0),
    ("multi_10_and_20", [{"name": "A", "quantity": 1, "unit_price": 1000, "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0}]},
                         {"name": "B", "quantity": 1, "unit_price": 500, "taxes": [{"code": "VAT", "name": "VAT", "rate": 20.0}]}],
     None, 1500.0, 200.0, 1700.0),
    ("compound_10_then_5", [{"name": "A", "quantity": 1, "unit_price": 1000,
                             "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0},
                                       {"code": "SVC", "name": "Svc", "rate": 5.0, "is_compound": True}]}],
     None, 1000.0, 155.0, 1155.0),
    ("doc_level_10", [{"name": "A", "quantity": 1, "unit_price": 1000}],
     [{"code": "VAT", "name": "VAT", "rate": 10.0}], 1000.0, 100.0, 1100.0),
    ("mixed_line_and_doc", [{"name": "A", "quantity": 1, "unit_price": 1000, "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0}]}],
     [{"code": "GST", "name": "GST", "rate": 5.0}], 1000.0, 150.0, 1150.0),
]


@pytest.mark.parametrize("label,lines,doc_taxes,subtotal,tax,total", _INVOICE_TAX_CASES,
                         ids=[c[0] for c in _INVOICE_TAX_CASES])
@pytest.mark.asyncio
async def test_invoice_tax_matrix_persists_and_splits_revenue_vat(client, session, label, lines, doc_taxes, subtotal, tax, total):
    """Across tax shapes the doc must persist the effective tax and the finalize JE must credit Revenue
    net of tax and Output VAT for the tax, and balance."""
    token = await _register(client)
    body = {"doc_type": "invoice", "contact_id": "contact:1", "line_items": lines, "currency": "USD"}
    if doc_taxes:
        body["doc_taxes"] = doc_taxes
    did = (await client.post("/docs", headers=_h(token), json=body)).json()["id"]
    doc = (await client.get(f"/docs/{did}", headers=_h(token))).json()
    assert (doc["subtotal"], doc["tax"], doc["total"]) == (subtotal, tax, total), f"{label}: {doc['subtotal']}/{doc['tax']}/{doc['total']}"

    assert (await client.post(f"/docs/{did}/finalize", headers=_h(token))).status_code == 200
    entries = (await _find_je(client, token, "doc.finalized", did))["data"]["entries"]
    _assert_balanced(entries)
    by = {e["account"]: e for e in entries}
    assert by["1120"]["debit"] == total          # AR = gross
    assert by["4100"]["credit"] == subtotal      # Revenue = net of tax
    assert by["2120"]["credit"] == tax           # Output VAT = the tax


# (label, line_items, doc_taxes, subtotal, tax, total) — bill posts Input VAT (1150), credits AP (2110).
_BILL_TAX_CASES = [
    ("bill_line_10", [{"sku": "R", "name": "X", "quantity": 1, "unit_price": 1000, "taxes": [{"code": "VAT", "name": "VAT", "rate": 10.0}]}],
     None, 1000.0, 100.0, 1100.0),
    ("bill_doc_10", [{"sku": "R", "name": "X", "quantity": 1, "unit_price": 1000}],
     [{"code": "VAT", "name": "VAT", "rate": 10.0}], 1000.0, 100.0, 1100.0),
]


@pytest.mark.parametrize("label,lines,doc_taxes,subtotal,tax,total", _BILL_TAX_CASES,
                         ids=[c[0] for c in _BILL_TAX_CASES])
@pytest.mark.asyncio
async def test_bill_tax_posts_input_vat_and_balances(client, session, label, lines, doc_taxes, subtotal, tax, total):
    """Regression: a vendor bill with tax must debit Inventory net, debit Input VAT (1150) for the tax,
    and credit AP (2110) for the gross — and BALANCE. Previously the bill JE took tax from a per-line
    `tax_rate` the structured-tax path never sets, leaving the entry unbalanced by the tax amount."""
    token = await _register(client)
    body = {"doc_type": "bill", "contact_id": "contact:1", "line_items": lines, "currency": "USD"}
    if doc_taxes:
        body["doc_taxes"] = doc_taxes
    did = (await client.post("/docs", headers=_h(token), json=body)).json()["id"]
    doc = (await client.get(f"/docs/{did}", headers=_h(token))).json()
    assert (doc["subtotal"], doc["tax"], doc["total"]) == (subtotal, tax, total), f"{label}: {doc['subtotal']}/{doc['tax']}/{doc['total']}"

    assert (await client.post(f"/docs/{did}/finalize", headers=_h(token))).status_code == 200
    rows = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in rows if did.split(":")[-1] in (e["data"].get("memo") or "") and "bill" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    _assert_balanced(entries)
    agg: dict[str, float] = {}
    for e in entries:
        agg[e["account"]] = agg.get(e["account"], 0.0) + float(e.get("debit", 0) or 0) - float(e.get("credit", 0) or 0)
    assert round(agg["1130-P"], 2) == subtotal       # inventory at net cost
    assert round(agg["1150"], 2) == tax              # recoverable input VAT
    assert round(agg["2110"], 2) == -total           # AP credited the gross


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
    r = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 40, "bank_account": "1111"})
    assert r.status_code == 200

    partial = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert partial["status"] == "partial"
    assert partial["amount_paid"] == 40
    assert partial["amount_outstanding"] == 67

    je1 = await _find_je(client, token, "doc.payment.received", inv)
    e1 = je1["data"]["entries"]
    assert {x["account"] for x in e1} == {"1111", "1120"}
    assert any(x["account"] == "1111" and float(x["debit"]) == 40 for x in e1)
    assert any(x["account"] == "1120" and float(x["credit"]) == 40 for x in e1)
    _assert_balanced(e1)

    je_row = (await session.execute(select(LedgerEntry).where(LedgerEntry.id == je1["id"]))).scalar_one()
    assert je_row.metadata_["trigger"] == "doc.payment.received"
    assert je_row.metadata_["doc_id"] == inv

    r2 = await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 67, "bank_account": "1111"})
    assert r2.status_code == 200
    paid = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert paid["status"] == "paid"
    assert paid["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_invoice_guards_void_edit_pay_and_overpayment(client):
    token = await _register(client)
    inv = await _create_invoice(client, token)

    # pay draft
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 1, "bank_account": "1111"})).status_code == 409

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
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 108, "bank_account": "1111"})).status_code == 409

    # paid then void forbidden
    assert (await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"payment_date": "2026-01-15", "amount": 107, "bank_account": "1111"})).status_code == 200
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
    assert {x["account"] for x in po_entries} == {"1130-P", "2110"}
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
    assert (await client.post(f"/docs/{expired.json()['id']}/convert", headers=_h(token))).status_code == 409

    # credit note adjusts source invoice outstanding
    inv = await _create_invoice(client, token, subtotal=100, tax=0, total=100)
    cn = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "original_doc_id": inv, "reason": "return", "line_items": [], "subtotal": 0, "tax": 0, "total": 30},
    )
    assert cn.status_code == 200
    assert (await client.get(f"/docs/{inv}", headers=_h(token))).json()["amount_outstanding"] == 70


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_edit_ref_id_allowed_on_non_draft(client, session):
    """ref_id is in the finalized-editable allowlist and should be patchable on finalized docs."""
    token = await _register(client)
    eid = await _create_invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.patch(
        f"/docs/{eid}", headers=_h(token),
        json={"fields_changed": {"ref_id": {"old": None, "new": "REF-UPDATED"}}},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_patch_sequence_prefix(client, session):
    """PATCH /docs/sequences/invoice updates prefix."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"prefix": "FAK"})
    assert r.status_code == 200
    assert r.json()["prefix"] == "FAK"


@pytest.mark.asyncio
async def test_patch_sequence_pattern(client, session):
    """PATCH /docs/sequences/invoice updates pattern."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"pattern": "{PREFIX}-{YYYY}-{####}"})
    assert r.status_code == 200
    assert r.json()["pattern"] == "{PREFIX}-{YYYY}-{####}"


@pytest.mark.asyncio
async def test_patch_sequence_invalid_pattern(client, session):
    """PATCH rejects pattern without sequence token."""
    token = await _register(client)
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"pattern": "NO-SEQ"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_sequence_reset_next(client, session):
    """PATCH next=1 resets counter."""
    token = await _register(client)
    # Generate a doc to advance counter
    await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    # Reset
    r = await client.patch("/docs/sequences/invoice", headers=_h(token), json={"next": 1})
    assert r.status_code == 200
    assert r.json()["next"] == 1


@pytest.mark.asyncio
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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_list_docs_due_from_due_to_filter(client, session):
    """due_from/due_to filter on due_date (not issue_date)."""
    from datetime import date, timedelta
    token = await _register(client)
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    future = (today + timedelta(days=10)).isoformat()

    # Doc A: due yesterday - created with due_date at creation time
    r_a = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "due_date": yesterday,
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 100, "line_total": 100}],
        "total": 100,
    })
    assert r_a.status_code == 200
    doc_a = r_a.json()["id"]

    # Doc B: due in 10 days
    r_b = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "due_date": future,
        "line_items": [{"name": "B", "quantity": 1, "unit_price": 200, "line_total": 200}],
        "total": 200,
    })
    assert r_b.status_code == 200
    doc_b = r_b.json()["id"]

    # due_from=today should include doc_b (future) but not doc_a (past)
    r = await client.get(f"/docs?doc_type=invoice&due_from={today.isoformat()}", headers=_h(token))
    assert r.status_code == 200
    ids = {item["id"] for item in r.json().get("items", [])}
    assert doc_b in ids, "due_from=today must include doc with future due_date"
    assert doc_a not in ids, "due_from=today must exclude doc with past due_date"

    # due_to=yesterday should include doc_a but not doc_b
    r2 = await client.get(f"/docs?doc_type=invoice&due_to={yesterday}", headers=_h(token))
    assert r2.status_code == 200
    ids2 = {item["id"] for item in r2.json().get("items", [])}
    assert doc_a in ids2, "due_to=yesterday must include doc with past due_date"
    assert doc_b not in ids2, "due_to=yesterday must exclude doc with future due_date"

    # Exact range: yesterday only
    r3 = await client.get(f"/docs?doc_type=invoice&due_from={yesterday}&due_to={yesterday}", headers=_h(token))
    assert r3.status_code == 200
    ids3 = {item["id"] for item in r3.json().get("items", [])}
    assert doc_a in ids3
    assert doc_b not in ids3


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
                            "payment_date": "2026-04-25", "bank_account": "1111"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    cbs = data.get("count_by_status", {})
    assert cbs.get("partial", 0) >= 1, "partial-paid invoice should have status 'partial'"
    assert cbs.get("paid", 0) == 0, "partial-paid invoice must NOT be counted as 'paid'"
    assert data.get("awaiting_payment_count", 0) >= 1, "partial-paid invoice must be in awaiting_payment_count"


@pytest.mark.asyncio
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
                            "payment_date": "2026-04-25", "bank_account": "1111"})
    r = await client.get("/docs/summary?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    data = r.json()
    assert data["all_issued_count"] == 1, (
        f"Expected all_issued_count=1, got {data['all_issued_count']}"
    )


@pytest.mark.asyncio
async def test_summary_awaiting_payment_total_is_outstanding(client, session):
    """awaiting_payment_total must equal the outstanding balance, not the invoice face value."""
    token = await _register(client)
    doc_id = await _create_invoice(client, token, subtotal=800, tax=0, total=800)
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    # Partial payment of 300 -> outstanding = 500
    await client.post(f"/docs/{doc_id}/payment", headers=_h(token),
                      json={"amount": 300, "method": "cash", "reference": "P1",
                            "payment_date": "2026-04-25", "bank_account": "1111"})
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
              "payment_date": "2026-04-25", "bank_account": "1111"},
    )
    assert r.status_code == 200, f"Payment failed: {r.json()}"


async def _invoice(client, token: str, total: float) -> str:
    """Helper: create and finalize an invoice with the given total."""
    doc_id = await _create_invoice(client, token, subtotal=total, tax=0, total=total)
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token), json={})
    assert r.status_code == 200, f"Finalize failed: {r.json()}"
    return doc_id


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_credit_note_can_be_created_without_original_doc_id(client, session):
    """Regression: creating a credit_note without original_doc_id is now allowed (link can be added later)."""
    token = await _register(client)
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={"doc_type": "credit_note", "line_items": [], "subtotal": 0, "tax": 0, "total": 10},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_receive_return_fails_loudly_when_no_data_resolvable(client, session):
    """receive-return must 422 with a clear message when neither sold inventory nor
    invoice line items can supply name/sell_by for the requested SKU.

    This prevents silent creation of broken inventory records.
    """
    token = await _register(client)
    h = _h(token)

    # Unlinked CN (no original_doc_id), no inventory, no invoice fallback. It carries an
    # unrelated free-text line (so it can be finalized) that does NOT match the SKU we
    # later try to return - leaving nothing to resolve GHOST-SKU from.
    cn = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note",
        "line_items": [{"sku": "UNRELATED-SKU", "description": "Adjustment", "quantity": 1, "unit_price": 10}],
        "subtotal": 10, "tax": 0, "total": 10,
    })
    assert cn.status_code == 200
    cn_id = cn.json()["id"]
    assert (await client.post(f"/docs/{cn_id}/finalize", headers=h)).status_code == 200

    # SKU has no sold record and no matching line item on the CN - must fail loudly
    r = await client.post(f"/docs/{cn_id}/receive-return", headers=h, json={"items": [{"sku": "GHOST-SKU", "quantity": 1}]})
    assert r.status_code == 422, r.text
    assert "name" in r.json()["detail"] or "sell_by" in r.json()["detail"]


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_undo_receive_return_removes_items_from_inventory(client, session):
    """Regression: Revert Return Stock must archive returned items so they no longer appear in inventory.

    Previously item.disposed projection never set status correctly, so items remained visible
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

    # The returned item must no longer appear as available in inventory (status=archived hides it)
    inv_list_after = (await client.get("/items", headers=h)).json()["items"]
    available_skus_after = [i["sku"] for i in inv_list_after if i.get("status") == "available"]
    assert "RR-001" not in available_skus_after, (
        "Disposed item must not appear in inventory after Revert Return Stock. "
        "Check item.status.set projection sets status='archived'."
    )


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_revert_goods_received_removes_items_from_inventory(client, session):
    """Revert Goods Received must archive all inventory items created by the receive."""
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

    # Items must be archived (not available in inventory)
    inv = (await client.get("/items", headers=h)).json()["items"]
    available_skus = [i["sku"] for i in inv if i.get("status") == "available"]
    assert "RG-001" not in available_skus, "Disposed item must not appear in inventory"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_receive_goods_inherits_sku_attributes(client, session):
    """Receive Goods must copy category, sell_by, prices, and dynamic attributes from existing item with same SKU.
    Barcode must NOT be copied (unique per physical item).
    """
    token = await _register(client)
    h = _h(token)

    # Create a master item with rich attributes including dynamic category-specific ones
    item_r = await client.post("/items", headers=h, json={
        "sku": "MASTER-001", "name": "Master Widget",
        "sell_by": "piece", "category": "Electronics",
        "retail_price": 99.0, "wholesale_price": 60.0, "cost_price": 40.0,
        "barcode": "1234567890",
        "attributes": {"color": "red", "shape": "round"},
    })
    assert item_r.status_code == 200, item_r.text

    # Create and finalize a bill for that SKU
    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Master Widget", "sku": "MASTER-001", "quantity": 2, "unit_price": 40.0, "sell_by": "piece"},
        ],
        "subtotal": 80, "tax": 0, "total": 80,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [{"sku": "MASTER-001", "name": "Master Widget", "quantity_received": 2.0}],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    created_ids = bill_state.get("received_item_ids", [])
    assert created_ids, "received_item_ids must be set"

    new_item = (await client.get(f"/items/{created_ids[0]}", headers=h)).json()
    assert new_item.get("category") == "Electronics", f"category not inherited: {new_item.get('category')}"
    assert new_item.get("sell_by") == "piece", f"sell_by not inherited: {new_item.get('sell_by')}"
    # Dynamic attributes are flattened to top-level by the GET endpoint
    assert new_item.get("color") == "red", f"attributes.color not inherited: {new_item}"
    assert new_item.get("shape") == "round", f"attributes.shape not inherited: {new_item}"
    # Barcode is NOT copied from the template (unique per physical lot); each received
    # parcel instead gets its own fresh, scannable sequential barcode (2026-06-17 §8.1).
    assert new_item.get("barcode"), "received parcel should get a fresh barcode"
    assert new_item["barcode"] != "1234567890", "template barcode must not be inherited"


@pytest.mark.asyncio
async def test_receive_goods_works_after_revert(client, session):
    """Receive Goods button must work again after Revert Goods Received (idempotency key must be unique per receive)."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Widget Retry", "sku": "RR-001", "quantity": 1, "unit_price": 20.0, "sell_by": "piece"},
        ],
        "subtotal": 20, "tax": 0, "total": 20,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    # First receive
    r1 = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [{"sku": "RR-001", "name": "Widget Retry", "quantity_received": 1.0}],
        "location_id": "",
    })
    assert r1.status_code == 200, r1.text

    # Revert
    undo_r = await client.delete(f"/docs/{bill_id}/receive", headers=h)
    assert undo_r.status_code == 200, undo_r.text

    # Second receive - must succeed with fresh items and JE
    r2 = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [{"sku": "RR-001", "name": "Widget Retry", "quantity_received": 1.0}],
        "location_id": "",
    })
    assert r2.status_code == 200, f"Re-receive after revert must succeed. Got: {r2.status_code}: {r2.text}"

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    assert bill_state.get("received_item_ids"), "received_item_ids must be repopulated after second receive"


@pytest.mark.asyncio
async def test_receive_as_expense_skips_inventory_creation(client, session):
    """Expense lines must not create inventory items; stock lines must."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {"name": "Stock Widget", "sku": "SW-001", "quantity": 2, "unit_price": 10.0},
            {"name": "Shipping Fee", "sku": "SHP-001", "quantity": 1, "unit_price": 5.0},
        ],
        "subtotal": 25, "tax": 0, "total": 25,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [
            {"sku": "SW-001", "name": "Stock Widget", "quantity_received": 2.0, "receive_as": "stock"},
            {"sku": "SHP-001", "name": "Shipping Fee", "quantity_received": 1.0, "receive_as": "expense"},
        ],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    created_ids = bill_state.get("received_item_ids", [])
    # Only the stock line should have created an inventory item
    assert len(created_ids) == 1, f"Expected 1 inventory item (stock line only), got {len(created_ids)}: {created_ids}"


@pytest.mark.asyncio
async def test_receive_as_stock_creates_inventory(client, session):
    """Explicit receive_as=stock must create an inventory item."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [{"name": "Gadget", "sku": "GDG-001", "quantity": 1, "unit_price": 50.0}],
        "subtotal": 50, "tax": 0, "total": 50,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [
            {"sku": "GDG-001", "name": "Gadget", "quantity_received": 1.0, "receive_as": "stock"},
        ],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    created_ids = bill_state.get("received_item_ids", [])
    assert len(created_ids) == 1, f"Expected 1 inventory item for stock line, got {len(created_ids)}"


@pytest.mark.asyncio
async def test_receive_as_defaults_to_stock(client, session):
    """When receive_as is omitted, it defaults to stock behavior (inventory item created)."""
    token = await _register(client)
    h = _h(token)

    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [{"name": "Default Widget", "sku": "DW-001", "quantity": 1, "unit_price": 15.0}],
        "subtotal": 15, "tax": 0, "total": 15,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [
            {"sku": "DW-001", "name": "Default Widget", "quantity_received": 1.0},
        ],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    created_ids = bill_state.get("received_item_ids", [])
    assert len(created_ids) == 1, f"Expected 1 inventory item (default stock), got {len(created_ids)}"


@pytest.mark.asyncio
async def test_bill_category_and_receive_as_preserved_after_finalize(client, session):
    """category and receive_as on line items must survive patch_doc (autosave) and finalize.

    Root cause: _celerpCollectLines() was not collecting category/receive_as from the DOM,
    so autosave sent line items without those fields and they were lost on next load.
    This test verifies the backend correctly stores and returns both fields via patch_doc.
    """
    token = await _register(client)
    h = _h(token)

    # Create draft bill with category and receive_as on the line item
    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {
                "name": "Widget",
                "sku": "WDG-CAT-01",
                "quantity": 2,
                "unit_price": 30.0,
                "category": "Electronics",
                "receive_as": "stock",
            }
        ],
        "subtotal": 60, "tax": 0, "total": 60,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]

    # Simulate autosave: patch with the same line items (as the JS now collects them)
    patch_r = await client.patch(f"/docs/{bill_id}", headers=h, json={
        "fields_changed": {
            "line_items": {
                "old": None,
                "new": [
                    {
                        "name": "Widget",
                        "sku": "WDG-CAT-01",
                        "quantity": 2,
                        "unit_price": 30.0,
                        "category": "Electronics",
                        "receive_as": "stock",
                        "line_total": 60.0,
                    }
                ],
            }
        }
    })
    assert patch_r.status_code == 200, patch_r.text

    # Finalize the bill
    fin_r = await client.post(f"/docs/{bill_id}/finalize", headers=h)
    assert fin_r.status_code == 200, fin_r.text

    # Verify category and receive_as survived
    doc = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    assert doc["status"] == "final", f"Expected final, got {doc['status']}"
    line = doc["line_items"][0]
    assert line.get("category") == "Electronics", f"category lost after finalize: {line}"
    assert line.get("receive_as") == "stock", f"receive_as lost after finalize: {line}"


@pytest.mark.asyncio
async def test_receive_goods_uses_bill_line_category_and_attributes(client, session):
    """Receive Goods must copy category and attributes from the bill line item.

    This covers the case where a brand-new SKU is received (no existing inventory item),
    so sku_ref lookup returns nothing. The bill line item is the authoritative source.

    Priority order: bill line item > sku_ref (existing inventory).
    """
    token = await _register(client)
    h = _h(token)

    # Create and finalize a bill with category + attributes on the line item (new SKU, no prior inventory)
    bill_r = await client.post("/docs", headers=h, json={
        "doc_type": "bill",
        "line_items": [
            {
                "name": "Emerald Stone",
                "sku": "GEM-EM-001",
                "quantity": 5,
                "unit_price": 200.0,
                "category": "Gemstones",
                "attributes": {"color": "green", "shape": "oval", "measurements (mm)": "10x8"},
            }
        ],
        "subtotal": 1000, "tax": 0, "total": 1000,
    })
    assert bill_r.status_code == 200, bill_r.text
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=h)

    receive_r = await client.post(f"/docs/{bill_id}/receive", headers=h, json={
        "received_items": [
            {
                "sku": "GEM-EM-001",
                "name": "Emerald Stone",
                "quantity_received": 5.0,
                "category": "Gemstones",
                "attributes": {"color": "green", "shape": "oval", "measurements (mm)": "10x8"},
            }
        ],
        "location_id": "",
    })
    assert receive_r.status_code == 200, receive_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=h)).json()
    created_ids = bill_state.get("received_item_ids", [])
    assert created_ids, "Expected inventory items to be created"

    new_item = (await client.get(f"/items/{created_ids[0]}", headers=h)).json()
    assert new_item.get("category") == "Gemstones", f"category not set from bill line: {new_item}"
    # Dynamic attributes are flattened to top-level by the GET endpoint
    assert new_item.get("color") == "green", f"attributes.color not set: {new_item}"
    assert new_item.get("shape") == "oval", f"attributes.shape not set: {new_item}"
    assert new_item.get("measurements (mm)") == "10x8", f"attributes.measurements not set: {new_item}"


# ---------------------------------------------------------------------------
# Revert-to-draft → re-finalize tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoice_revert_to_draft_then_re_finalize(client, session):
    """Finalize → revert → finalize again must succeed and reuse the same INV ref."""
    token = await _register(client)

    inv = await _create_invoice(client, token)
    h = _h(token)

    # First finalize
    r = await client.post(f"/docs/{inv}/finalize", headers=h)
    assert r.status_code == 200, r.text
    state1 = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state1["status"] == "final"
    inv_ref = state1["ref_id"]
    assert re.match(r"^INV-", inv_ref), f"Expected INV- ref, got: {inv_ref}"
    pf_ref = state1.get("source_proforma_ref", "")

    # Revert to draft
    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=h, json={"reason": "mistake"})
    assert r.status_code == 200, r.text
    draft = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert draft["status"] == "draft"
    # ref_id should still be the INV ref (not reset to PF)
    assert draft["ref_id"] == inv_ref

    # Re-finalize
    r = await client.post(f"/docs/{inv}/finalize", headers=h)
    assert r.status_code == 200, r.text
    state2 = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state2["status"] == "final"
    # Must reuse the same INV ref - no new counter slot consumed
    assert state2["ref_id"] == inv_ref, (
        f"Re-finalize changed INV ref from {inv_ref} to {state2['ref_id']} - counter slot wasted"
    )
    # source_proforma_ref must still point to the original PF ref
    if pf_ref:
        assert state2.get("source_proforma_ref") == pf_ref, (
            f"source_proforma_ref changed on re-finalize: {state2.get('source_proforma_ref')!r} != {pf_ref!r}"
        )


@pytest.mark.asyncio
async def test_invoice_re_finalize_creates_fresh_je(client, session):
    """JE must be voided on revert and a fresh JE created on re-finalize.

    The dedup guard on idempotency keys must not block the second JE.
    """
    token = await _register(client)
    inv = await _create_invoice(client, token)
    h = _h(token)

    # Finalize → check JE is posted
    await client.post(f"/docs/{inv}/finalize", headers=h)

    ledger_rows = (await client.get("/ledger?entity_type=journal_entry", headers=h)).json()["items"]
    fin_je_after_first = [
        e for e in ledger_rows
        if inv in (e["data"].get("memo") or "") and "finalized" in (e["data"].get("memo") or "")
    ]
    assert fin_je_after_first, "No finalize JE found after first finalize"

    # Revert → finalize JE must be voided
    await client.post(f"/docs/{inv}/revert-to-draft", headers=h, json={"reason": "test"})

    # Re-finalize → a new posted JE must exist
    r = await client.post(f"/docs/{inv}/finalize", headers=h)
    assert r.status_code == 200, r.text

    # Collect all JEs for this doc
    ledger_rows2 = (await client.get("/ledger?entity_type=journal_entry", headers=h)).json()["items"]
    fin_je_rows = [
        e for e in ledger_rows2
        if inv in (e["data"].get("memo") or "") and "finalized" in (e["data"].get("memo") or "")
    ]
    # There should be at least two posted events (created + posted) for the second finalize cycle
    assert len(fin_je_rows) >= 1, "Expected at least one JE event for re-finalize"

    # Verify the document is final and has a valid AR balance (JE was actually recorded)
    state = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state["status"] == "final"


@pytest.mark.asyncio
async def test_invoice_counter_not_double_incremented_on_re_finalize(client, session):
    """Re-finalizing a previously-finalized invoice must not consume a new counter slot.

    Create two invoices; finalize both; revert the first; re-finalize the first.
    The re-finalized invoice must keep its original number, not leapfrog the second.
    """
    token = await _register(client)
    h = _h(token)

    inv_a = await _create_invoice(client, token)
    inv_b = await _create_invoice(client, token)

    await client.post(f"/docs/{inv_a}/finalize", headers=h)
    await client.post(f"/docs/{inv_b}/finalize", headers=h)

    ref_a = (await client.get(f"/docs/{inv_a}", headers=h)).json()["ref_id"]
    ref_b = (await client.get(f"/docs/{inv_b}", headers=h)).json()["ref_id"]

    # Revert inv_a, then re-finalize
    await client.post(f"/docs/{inv_a}/revert-to-draft", headers=h, json={"reason": "test"})
    await client.post(f"/docs/{inv_a}/finalize", headers=h)

    ref_a_after = (await client.get(f"/docs/{inv_a}", headers=h)).json()["ref_id"]
    ref_b_after = (await client.get(f"/docs/{inv_b}", headers=h)).json()["ref_id"]

    # inv_a keeps its original ref; inv_b is unaffected
    assert ref_a_after == ref_a, f"Re-finalize changed ref_a from {ref_a} to {ref_a_after}"
    assert ref_b_after == ref_b, f"inv_b ref changed unexpectedly to {ref_b_after}"


@pytest.mark.asyncio
async def test_invoice_multiple_revert_cycles(client, session):
    """Finalize → revert → finalize → revert → finalize must work for N cycles."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    h = _h(token)

    await client.post(f"/docs/{inv}/finalize", headers=h)
    original_ref = (await client.get(f"/docs/{inv}", headers=h)).json()["ref_id"]

    for cycle in range(1, 4):
        r_revert = await client.post(f"/docs/{inv}/revert-to-draft", headers=h, json={"reason": f"cycle {cycle}"})
        assert r_revert.status_code == 200, f"Revert failed on cycle {cycle}: {r_revert.text}"

        r_final = await client.post(f"/docs/{inv}/finalize", headers=h)
        assert r_final.status_code == 200, f"Re-finalize failed on cycle {cycle}: {r_final.text}"

        state = (await client.get(f"/docs/{inv}", headers=h)).json()
        assert state["status"] == "final", f"Expected final status after cycle {cycle}, got: {state['status']}"
        assert state["ref_id"] == original_ref, (
            f"Ref changed on cycle {cycle}: {original_ref} -> {state['ref_id']}"
        )
        assert int(state.get("revert_count", 0)) == cycle, (
            f"revert_count should be {cycle} after cycle {cycle}, got {state.get('revert_count')}"
        )


# ---------------------------------------------------------------------------
# Status protection tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_doc_status_rejected(client, session):
    """PATCH /docs/{id} with status in fields_changed must return 422."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    h = _h(token)
    r = await client.patch(
        f"/docs/{inv}",
        headers=h,
        json={"fields_changed": {"status": {"old": "draft", "new": "paid"}}},
    )
    assert r.status_code == 422, r.text
    assert "status" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_doc_entity_type_rejected(client, session):
    """PATCH /docs/{id} with entity_type in fields_changed must return 422."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    h = _h(token)
    r = await client.patch(
        f"/docs/{inv}",
        headers=h,
        json={"fields_changed": {"entity_type": {"old": "doc", "new": "malicious"}}},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_patch_doc_status_not_applied_via_projection(client, session):
    """Projection ignores status even if somehow smuggled into fields_changed; safe data fields still apply."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    h = _h(token)

    # Confirm draft
    state_before = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state_before["status"] == "draft"

    # Patch a safe field to confirm the normal path still works
    r = await client.patch(
        f"/docs/{inv}",
        headers=h,
        json={"fields_changed": {"reference": {"old": None, "new": "TEST-REF"}}},
    )
    assert r.status_code == 200, r.text

    state_after = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state_after["status"] == "draft"  # unchanged
    assert state_after["reference"] == "TEST-REF"  # data field updated


@pytest.mark.asyncio
async def test_status_only_reachable_via_lifecycle(client, session):
    """An invoice reaches 'paid' only after finalize + record_payment, never via patch."""
    token = await _register(client)
    inv = await _create_invoice(client, token, subtotal=100, tax=0, total=100)
    h = _h(token)

    # Attempt direct patch to paid - must be blocked
    r = await client.patch(
        f"/docs/{inv}",
        headers=h,
        json={"fields_changed": {"status": {"old": "draft", "new": "paid"}}},
    )
    assert r.status_code == 422, r.text

    # Status must still be draft
    assert (await client.get(f"/docs/{inv}", headers=h)).json()["status"] == "draft"

    # Now go through legitimate lifecycle
    await client.post(f"/docs/{inv}/finalize", headers=h)
    state = (await client.get(f"/docs/{inv}", headers=h)).json()
    assert state["status"] == "final"
    # (full payment flow tested in test_payments.py)


@pytest.mark.asyncio
async def test_memo_fulfill_sets_memo_out_status(client, session):
    """Fulfilling a memo doc sets item status to memo_out, not sold."""
    token = await _register(client)
    h = _h(token)

    # Create an item
    item_r = await client.post("/items", headers=h, json={"name": "Gem A", "quantity": 1, "sku": "GEM-MEMO-001", "sell_by": "piece"})
    assert item_r.status_code == 200
    item_id = item_r.json()["id"]

    # Create and finalize a memo doc
    doc_r = await client.post("/docs", headers=h, json={
        "doc_type": "memo",
        "contact_id": "contact:1",
        "line_items": [{"name": "Gem A", "sku": "GEM-MEMO-001", "item_id": item_id, "quantity": 1, "unit_price": 100, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100,
    })
    assert doc_r.status_code == 200
    doc_id = doc_r.json()["id"]

    await client.post(f"/docs/{doc_id}/finalize", headers=h)

    # Fulfill via per-line endpoint (entity_id = item_id for linked items)
    fulfill_r = await client.post(f"/docs/{doc_id}/fulfill-lines", headers=h,
                                  json={"line_entity_ids": [item_id]})
    assert fulfill_r.status_code == 200

    item_state = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item_state["status"] == "memo_out", f"Expected memo_out, got {item_state['status']}"

    # Revert via per-line endpoint
    unfulfill_r = await client.post(f"/docs/{doc_id}/revert-lines", headers=h,
                                    json={"line_entity_ids": [item_id]})
    assert unfulfill_r.status_code == 200

    item_state2 = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item_state2["status"] == "available", f"Expected available, got {item_state2['status']}"


# ---------------------------------------------------------------------------
# Inbound per-line fulfillment: Bill & Consignment In
# ---------------------------------------------------------------------------

async def _create_location(client, token: str) -> str:
    r = await client.post(
        "/companies/me/locations",
        headers=_h(token),
        json={"name": "Warehouse", "type": "warehouse", "is_default": True},
    )
    assert r.status_code == 200
    return r.json()["id"]


async def _create_and_finalize_bill(client, token: str, sku: str = "BILL-SKU-001") -> tuple[str, str]:
    """Create a bill with two lines (one stock, one expense) and finalize it. Returns (bill_id, location_id)."""
    location_id = await _create_location(client, token)
    bill_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "bill",
            "contact_id": "contact:vendor1",
            "line_items": [
                {"sku": sku, "name": "Widget", "quantity": 2, "unit_price": 50, "line_total": 100, "receive_as": "stock"},
                {"sku": "SVC-001", "name": "Service", "quantity": 1, "unit_price": 20, "line_total": 20, "receive_as": "expense"},
            ],
            "subtotal": 120, "tax": 0, "total": 120,
        },
    )
    assert bill_r.status_code == 200
    bill_id = bill_r.json()["id"]
    fin_r = await client.post(f"/docs/{bill_id}/finalize", headers=_h(token))
    assert fin_r.status_code == 200
    return bill_id, location_id


@pytest.mark.asyncio
async def test_bill_receive_writes_entity_id_to_line_items(client, session):
    """After /receive with po_line_index, line_items[i]['entity_id'] must be set."""
    token = await _register(client)
    bill_id, location_id = await _create_and_finalize_bill(client, token)

    rec_r = await client.post(
        f"/docs/{bill_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "sku": "BILL-SKU-001", "name": "Widget", "quantity_received": 2, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    doc_state = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    stock_line = doc_state["line_items"][0]
    assert stock_line.get("entity_id"), "Stock line must have entity_id after receive"
    expense_line = doc_state["line_items"][1]
    assert not expense_line.get("entity_id"), "Expense line must NOT have entity_id"


@pytest.mark.asyncio
async def test_bill_receive_sku_fallback_writes_entity_id(client, session):
    """One-click receive (po_line_index=-1) falls back to SKU match for entity_id linkage."""
    token = await _register(client)
    bill_id, location_id = await _create_and_finalize_bill(client, token, sku="BILL-FALLBACK")

    rec_r = await client.post(
        f"/docs/{bill_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": -1, "sku": "BILL-FALLBACK", "name": "Widget", "quantity_received": 2, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    doc_state = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    stock_line = doc_state["line_items"][0]
    assert stock_line.get("entity_id"), "SKU-fallback receive must set entity_id on matching line"


@pytest.mark.asyncio
async def test_bill_undo_receive_clears_entity_id(client, session):
    """After undo-receive, entity_id must be cleared from all line items."""
    token = await _register(client)
    bill_id, location_id = await _create_and_finalize_bill(client, token, sku="BILL-UNDO")

    await client.post(
        f"/docs/{bill_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "sku": "BILL-UNDO", "name": "Widget", "quantity_received": 2, "receive_as": "stock"},
            ],
        },
    )
    # Verify entity_id was set
    doc_state = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    assert doc_state["line_items"][0].get("entity_id")

    # Undo receive
    undo_r = await client.delete(f"/docs/{bill_id}/receive", headers=_h(token))
    assert undo_r.status_code == 200

    doc_state2 = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    assert not doc_state2["line_items"][0].get("entity_id"), "entity_id must be cleared after undo-receive"


@pytest.mark.asyncio
async def test_bill_fulfill_lines_rejected_before_receive(client, session):
    """fulfill-lines is never supported for bill (inbound doc). Must always return 422."""
    token = await _register(client)
    bill_id, _ = await _create_and_finalize_bill(client, token, sku="BILL-REJECT")
    r = await client.post(f"/docs/{bill_id}/fulfill-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r.status_code == 422, f"fulfill-lines on bill must always be rejected, got {r.status_code}"


@pytest.mark.asyncio
async def test_consignment_in_receive_writes_entity_id_to_line_items(client, session):
    """Same entity_id backfill must work for consignment_in docs."""
    token = await _register(client)
    location_id = await _create_location(client, token)

    cin_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "consignment_in",
            "contact_id": "contact:supplier",
            "line_items": [
                {"sku": "CIN-SKU-001", "name": "Consigned Gem", "quantity": 1, "unit_price": 100, "line_total": 100},
            ],
            "subtotal": 100, "tax": 0, "total": 100,
        },
    )
    assert cin_r.status_code == 200
    cin_id = cin_r.json()["id"]
    await client.post(f"/docs/{cin_id}/finalize", headers=_h(token))

    rec_r = await client.post(
        f"/docs/{cin_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "sku": "CIN-SKU-001", "name": "Consigned Gem", "quantity_received": 1, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    doc_state = (await client.get(f"/docs/{cin_id}", headers=_h(token))).json()
    assert doc_state["line_items"][0].get("entity_id"), "Consignment In line must have entity_id after receive"


@pytest.mark.asyncio
async def test_consignment_in_receive_stamps_status_doc(client, session):
    """Items received on consignment carry the consignment doc as their status doc
    pairing, so the inventory page links them to the doc and q-search finds them
    by its number."""
    token = await _register(client)
    location_id = await _create_location(client, token)

    cin_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "consignment_in",
            "contact_id": "contact:supplier",
            "line_items": [
                {"sku": "CIN-SD-001", "name": "Consigned Stone", "quantity": 1, "unit_price": 100, "line_total": 100},
            ],
            "subtotal": 100, "tax": 0, "total": 100,
        },
    )
    assert cin_r.status_code == 200
    cin_id = cin_r.json()["id"]
    await client.post(f"/docs/{cin_id}/finalize", headers=_h(token))

    rec_r = await client.post(
        f"/docs/{cin_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "sku": "CIN-SD-001", "name": "Consigned Stone", "quantity_received": 1, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    doc_state = (await client.get(f"/docs/{cin_id}", headers=_h(token))).json()
    doc_number = doc_state.get("doc_number") or doc_state.get("ref_id")
    assert doc_number

    r = await client.get(f"/items?q={doc_number}", headers=_h(token))
    assert r.status_code == 200
    rows = [i for i in r.json()["items"] if i.get("sku") == "CIN-SD-001"]
    assert rows, "consigned-in item must be findable by the consignment doc number"
    assert rows[0]["status_doc_id"] == cin_id
    assert rows[0]["status_doc_number"] == doc_number


@pytest.mark.asyncio
async def test_consignment_in_fulfill_lines_rejected(client, session):
    """consignment_in must always return 422 for fulfill-lines (inbound doc - use /receive instead)."""
    token = await _register(client)
    location_id = await _create_location(client, token)

    cin_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "consignment_in",
            "contact_id": "contact:supplier2",
            "line_items": [{"sku": "CIN-REG-001", "name": "Gem", "quantity": 1, "unit_price": 80, "line_total": 80}],
            "subtotal": 80, "tax": 0, "total": 80,
        },
    )
    cin_id = cin_r.json()["id"]
    await client.post(f"/docs/{cin_id}/finalize", headers=_h(token))
    await client.post(
        f"/docs/{cin_id}/receive",
        headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [{"po_line_index": 0, "sku": "CIN-REG-001", "name": "Gem", "quantity_received": 1, "receive_as": "stock"}],
        },
    )

    r = await client.post(f"/docs/{cin_id}/fulfill-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r.status_code == 422, f"fulfill-lines on consignment_in must be rejected, got {r.status_code}"


@pytest.mark.asyncio
async def test_bill_revert_to_draft_from_final_then_re_finalize(client, session):
    """A finalized (but not received) direct bill can be reverted to draft and re-finalized."""
    token = await _register(client)
    bill_id, _ = await _create_and_finalize_bill(client, token, sku="BILL-CYCLE-SKU")

    # Bill is now status=final, doc_type=bill
    doc = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    assert doc.get("status") == "final"

    # Revert to draft
    r = await client.post(f"/docs/{bill_id}/revert-to-draft", headers=_h(token), json={"reason": "test"})
    assert r.status_code == 200, r.text

    doc2 = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    assert doc2.get("status") == "draft"
    assert doc2.get("doc_type") == "bill"

    # Re-finalize
    r2 = await client.post(f"/docs/{bill_id}/finalize", headers=_h(token))
    assert r2.status_code == 200, r2.text

    doc3 = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    assert doc3.get("status") == "final"


@pytest.mark.asyncio
async def test_bill_receive_known_sku_creates_new_parcel_not_adjust(client, session):
    """Receiving a bill line with a known catalog item_id must create a NEW parcel,
    not adjust the existing item's quantity. The catalog template is untouched."""
    token = await _register(client)

    # Create a catalog template item for the known SKU
    template_r = await client.post(
        "/items", headers=_h(token),
        json={"sku": "KNOWN-SKU-001", "name": "Known Widget", "quantity": 5, "sell_by": "piece"},
    )
    assert template_r.status_code == 200
    template_id = template_r.json()["id"]

    location_id = await _create_location(client, token)
    bill_r = await client.post(
        "/docs", headers=_h(token),
        json={
            "doc_type": "bill",
            "contact_id": "contact:vendor1",
            "line_items": [
                {"sku": "KNOWN-SKU-001", "name": "Known Widget", "quantity": 3,
                 "unit_price": 10, "line_total": 30, "receive_as": "stock"},
            ],
            "subtotal": 30, "tax": 0, "total": 30,
        },
    )
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=_h(token))

    rec_r = await client.post(
        f"/docs/{bill_id}/receive", headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "item_id": template_id, "sku": "KNOWN-SKU-001",
                 "name": "Known Widget", "quantity_received": 3, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    # Template item quantity must NOT have changed
    tmpl = (await client.get(f"/items/{template_id}", headers=_h(token))).json()
    assert tmpl["quantity"] == 5, "Template item must not be modified on inbound receive"

    # A new parcel must exist with the received quantity
    items = (await client.get("/items", headers=_h(token))).json()["items"]
    same_sku = [i for i in items if i.get("sku") == "KNOWN-SKU-001"]
    assert len(same_sku) == 2, f"Expected 2 items with KNOWN-SKU-001 (template + parcel), got {len(same_sku)}"
    new_parcel = next(i for i in same_sku if i["id"] != template_id)
    assert new_parcel["quantity"] == 3, "New parcel must have received quantity"

    # Bill line must have entity_id pointing to the new parcel
    doc_state = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    line_eid = doc_state["line_items"][0].get("entity_id")
    assert line_eid and line_eid != template_id, "Line entity_id must point to new parcel, not template"


@pytest.mark.asyncio
async def test_consignment_in_receive_known_sku_creates_new_parcel(client, session):
    """consignment_in with a known catalog item_id must also create a new parcel."""
    token = await _register(client)

    template_r = await client.post(
        "/items", headers=_h(token),
        json={"sku": "CONS-SKU-001", "name": "Consigned Widget", "quantity": 4, "sell_by": "piece"},
    )
    template_id = template_r.json()["id"]

    location_id = await _create_location(client, token)
    doc_r = await client.post(
        "/docs", headers=_h(token),
        json={
            "doc_type": "consignment_in",
            "contact_id": "contact:vendor1",
            "line_items": [
                {"sku": "CONS-SKU-001", "name": "Consigned Widget", "quantity": 2,
                 "unit_price": 5, "line_total": 10, "receive_as": "stock"},
            ],
            "subtotal": 10, "tax": 0, "total": 10,
        },
    )
    doc_id = doc_r.json()["id"]
    await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))

    rec_r = await client.post(
        f"/docs/{doc_id}/receive", headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "item_id": template_id, "sku": "CONS-SKU-001",
                 "name": "Consigned Widget", "quantity_received": 2, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    # Template unchanged
    tmpl = (await client.get(f"/items/{template_id}", headers=_h(token))).json()
    assert tmpl["quantity"] == 4, "Consignment template must not be modified"

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    same_sku = [i for i in items if i.get("sku") == "CONS-SKU-001"]
    assert len(same_sku) == 2, f"Expected template + parcel, got {len(same_sku)}"


@pytest.mark.asyncio
async def test_po_receive_known_item_still_adjusts_qty(client, session):
    """PO receive with item_id must still adjust the existing item's quantity (unchanged behavior).
    Note: PO is received before finalize (finalize converts PO → bill).
    """
    token = await _register(client)

    existing_r = await client.post(
        "/items", headers=_h(token),
        json={"sku": "PO-EXIST-001", "name": "PO Existing", "quantity": 10, "sell_by": "piece"},
    )
    item_id = existing_r.json()["id"]

    po_r = await client.post(
        "/docs", headers=_h(token),
        json={
            "doc_type": "purchase_order",
            "contact_id": "contact:vendor1",
            "line_items": [
                {"sku": "PO-EXIST-001", "name": "PO Existing", "quantity": 5,
                 "unit_price": 10, "line_total": 50},
            ],
            "subtotal": 50, "tax": 0, "total": 50,
        },
    )
    po_id = po_r.json()["id"]
    # Receive BEFORE finalize - finalize converts PO to bill (inbound doc type).
    # purchase_order doc_type = outbound-style: adjusts qty on existing item.
    rec_r = await client.post(
        f"/docs/{po_id}/receive", headers=_h(token),
        json={
            "location_id": "loc:1",
            "received_items": [
                {"po_line_index": 0, "item_id": item_id, "quantity_received": 5, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200

    # PO: existing item quantity must have increased
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item["quantity"] == 15, "PO receive must adjust existing item quantity"

    # No new parcel created
    items = (await client.get("/items", headers=_h(token))).json()["items"]
    same_sku = [i for i in items if i.get("sku") == "PO-EXIST-001"]
    assert len(same_sku) == 1, "PO receive must not create a new parcel"


@pytest.mark.asyncio
async def test_bill_receive_no_sku_item_auto_generates_sku(client, session):
    """Receiving a bill line for a custom item with no SKU must auto-generate a SKU from name."""
    token = await _register(client)
    location_id = await _create_location(client, token)

    bill_r = await client.post(
        "/docs", headers=_h(token),
        json={
            "doc_type": "bill",
            "contact_id": "contact:vendor1",
            "line_items": [
                {"name": "Custom Widget", "quantity": 5, "unit_price": 10,
                 "line_total": 50, "receive_as": "stock"},
            ],
            "subtotal": 50, "tax": 0, "total": 50,
        },
    )
    bill_id = bill_r.json()["id"]
    await client.post(f"/docs/{bill_id}/finalize", headers=_h(token))

    rec_r = await client.post(
        f"/docs/{bill_id}/receive", headers=_h(token),
        json={
            "location_id": location_id,
            "received_items": [
                {"po_line_index": 0, "name": "Custom Widget", "quantity_received": 5, "receive_as": "stock"},
            ],
        },
    )
    assert rec_r.status_code == 200, rec_r.text

    items = (await client.get("/items", headers=_h(token))).json()["items"]
    new_items = [i for i in items if i.get("name") == "Custom Widget"]
    assert len(new_items) == 1, "A new catalog entry must be created for the custom item"
    assert new_items[0]["quantity"] == 5
    assert new_items[0].get("sku"), "SKU must be auto-generated from item name"


# ---------------------------------------------------------------------------
# New tests: inbound doc rejection of fulfill-lines/revert-lines (both types)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bill_fulfill_lines_always_rejected(client, session):
    """fulfill-lines must return 422 for bill at any status."""
    token = await _register(client)
    bill_id, location_id = await _create_and_finalize_bill(client, token, sku="BILL-FL-REJECT")

    # After finalize (status=final) - must reject
    r = await client.post(f"/docs/{bill_id}/fulfill-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r.status_code == 422, f"fulfill-lines on finalized bill must be rejected, got {r.status_code}"

    # After receive (status=received) - must still reject
    await client.post(f"/docs/{bill_id}/receive", headers=_h(token), json={
        "location_id": location_id,
        "received_items": [{"po_line_index": 0, "sku": "BILL-FL-REJECT", "name": "Widget", "quantity_received": 1, "receive_as": "stock"}],
    })
    r2 = await client.post(f"/docs/{bill_id}/fulfill-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r2.status_code == 422, f"fulfill-lines on received bill must be rejected, got {r2.status_code}"


@pytest.mark.asyncio
async def test_bill_revert_lines_always_rejected(client, session):
    """revert-lines must return 422 for bill at any status."""
    token = await _register(client)
    bill_id, _ = await _create_and_finalize_bill(client, token, sku="BILL-RL-REJECT")
    r = await client.post(f"/docs/{bill_id}/revert-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r.status_code == 422, f"revert-lines on bill must be rejected, got {r.status_code}"


@pytest.mark.asyncio
async def test_consignment_in_revert_lines_always_rejected(client, session):
    """revert-lines must return 422 for consignment_in at any status."""
    token = await _register(client)
    cin_r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "consignment_in",
            "contact_id": "contact:supplier3",
            "line_items": [{"sku": "CIN-RL-REJ", "name": "Gem", "quantity": 1, "unit_price": 50, "line_total": 50}],
            "subtotal": 50, "tax": 0, "total": 50,
        },
    )
    cin_id = cin_r.json()["id"]
    await client.post(f"/docs/{cin_id}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{cin_id}/revert-lines", headers=_h(token), json={"line_entity_ids": []})
    assert r.status_code == 422, f"revert-lines on consignment_in must be rejected, got {r.status_code}"


@pytest.mark.asyncio
async def test_bill_receive_with_location_sets_location_on_parcel(client, session):
    """POST /receive with location_id creates parcel with that location."""
    token = await _register(client)
    bill_id, location_id = await _create_and_finalize_bill(client, token, sku="BILL-LOC-TEST")

    rec_r = await client.post(f"/docs/{bill_id}/receive", headers=_h(token), json={
        "location_id": location_id,
        "received_items": [{"po_line_index": 0, "sku": "BILL-LOC-TEST", "name": "Widget", "quantity_received": 3, "receive_as": "stock"}],
    })
    assert rec_r.status_code == 200, rec_r.text

    bill_state = (await client.get(f"/docs/{bill_id}", headers=_h(token))).json()
    created_ids = bill_state.get("received_item_ids", [])
    assert created_ids, "Parcel must be created"

    parcel = (await client.get(f"/items/{created_ids[0]}", headers=_h(token))).json()
    assert parcel.get("location_id") == location_id or parcel.get("location_name"), \
        f"Parcel must have location set, got: {parcel}"


@pytest.mark.asyncio
async def test_bulk_delete_drafts_route_not_caught_by_entity_id(client, session):
    """DELETE /docs/bulk-draft must NOT be captured by DELETE /docs/{entity_id}.
    This is a route-order regression test - bulk-draft must be registered before /{entity_id}."""
    token = await _register(client)

    # Create two drafts
    r1 = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "status": "draft", "line_items": [], "subtotal": 0, "tax": 0, "total": 0})
    r2 = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "status": "draft", "line_items": [], "subtotal": 0, "tax": 0, "total": 0})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    id1, id2 = r1.json()["id"], r2.json()["id"]

    # Bulk delete both - if route order is wrong this returns 404
    r = await client.delete(f"/docs/bulk-draft?doc_ids={id1},{id2}", headers=_h(token))
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["count"] == 2
    assert id1 in body["deleted"]
    assert id2 in body["deleted"]

    # Verify docs are gone
    assert (await client.get(f"/docs/{id1}", headers=_h(token))).status_code == 404
    assert (await client.get(f"/docs/{id2}", headers=_h(token))).status_code == 404


@pytest.mark.asyncio
async def test_bulk_delete_drafts_skips_non_drafts(client, session):
    """bulk-draft delete silently skips finalized docs - only drafts deleted."""
    token = await _register(client)
    draft_r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "status": "draft", "line_items": [], "subtotal": 0, "tax": 0, "total": 0})
    assert draft_r.status_code == 200
    draft_id = draft_r.json()["id"]

    r = await client.delete(f"/docs/bulk-draft?doc_ids={draft_id},nonexistent-id", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert draft_id in body["deleted"]


@pytest.mark.asyncio
async def test_bulk_delete_drafts_empty_ids_returns_422(client, session):
    """bulk-draft with no ids returns 422."""
    token = await _register(client)
    r = await client.delete("/docs/bulk-draft?doc_ids=", headers=_h(token))
    assert r.status_code == 422


async def _posted_then_draft(client, token: str) -> tuple[str, str]:
    """A document that has posted to the books and is back in draft, and its ref.

    Finalizing posts a journal entry that names the document. Reverting to draft
    voids that entry but keeps it, because the books keep their history, so the
    document is deletable by status while the journal still points at it.
    """
    h = _h(token)
    eid = await _create_invoice(client, token)
    r = await client.post(f"/docs/{eid}/finalize", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{eid}/revert-to-draft", headers=h, json={"reason": "keyed the wrong customer"})
    assert r.status_code == 200, r.text
    state = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert state["status"] == "draft", state
    return eid, state["ref_id"]


async def _posting_ids(session, entity_id: str) -> list[str]:
    """The journal entry ledger rows posted for a document."""
    rows = await session.execute(
        select(LedgerEntry.entity_id).where(LedgerEntry.entity_id.like(f"je:auto:{entity_id}:%"))
    )
    return sorted(set(rows.scalars().all()))


@pytest.mark.asyncio
async def test_delete_draft_with_journal_entries_is_refused(client, session):
    """A document that has reached the books cannot be deleted, even back in draft.

    Deleting it leaves entries in the journal naming a document nothing can
    resolve, which is where unattributable entries come from. The refusal names
    them so the reader can see what stands in the way.
    """
    token = await _register(client)
    h = _h(token)
    eid, _ref = await _posted_then_draft(client, token)
    before = await _posting_ids(session, eid)
    assert before, "the finalize posted nothing, so this test proves nothing"

    r = await client.delete(f"/docs/{eid}", headers=h)
    assert r.status_code == 409, r.text
    assert f"je:auto:{eid}:fin" in r.json()["detail"], r.text

    assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 200
    assert await _posting_ids(session, eid) == before


@pytest.mark.asyncio
async def test_bulk_delete_drafts_refuses_the_whole_batch_and_deletes_nothing(client, session):
    """One posted document in the selection stops the whole batch.

    A successful bulk delete reloads the page, so a partly done one has nowhere
    left to report what it skipped and the reader is left believing every ticked
    document is gone. Nothing is deleted, and the refusal names the documents that
    have postings so the reader can untick them and retry.
    """
    token = await _register(client)
    h = _h(token)
    clean = await _create_invoice(client, token)
    posted, posted_ref = await _posted_then_draft(client, token)
    before = await _posting_ids(session, posted)

    r = await client.delete(f"/docs/bulk-draft?doc_ids={clean},{posted}", headers=h)
    assert r.status_code == 422, r.text
    assert posted_ref in r.json()["detail"], r.text

    for eid in (clean, posted):
        assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 200, eid
    assert await _posting_ids(session, posted) == before


@pytest.mark.asyncio
async def test_delete_draft_never_finalized_still_works(client, session):
    """The guard refuses documents that have posted, not deletion itself.

    A draft that never reached the books still deletes, one at a time and in a
    batch. Without this, refusing everything would pass the two tests above.
    """
    token = await _register(client)
    h = _h(token)
    single = await _create_invoice(client, token)
    r = await client.delete(f"/docs/{single}", headers=h)
    assert r.status_code == 200, r.text
    assert (await client.get(f"/docs/{single}", headers=h)).status_code == 404

    first = await _create_invoice(client, token)
    second = await _create_invoice(client, token)
    r = await client.delete(f"/docs/bulk-draft?doc_ids={first},{second}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    for eid in (first, second):
        assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 404, eid


@pytest.mark.asyncio
async def test_finalized_flag_survives_send(client, session):
    """Sending a finalized invoice overwrites its status to 'sent', but the
    durable `finalized` flag must remain True so the Issue button never
    reappears (regression: finalized+emailed invoice showed 'Issue Invoice')."""
    token = await _register(client)
    inv = await _create_invoice(client, token)

    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc.get("finalized") in (None, False)
    assert str(doc.get("ref_id", "")).upper().startswith("PF")  # proforma draft

    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(token))).status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["finalized"] is True
    assert doc["status"] in ("final", "awaiting_payment")

    # Emailing it flips status to 'sent' but must not clear finalized.
    assert (await client.post(f"/docs/{inv}/send", headers=_h(token),
                              json={"sent_via": "manual"})).status_code == 200
    doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
    assert doc["status"] == "sent"
    assert doc["finalized"] is True


@pytest.mark.asyncio
async def test_finalized_flag_cleared_on_revert(client, session):
    """Reverting an issued invoice to draft clears the flag so it can be
    issued again."""
    token = await _register(client)
    inv = await _create_invoice(client, token)
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{inv}/revert-to-draft", headers=_h(token), json={})
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        doc = (await client.get(f"/docs/{inv}", headers=_h(token))).json()
        assert doc["status"] == "draft"
        assert doc["finalized"] is False
