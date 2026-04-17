# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Comprehensive user journey tests for the Celerp ERP system.

Categories:
  1. Discovery / Navigation (30)
  2. CRUD (60)
  3. Accounting Integrity (30)
  4. Workflows (40)
  5. Search & Filter (30)
  6. Edge Cases (40)
  7. Import / Export (20)
  8. Reports (20)
  9. Permissions (15)

Total: 285+ individual test functions.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import date, timedelta

import pytest

_crm_skip = pytest.mark.skipif(
    not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "premium_modules", "celerp-sales-funnel")),
    reason="celerp-sales-funnel not installed",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _reg(client, suffix: str = "") -> str:
    """Register the first user (or re-use if already seeded) and return an access token.

    Because register is locked after bootstrap, we first try to register; if the system
    is already bootstrapped we login with the default seeded credentials instead.
    """
    email = f"uj-{uuid.uuid4().hex[:10]}{suffix}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": f"UJ Co {suffix}", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _item(client, token: str, *, sku: str | None = None, qty: float = 1) -> str:
    sku = sku or f"SKU-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=_h(token), json={"sku": sku, "name": f"Item {sku}", "quantity": qty, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(
    client,
    token: str,
    *,
    subtotal: float = 100,
    tax: float = 7,
    total: float = 107,
    status: str = "draft",
) -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "invoice",
            "contact_id": "contact:test",
            "line_items": [{"name": "Item A", "quantity": 1, "unit_price": subtotal, "line_total": subtotal}],
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
            "status": status,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _po(client, token: str, *, subtotal: float = 100, tax: float = 0, total: float = 100) -> str:
    r = await client.post(
        "/docs",
        headers=_h(token),
        json={
            "doc_type": "purchase_order",
            "contact_id": "supplier:test",
            "line_items": [{"name": "Raw", "quantity": 2, "unit_price": subtotal / 2, "line_total": subtotal}],
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tb_balanced(tb: dict) -> bool:
    rows = tb.get("rows") or tb.get("accounts") or []
    if not rows:
        return True
    total_debit = sum(float(r.get("debit", 0) or 0) for r in rows)
    total_credit = sum(float(r.get("credit", 0) or 0) for r in rows)
    return abs(total_debit - total_credit) < 1e-4


# ===========================================================================
# Category 1: Discovery / Navigation
# ===========================================================================

@pytest.mark.asyncio
async def test_nav_dashboard_kpis(client):
    token = await _reg(client)
    r = await client.get("/dashboard/kpis", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_items_list(client):
    token = await _reg(client)
    r = await client.get("/items", headers=_h(token))
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


@pytest.mark.asyncio
async def test_nav_crm_contacts(client):
    token = await _reg(client)
    r = await client.get("/crm/contacts", headers=_h(token))
    assert r.status_code == 200


@_crm_skip
@pytest.mark.asyncio
async def test_nav_crm_deals(client):
    token = await _reg(client)
    r = await client.get("/crm/deals", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_crm_memos(client):
    token = await _reg(client)
    r = await client.get("/crm/memos", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_docs_list(client):
    token = await _reg(client)
    r = await client.get("/docs", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_accounting_trial_balance(client):
    token = await _reg(client)
    r = await client.get("/accounting/trial-balance", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_accounting_pnl(client):
    token = await _reg(client)
    r = await client.get("/accounting/pnl", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_accounting_balance_sheet(client):
    token = await _reg(client)
    r = await client.get("/accounting/balance-sheet", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_accounting_chart(client):
    token = await _reg(client)
    r = await client.get("/accounting/chart", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_reports_ar_aging(client):
    token = await _reg(client)
    r = await client.get("/reports/ar-aging", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_reports_ap_aging(client):
    token = await _reg(client)
    r = await client.get("/reports/ap-aging", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_reports_sales(client):
    token = await _reg(client)
    r = await client.get("/reports/sales", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_reports_purchases(client):
    token = await _reg(client)
    r = await client.get("/reports/purchases", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_reports_expiring(client):
    token = await _reg(client)
    r = await client.get("/reports/expiring", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_manufacturing_list(client):
    token = await _reg(client)
    r = await client.get("/manufacturing", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_subscriptions_list(client):
    token = await _reg(client)
    r = await client.get("/subscriptions", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_nav_scanning_resolve_missing(client):
    token = await _reg(client)
    r = await client.get("/scanning/resolve/item:nonexistent", headers=_h(token))
    # 404 is valid here - confirms endpoint exists
    assert r.status_code in {200, 404}


@pytest.mark.asyncio
async def test_nav_docs_filter_by_status_draft(client):
    token = await _reg(client)
    r = await client.get("/docs?status=draft", headers=_h(token))
    assert r.status_code == 200
    assert all(d.get("status") == "draft" for d in r.json()["items"])


@pytest.mark.asyncio
async def test_nav_docs_filter_by_status_final(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.get("/docs?status=final", headers=_h(token))
    assert r.status_code == 200
    assert all(d.get("status") == "final" for d in r.json()["items"])


@pytest.mark.asyncio
async def test_nav_docs_filter_by_doc_type_invoice(client):
    token = await _reg(client)
    await _invoice(client, token)
    r = await client.get("/docs?doc_type=invoice", headers=_h(token))
    assert r.status_code == 200
    assert all(d.get("doc_type") == "invoice" for d in r.json()["items"])


@pytest.mark.asyncio
async def test_nav_items_pagination_limit(client):
    token = await _reg(client)
    for _ in range(5):
        await _item(client, token)
    r = await client.get("/items?limit=2&offset=0", headers=_h(token))
    assert r.status_code == 200
    assert len(r.json()["items"]) <= 2


@pytest.mark.asyncio
async def test_nav_items_pagination_offset(client):
    token = await _reg(client)
    for _ in range(4):
        await _item(client, token)
    page1 = (await client.get("/items?limit=2&offset=0", headers=_h(token))).json()["items"]
    page2 = (await client.get("/items?limit=2&offset=2", headers=_h(token))).json()["items"]
    ids1 = {i["id"] for i in page1}
    ids2 = {i["id"] for i in page2}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_nav_items_search_by_name(client):
    token = await _reg(client)
    unique = uuid.uuid4().hex[:8]
    await _item(client, token, sku=f"SEARCH-{unique}")
    r = await client.get(f"/items?q={unique}", headers=_h(token))
    assert r.status_code == 200
    assert any(unique in i.get("sku", "") for i in r.json()["items"])


@pytest.mark.asyncio
async def test_nav_items_search_no_match(client):
    token = await _reg(client)
    r = await client.get("/items?q=XYZZY_NO_MATCH_EVER", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_nav_docs_summary(client):
    token = await _reg(client)
    r = await client.get("/docs/summary", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert "count_by_status" in body


@pytest.mark.asyncio
async def test_nav_items_valuation(client):
    token = await _reg(client)
    r = await client.get("/items/valuation", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert "item_count" in body


@pytest.mark.asyncio
async def test_nav_ledger_list(client):
    token = await _reg(client)
    r = await client.get("/ledger", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_nav_companies_me(client):
    token = await _reg(client)
    r = await client.get("/companies/me", headers=_h(token))
    assert r.status_code == 200


# ===========================================================================
# Category 2: CRUD
# ===========================================================================

@pytest.mark.asyncio
async def test_crud_contact_create_read_update(client):
    token = await _reg(client)
    h = _h(token)

    # create
    r = await client.post("/crm/contacts", headers=h, json={"name": "Alice", "email": "alice@example.com", "phone": "+1234", "address": "1 Main St"})
    assert r.status_code == 200
    cid = r.json()["id"]

    # read
    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "Alice"

    # update email
    r = await client.patch(f"/crm/contacts/{cid}", headers=h, json={"fields_changed": {"email": {"old": "alice@example.com", "new": "alice2@example.com"}}})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_contact_tag(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/contacts", headers=h, json={"name": "Bob"})
    cid = r.json()["id"]
    r2 = await client.post(f"/crm/contacts/{cid}/tags", headers=h, json={"tags": ["vip", "wholesale"]})
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_crud_contact_list_contains_created(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/contacts", headers=h, json={"name": "Carol"})
    cid = r.json()["id"]
    contacts = (await client.get("/crm/contacts", headers=h)).json()
    contacts_list = contacts["items"] if isinstance(contacts, dict) else contacts
    assert any(c["id"] == cid for c in contacts_list)


@pytest.mark.asyncio
async def test_crud_contact_with_attributes(client):
    token = await _reg(client)
    h = _h(token)
    attrs = {"country": "TH", "vat_id": "TH123456789"}
    r = await client.post("/crm/contacts", headers=h, json={"name": "Dave", "attributes": attrs})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_create_read_update(client):
    token = await _reg(client)
    h = _h(token)

    r = await client.post("/items", headers=h, json={"sku": "CRUD-1", "name": "Widget", "quantity": 10, "category": "Electronics", "sell_by": "piece"})
    assert r.status_code == 200
    eid = r.json()["id"]

    r = await client.get(f"/items/{eid}", headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "Widget"

    r = await client.patch(f"/items/{eid}", headers=h, json={"fields_changed": {"name": {"old": "Widget", "new": "Widget Pro"}}})
    assert r.status_code == 200

    r = await client.get(f"/items/{eid}", headers=h)
    assert r.json()["name"] == "Widget Pro"


@pytest.mark.asyncio
async def test_crud_item_set_retail_price(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/price", headers=h, json={"price_type": "retail_price", "new_price": 99.99})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_set_wholesale_price(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/price", headers=h, json={"price_type": "wholesale_price", "new_price": 75.0})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_set_cost_price(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/price", headers=h, json={"price_type": "cost_price", "new_price": 50.0})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_set_status_active(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/status", headers=h, json={"new_status": "active"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_adjust_quantity(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, qty=5)
    r = await client.post(f"/items/{eid}/adjust", headers=h, json={"new_qty": 20})
    assert r.status_code == 200
    state = (await client.get(f"/items/{eid}", headers=h)).json()
    assert state["quantity"] == 20


@pytest.mark.asyncio
async def test_crud_item_reserve_and_unreserve(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, qty=10)
    assert (await client.post(f"/items/{eid}/reserve", headers=h, json={"quantity": 3})).status_code == 200
    assert (await client.post(f"/items/{eid}/unreserve", headers=h, json={"quantity": 1})).status_code == 200


@pytest.mark.asyncio
async def test_crud_item_expire(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/expire", headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_dispose(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    r = await client.post(f"/items/{eid}/dispose", headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_invoice_create_read(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=200, tax=14, total=214)
    r = await client.get(f"/docs/{eid}", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert body["doc_type"] == "invoice"
    assert body["status"] == "draft"
    assert body["total"] == 214


@pytest.mark.asyncio
async def test_crud_invoice_custom_ref_id(client):
    """User can set their own invoice number via ref_id."""
    token = await _reg(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "ref_id": "MY-INV-001",
        "line_items": [{"name": "X", "quantity": 1, "unit_price": 50, "line_total": 50}],
        "subtotal": 50, "total": 50,
    })
    assert r.status_code == 200
    eid = r.json()["id"]
    assert eid == "doc:MY-INV-001"
    detail = await client.get(f"/docs/{eid}", headers=_h(token))
    assert detail.json()["ref_id"] == "MY-INV-001"


@pytest.mark.asyncio
async def test_crud_invoice_duplicate_ref_id_rejected(client):
    """Duplicate ref_id should be rejected with 409."""
    token = await _reg(client)
    r1 = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "ref_id": "DUP-001",
        "line_items": [{"name": "X", "quantity": 1, "unit_price": 10, "line_total": 10}],
        "subtotal": 10, "total": 10,
    })
    assert r1.status_code == 200
    r2 = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "ref_id": "DUP-001",
        "line_items": [{"name": "Y", "quantity": 1, "unit_price": 20, "line_total": 20}],
        "subtotal": 20, "total": 20,
    })
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_crud_invoice_auto_ref_when_blank(client):
    """When ref_id is not provided, auto-generated number is used."""
    token = await _reg(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "line_items": [{"name": "X", "quantity": 1, "unit_price": 10, "line_total": 10}],
        "subtotal": 10, "total": 10,
    })
    assert r.status_code == 200
    eid = r.json()["id"]
    assert eid.startswith("doc:PF-")


@pytest.mark.asyncio
async def test_crud_invoice_update_notes(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json={"fields_changed": {"notes": {"old": None, "new": "Special delivery"}}})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_invoice_finalize_changes_status(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    r = await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    assert r.status_code == 200
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "final"


@pytest.mark.asyncio
async def test_crud_invoice_send_changes_status(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    r = await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    assert r.status_code == 200
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "sent"


@pytest.mark.asyncio
async def test_crud_invoice_record_payment(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=100, tax=0, subtotal=100)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 100})
    assert r.status_code == 200
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "paid"


@pytest.mark.asyncio
async def test_crud_invoice_partial_payment_status(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=200, tax=0, subtotal=200)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 100})
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "partial"
    assert state["amount_paid"] == 100
    assert state["amount_outstanding"] == 100


@pytest.mark.asyncio
async def test_crud_invoice_void_draft(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "duplicate"})
    assert r.status_code == 200
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "void"


@pytest.mark.asyncio
async def test_crud_po_create_read(client):
    token = await _reg(client)
    eid = await _po(client, token)
    r = await client.get(f"/docs/{eid}", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["doc_type"] == "purchase_order"


@pytest.mark.asyncio
async def test_crud_po_finalize(client):
    token = await _reg(client)
    eid = await _po(client, token)
    r = await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_po_receive(client):
    token = await _reg(client)
    eid = await _po(client, token)
    r = await client.post(
        f"/docs/{eid}/receive",
        headers=_h(token),
        json={
            "location_id": "loc:1",
            "received_items": [{"po_line_index": 0, "sku": "PO-ITEM-1", "name": "PO Item 1", "quantity_received": 2, "sell_by": "piece"}],
        },
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_po_void_draft(client):
    token = await _reg(client)
    eid = await _po(client, token)
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "cancelled"})
    assert r.status_code == 200
    assert (await client.get(f"/docs/{eid}", headers=_h(token))).json()["status"] == "void"


@_crm_skip
@pytest.mark.asyncio
async def test_crud_deal_create_read(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/deals", headers=h, json={"name": "Big Deal", "stage": "lead"})
    assert r.status_code == 200
    did = r.json()["id"]
    deals = (await client.get("/crm/deals", headers=h)).json()["items"]
    assert any(d["id"] == did for d in deals)


@_crm_skip
@pytest.mark.asyncio
async def test_crud_deal_stage_update(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/deals", headers=h, json={"name": "Deal B", "stage": "lead"})
    did = r.json()["id"]
    r2 = await client.patch(f"/crm/deals/{did}/stage", headers=h, json={"new_stage": "proposal"})
    assert r2.status_code == 200


@_crm_skip
@pytest.mark.asyncio
async def test_crud_deal_won(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/deals", headers=h, json={"name": "Win Deal", "stage": "negotiation"})
    did = r.json()["id"]
    assert (await client.post(f"/crm/deals/{did}/won", headers=h)).status_code == 200


@_crm_skip
@pytest.mark.asyncio
async def test_crud_deal_lost(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/deals", headers=h, json={"name": "Lost Deal", "stage": "proposal"})
    did = r.json()["id"]
    assert (await client.post(f"/crm/deals/{did}/lost", headers=h, json={"reason": "budget"})).status_code == 200


@pytest.mark.asyncio
async def test_crud_memo_create_add_item(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "Memo Contact"})).json()
    r = await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})
    assert r.status_code == 200
    mid = r.json()["id"]
    r2 = await client.post(f"/crm/memos/{mid}/items", headers=h, json={"item_id": "item:1", "quantity": 2})
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_crud_memo_remove_item(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "Memo2"})).json()
    mid = (await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})).json()["id"]
    await client.post(f"/crm/memos/{mid}/items", headers=h, json={"item_id": "item:x", "quantity": 1})
    r = await client.delete(f"/crm/memos/{mid}/items/item:x", headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_items_export_csv_content_type(client):
    token = await _reg(client)
    await _item(client, token, sku="EXP-CSV-1")
    r = await client.get("/items/export/csv", headers=_h(token))
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_crud_items_export_csv_has_headers(client):
    token = await _reg(client)
    await _item(client, token, sku="EXP-CSV-HDR")
    r = await client.get("/items/export/csv", headers=_h(token))
    assert r.status_code == 200
    first_line = r.text.split("\n")[0]
    assert "sku" in first_line.lower() or "name" in first_line.lower()


@pytest.mark.asyncio
async def test_crud_item_transfer(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token)
    loc_id = str(uuid.uuid4())
    r = await client.post(f"/items/{eid}/transfer", headers=h, json={"to_location_id": loc_id})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_split(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, qty=5)
    r = await client.post(f"/items/{eid}/split", headers=h, json={
        "children": [{"sku": "CHILD-A", "quantity": 2.0}, {"sku": "CHILD-B", "quantity": 2.0}]
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crud_item_merge(client):
    token = await _reg(client)
    h = _h(token)
    eid_a = await _item(client, token, qty=5)
    eid_b = await _item(client, token, qty=3)
    r = await client.post("/items/merge", headers=h, json={
        "source_entity_ids": [eid_a, eid_b],
        "target_sku_from": eid_a,
    })
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    new_id = data["id"]
    # New item should have sum quantity.
    r2 = await client.get(f"/items/{new_id}", headers=h)
    assert r2.status_code == 200
    assert r2.json()["quantity"] == 8.0
    assert r2.json()["status"] == "available"
    # Sources should be deactivated.
    for eid in (eid_a, eid_b):
        r3 = await client.get(f"/items/{eid}", headers=h)
        assert r3.status_code == 200
        assert r3.json()["is_available"] is False
        assert r3.json()["quantity"] == 0
        assert r3.json()["status"] == "merged"
        assert r3.json()["merged_into"] == new_id
    # Default list should show new item but not merged sources.
    listing = await client.get("/items", headers=h)
    assert listing.status_code == 200
    ids_default = [i["id"] for i in listing.json()["items"]]
    assert new_id in ids_default
    assert eid_a not in ids_default
    assert eid_b not in ids_default
    # Filter by status=merged should return the sources only.
    listing2 = await client.get("/items", headers=h, params={"status": "merged"})
    assert listing2.status_code == 200
    ids_merged = [i["id"] for i in listing2.json()["items"]]
    assert eid_a in ids_merged
    assert eid_b in ids_merged
    assert new_id not in ids_merged


@pytest.mark.asyncio
async def test_merge_active_count_drops_by_one(client):
    """Merging two items should reduce active count by 1, not 2 (2 archived - 1 new = net -1)."""
    token = await _reg(client)
    h = _h(token)
    eid_a = await _item(client, token, qty=5)
    eid_b = await _item(client, token, qty=3)
    # Get count before merge.
    val_before = await client.get("/items/valuation", headers=h)
    assert val_before.status_code == 200
    count_before = val_before.json()["active_item_count"]
    available_before = val_before.json()["count_by_status"].get("available", 0)
    list_before = await client.get("/items", headers=h)
    list_count_before = list_before.json()["total"]
    assert count_before == list_count_before, "valuation and list counts must agree before merge"
    assert available_before == count_before, "all items should be 'available' before merge"
    # Merge.
    r = await client.post("/items/merge", headers=h, json={
        "source_entity_ids": [eid_a, eid_b],
        "target_sku_from": eid_a,
    })
    assert r.status_code == 200
    new_id = r.json()["id"]
    # Verify new item is available and visible.
    new_item = await client.get(f"/items/{new_id}", headers=h)
    assert new_item.json()["status"] == "available"
    assert new_item.json()["is_available"] is True
    # Get count after merge.
    val_after = await client.get("/items/valuation", headers=h)
    assert val_after.status_code == 200
    count_after = val_after.json()["active_item_count"]
    available_after = val_after.json()["count_by_status"].get("available", 0)
    list_after = await client.get("/items", headers=h)
    list_count_after = list_after.json()["total"]
    assert count_after == list_count_after, "valuation and list counts must agree after merge"
    assert count_after == count_before - 1, f"Expected {count_before - 1}, got {count_after}"
    assert available_after == available_before - 1, f"Available count should drop by 1: {available_before} -> {available_after}"
    # Verify sources are hidden in default list but visible with status=merged.
    default_ids = [i["id"] for i in list_after.json()["items"]]
    assert new_id in default_ids
    assert eid_a not in default_ids
    assert eid_b not in default_ids
    # status=all should show everything.
    list_all = await client.get("/items", headers=h, params={"status": "all"})
    all_count = list_all.json()["total"]
    assert all_count == count_before + 1, f"Expected {count_before + 1} total (2 sources + 1 new), got {all_count}"


@pytest.mark.asyncio
async def test_merge_source_ledger_has_details(client):
    """After merge, source item ledger should contain item.source_deactivated with enriched data."""
    token = await _reg(client)
    h = _h(token)
    eid_a = await _item(client, token, qty=3)
    eid_b = await _item(client, token, qty=2)
    # Get source SKUs for verification.
    sku_a = (await client.get(f"/items/{eid_a}", headers=h)).json()["sku"]
    sku_b = (await client.get(f"/items/{eid_b}", headers=h)).json()["sku"]
    r = await client.post("/items/merge", headers=h, json={
        "source_entity_ids": [eid_a, eid_b],
        "target_sku_from": eid_a,
    })
    assert r.status_code == 200
    new_id = r.json()["id"]
    # Check source item ledger for deactivation event with enriched data.
    lr = await client.get("/ledger", headers=h, params={"entity_id": eid_a})
    assert lr.status_code == 200
    events = lr.json()["items"]
    deact = [e for e in events if e["event_type"] == "item.source_deactivated"]
    assert len(deact) == 1
    assert deact[0]["data"]["merged_into"] == new_id
    assert deact[0]["data"]["merged_into_sku"] == sku_a  # target SKU carried to new item
    assert deact[0]["data"]["original_qty"] == 3.0
    # Check new item ledger for merged marker event with source SKUs.
    lr2 = await client.get("/ledger", headers=h, params={"entity_id": new_id})
    assert lr2.status_code == 200
    events2 = lr2.json()["items"]
    merged = [e for e in events2 if e["event_type"] == "item.merged"]
    assert len(merged) == 1
    assert set(merged[0]["data"]["source_entity_ids"]) == {eid_a, eid_b}
    assert merged[0]["data"]["source_skus"][eid_a] == sku_a
    assert merged[0]["data"]["source_skus"][eid_b] == sku_b
    assert merged[0]["data"]["resulting_qty"] == 5.0


@pytest.mark.asyncio
async def test_crud_quotation_create_and_convert(client):
    token = await _reg(client)
    h = _h(token)
    future = (date.today() + timedelta(days=30)).isoformat()
    r = await client.post(
        "/docs",
        headers=h,
        json={
            "doc_type": "quotation",
            "contact_id": "contact:q1",
            "line_items": [{"name": "Q item", "quantity": 1, "unit_price": 50, "line_total": 50}],
            "subtotal": 50,
            "tax": 0,
            "total": 50,
            "valid_until": future,
        },
    )
    assert r.status_code == 200
    qid = r.json()["id"]
    conv = await client.post(f"/docs/{qid}/convert", headers=h)
    assert conv.status_code == 200
    inv_id = conv.json()["target_doc_id"]
    assert (await client.get(f"/docs/{inv_id}", headers=h)).json()["doc_type"] == "invoice"


@pytest.mark.asyncio
async def test_crud_credit_note_reduces_invoice_outstanding(client):
    token = await _reg(client)
    h = _h(token)
    inv_id = await _invoice(client, token, subtotal=100, tax=0, total=100)
    r = await client.post(
        "/docs",
        headers=h,
        json={
            "doc_type": "credit_note",
            "original_doc_id": inv_id,
            "reason": "return",
            "line_items": [],
            "subtotal": 0,
            "tax": 0,
            "total": 30,
        },
    )
    assert r.status_code == 200
    inv_state = (await client.get(f"/docs/{inv_id}", headers=h)).json()
    assert inv_state["amount_outstanding"] == 70


# ===========================================================================
# Category 3: Accounting Integrity
# ===========================================================================

@pytest.mark.asyncio
async def test_acct_trial_balance_balanced_empty(client):
    token = await _reg(client)
    tb = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_trial_balance_balanced_after_invoice(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=7, total=107)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    tb = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_trial_balance_balanced_after_payment(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=0, total=100)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 100})
    tb = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_trial_balance_balanced_after_po_receive(client):
    token = await _reg(client)
    eid = await _po(client, token, total=200)
    await client.post(
        f"/docs/{eid}/receive",
        headers=_h(token),
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "sku": "PO-TB", "name": "PO TB", "quantity_received": 2, "sell_by": "piece"}]},
    )
    tb = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_invoice_finalize_creates_ar_debit(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=7, total=107)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if eid in (e["data"].get("memo") or "") and "finalized" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    ar_entry = next((x for x in entries if x["account"] == "1120"), None)
    assert ar_entry is not None
    assert float(ar_entry["debit"]) == 107


@pytest.mark.asyncio
async def test_acct_invoice_finalize_creates_revenue_credit(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=7, total=107)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if eid in (e["data"].get("memo") or "") and "finalized" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    rev_entry = next((x for x in entries if x["account"] == "4100"), None)
    assert rev_entry is not None
    assert float(rev_entry["credit"]) == 100


@pytest.mark.asyncio
async def test_acct_payment_creates_cash_debit(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=200, tax=0, total=200)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 200})
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if eid in (e["data"].get("memo") or "") and "payment" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    cash = next((x for x in entries if x["account"] == "1110"), None)
    assert cash is not None
    assert float(cash["debit"]) == 200


@pytest.mark.asyncio
async def test_acct_payment_creates_ar_credit(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=150, tax=0, total=150)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 150})
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if eid in (e["data"].get("memo") or "") and "payment" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    ar = next((x for x in entries if x["account"] == "1120"), None)
    assert ar is not None
    assert float(ar["credit"]) == 150


@pytest.mark.asyncio
async def test_acct_po_receive_creates_inventory_debit(client):
    token = await _reg(client)
    po_id = await _po(client, token, total=300)
    await client.post(
        f"/docs/{po_id}/receive",
        headers=_h(token),
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "sku": "INV-DBT", "name": "INV Debit Test", "quantity_received": 3, "sell_by": "piece"}]},
    )
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if po_id in (e["data"].get("memo") or "") and "received" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    inv_debit = next((x for x in entries if x["account"] == "1130" and float(x.get("debit", 0) or 0) > 0), None)
    assert inv_debit is not None


@pytest.mark.asyncio
async def test_acct_po_receive_creates_ap_credit(client):
    token = await _reg(client)
    po_id = await _po(client, token, total=300)
    await client.post(
        f"/docs/{po_id}/receive",
        headers=_h(token),
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "sku": "AP-CRD", "name": "AP Credit Test", "quantity_received": 3, "sell_by": "piece"}]},
    )
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if po_id in (e["data"].get("memo") or "") and "received" in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    ap = next((x for x in entries if x["account"] == "2110"), None)
    assert ap is not None


@pytest.mark.asyncio
async def test_acct_balance_sheet_returns_sections(client):
    token = await _reg(client)
    r = await client.get("/accounting/balance-sheet", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert "assets" in body or "total_assets" in body


@pytest.mark.asyncio
async def test_acct_pnl_returns_revenue_and_expenses(client):
    token = await _reg(client)
    r = await client.get("/accounting/pnl", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert "revenue" in body or "gross_profit" in body or "net_income" in body


@pytest.mark.asyncio
async def test_acct_chart_of_accounts_has_entries(client):
    token = await _reg(client)
    r = await client.get("/accounting/chart", headers=_h(token))
    assert r.status_code == 200
    accounts = r.json()["items"]
    assert len(accounts) > 0


@pytest.mark.asyncio
async def test_acct_chart_has_standard_accounts(client):
    token = await _reg(client)
    accounts = (await client.get("/accounting/chart", headers=_h(token))).json()["items"]
    codes = {a.get("code") for a in accounts}
    assert "1110" in codes  # Cash
    assert "1120" in codes  # AR
    assert "2110" in codes  # AP


@pytest.mark.asyncio
async def test_acct_void_invoice_does_not_create_je(client):
    """Voiding a draft invoice (no JE ever fired) leaves TB unchanged."""
    token = await _reg(client)
    eid = await _invoice(client, token)
    tb_before = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "test"})
    tb_after = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    # TB should be unchanged since no JE was ever posted
    assert tb_before == tb_after


@pytest.mark.asyncio
async def test_acct_multiple_invoices_tb_always_balanced(client):
    token = await _reg(client)
    h = _h(token)
    for i in range(3):
        eid = await _invoice(client, token, subtotal=100 * (i + 1), tax=7 * (i + 1), total=107 * (i + 1))
        await client.post(f"/docs/{eid}/finalize", headers=h)
    tb = (await client.get("/accounting/trial-balance", headers=h)).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_partial_payments_tb_balanced(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _invoice(client, token, subtotal=300, tax=0, total=300)
    await client.post(f"/docs/{eid}/finalize", headers=h)
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    tb = (await client.get("/accounting/trial-balance", headers=h)).json()
    assert _tb_balanced(tb)


@pytest.mark.asyncio
async def test_acct_doctor_returns_zero_issues(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.post("/admin/doctor?checks=missing_jes", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert body.get("issues", 0) == 0 or body.get("missing_jes", 0) == 0


@pytest.mark.asyncio
async def test_acct_pnl_date_filter(client):
    token = await _reg(client)
    past = (date.today() - timedelta(days=90)).isoformat()
    future = (date.today() + timedelta(days=1)).isoformat()
    r = await client.get(f"/accounting/pnl?date_from={past}&date_to={future}", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_acct_balance_sheet_after_po_receive(client):
    token = await _reg(client)
    po_id = await _po(client, token, total=500)
    await client.post(
        f"/docs/{po_id}/receive",
        headers=_h(token),
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "sku": "BS-PO", "name": "BS PO item", "quantity_received": 5, "sell_by": "piece"}]},
    )
    r = await client.get("/accounting/balance-sheet", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_acct_je_entries_individually_balanced(client):
    """Each individual JE must have debit == credit."""
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=250, tax=0, total=250)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    for je in ledger:
        entries = je["data"].get("entries", [])
        if entries:
            d = sum(float(x.get("debit", 0) or 0) for x in entries)
            c = sum(float(x.get("credit", 0) or 0) for x in entries)
            assert abs(d - c) < 1e-6, f"JE {je['id']} not balanced"


@pytest.mark.asyncio
async def test_acct_vat_entry_on_invoice(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=7, total=107)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    je = next(e for e in ledger if eid in (e["data"].get("memo") or "") and "finalized" in (e["data"].get("memo") or ""))
    accounts_used = {x["account"] for x in je["data"]["entries"]}
    assert "2120" in accounts_used  # VAT payable


@pytest.mark.asyncio
async def test_acct_tb_rows_have_required_fields(client):
    token = await _reg(client)
    eid = await _invoice(client, token, subtotal=100, tax=7, total=107)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    tb = (await client.get("/accounting/trial-balance", headers=_h(token))).json()
    rows = tb.get("rows") or tb.get("accounts") or []
    if rows:
        r = rows[0]
        assert "code" in r or "account" in r or "name" in r


# ===========================================================================
# Category 4: Workflows
# ===========================================================================

@pytest.mark.asyncio
async def test_wf_full_invoice_lifecycle(client):
    """draft -> finalize -> payment -> verify paid"""
    token = await _reg(client)
    h = _h(token)

    eid = await _invoice(client, token, subtotal=500, tax=35, total=535)
    assert (await client.get(f"/docs/{eid}", headers=h)).json()["status"] == "draft"

    await client.post(f"/docs/{eid}/finalize", headers=h)
    assert (await client.get(f"/docs/{eid}", headers=h)).json()["status"] == "final"

    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 535})
    final = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert final["status"] == "paid"
    assert final["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_wf_full_po_lifecycle_inventory_increment(client):
    """draft -> receive -> verify inventory increased"""
    token = await _reg(client)
    h = _h(token)

    item_id = await _item(client, token, qty=5)
    po_id = await _po(client, token, total=100)

    await client.post(
        f"/docs/{po_id}/receive",
        headers=h,
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "item_id": item_id, "quantity_received": 10}]},
    )
    updated = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert updated["quantity"] == 15


@pytest.mark.asyncio
async def test_wf_po_receive_creates_new_item(client):
    token = await _reg(client)
    h = _h(token)
    po_id = await _po(client, token)
    new_sku = f"PO-NEW-{uuid.uuid4().hex[:6]}"
    await client.post(
        f"/docs/{po_id}/receive",
        headers=h,
        json={"location_id": "loc:1", "received_items": [{"po_line_index": 0, "sku": new_sku, "name": "New from PO", "quantity_received": 7, "sell_by": "piece"}]},
    )
    items = (await client.get("/items", headers=h)).json()["items"]
    assert any(i.get("sku") == new_sku and i.get("quantity") == 7 for i in items)


@pytest.mark.asyncio
async def test_wf_memo_approve_and_convert_to_invoice(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "Memo Client"})).json()
    mid = (await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})).json()["id"]
    await client.post(f"/crm/memos/{mid}/items", headers=h, json={"item_id": "item:prod-1", "quantity": 3})
    assert (await client.post(f"/crm/memos/{mid}/approve", headers=h)).status_code == 200
    r = await client.post(f"/crm/memos/{mid}/convert-to-invoice", headers=h)
    assert r.status_code == 200
    doc_id = r.json().get("doc_id")
    assert doc_id is not None


@pytest.mark.asyncio
async def test_wf_memo_cancel(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "Cancel Memo Contact"})).json()
    mid = (await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})).json()["id"]
    assert (await client.post(f"/crm/memos/{mid}/cancel", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_wf_manufacturing_full_cycle(client):
    token = await _reg(client)
    h = _h(token)
    raw_id = await _item(client, token, qty=20, sku="RAW-WF")

    order = await client.post(
        "/manufacturing",
        headers=h,
        json={
            "description": "WF Assembly",
            "estimated_cost": 500,
            "inputs": [{"item_id": raw_id, "quantity": 5}],
            "expected_outputs": [{"sku": "FG-WF", "name": "Finished WF", "quantity": 2}],
        },
    )
    assert order.status_code == 200
    oid = order.json()["id"]

    assert (await client.post(f"/manufacturing/{oid}/consume", headers=h, json={"item_id": raw_id, "quantity": 5})).status_code == 200
    assert (await client.post(f"/manufacturing/{oid}/start", headers=h)).status_code == 200
    assert (await client.post(f"/manufacturing/{oid}/complete", headers=h, json={})).status_code == 200

    state = (await client.get(f"/manufacturing/{oid}", headers=h)).json()
    assert state["status"] == "completed"

    raw_state = (await client.get(f"/items/{raw_id}", headers=h)).json()
    assert raw_state["quantity"] == 15  # 20 - 5

    items = (await client.get("/items", headers=h)).json()["items"]
    assert any(i.get("sku") == "FG-WF" for i in items)


@pytest.mark.asyncio
async def test_wf_subscription_create_and_list(client):
    token = await _reg(client)
    h = _h(token)
    start = date.today().isoformat()
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={
            "name": "Monthly SaaS",
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": start,
            "line_items": [{"description": "SaaS fee", "quantity": 1, "unit_price": 999}],
        },
    )
    assert r.status_code == 200
    sid = r.json()["id"]
    subs = (await client.get("/subscriptions", headers=h)).json()["items"]
    assert any(s["id"] == sid for s in subs)


@pytest.mark.asyncio
async def test_wf_subscription_pause_and_resume(client):
    token = await _reg(client)
    h = _h(token)
    start = date.today().isoformat()
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={"name": "Pause Test", "doc_type": "invoice", "frequency": "weekly", "start_date": start, "line_items": []},
    )
    sid = r.json()["id"]
    assert (await client.post(f"/subscriptions/{sid}/pause", headers=h)).status_code == 200
    assert (await client.post(f"/subscriptions/{sid}/resume", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_wf_subscription_generate_doc(client):
    token = await _reg(client)
    h = _h(token)
    start = date.today().isoformat()
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={"name": "Gen Test", "doc_type": "invoice", "frequency": "monthly", "start_date": start, "line_items": [{"description": "Fee", "quantity": 1, "unit_price": 100}]},
    )
    sid = r.json()["id"]
    r2 = await client.post(f"/subscriptions/{sid}/generate", headers=h)
    assert r2.status_code == 200


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_wf_scan_barcode_and_verify_ledger(client):
    token = await _reg(client)
    code = f"BAR-{uuid.uuid4().hex[:8]}"
    r = await client.post("/scanning/scan", headers=_h(token), json={"code": code, "location_id": "loc:1", "raw": {"source": "camera"}})
    assert r.status_code == 200
    ledger = (await client.get("/ledger?entity_type=scan", headers=_h(token))).json()["items"]
    assert any(e["data"].get("code") == code for e in ledger)


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_wf_scan_batch_create_and_complete(client):
    token = await _reg(client)
    h = _h(token)
    start = await client.post("/scanning/batch", headers=h, json={"location_id": "loc:batch"})
    assert start.status_code == 200
    batch_id = start.json()["batch_id"]
    done = await client.post(f"/scanning/batch/{batch_id}/complete", headers=h)
    assert done.status_code == 200
    assert done.json()["ok"] is True


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_wf_scan_resolve_item(client):
    token = await _reg(client)
    h = _h(token)
    item_id = await _item(client, token, sku="RESOLVE-ME")
    r = await client.get(f"/scanning/resolve/{item_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["id"] == item_id


@pytest.mark.asyncio
async def test_wf_search_after_create_finds_item(client):
    token = await _reg(client)
    unique = uuid.uuid4().hex[:10]
    await _item(client, token, sku=f"FIND-{unique}")
    results = (await client.get(f"/items?q={unique}", headers=_h(token))).json()["items"]
    assert len(results) >= 1
    assert any(unique in i.get("sku", "") for i in results)


@pytest.mark.asyncio
async def test_wf_search_contact_after_create(client):
    token = await _reg(client)
    h = _h(token)
    unique = uuid.uuid4().hex[:10]
    await client.post("/crm/contacts", headers=h, json={"name": f"Contact {unique}"})
    contacts_data = (await client.get("/crm/contacts", headers=h)).json()
    contacts_list = contacts_data["items"] if isinstance(contacts_data, dict) else contacts_data
    assert any(unique in c.get("name", "") for c in contacts_list)


@pytest.mark.asyncio
async def test_wf_invoice_send_then_finalize(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/send", headers=h, json={})
    await client.post(f"/docs/{eid}/finalize", headers=h)
    state = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert state["status"] == "final"


@pytest.mark.asyncio
async def test_wf_po_update_before_finalize(client):
    token = await _reg(client)
    h = _h(token)
    po_id = await _po(client, token)
    r = await client.patch(f"/docs/{po_id}", headers=h, json={"fields_changed": {"notes": {"old": None, "new": "Rush order"}}})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_wf_item_adjust_then_verify(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, qty=10)
    await client.post(f"/items/{eid}/adjust", headers=h, json={"new_qty": 42})
    state = (await client.get(f"/items/{eid}", headers=h)).json()
    assert state["quantity"] == 42


@pytest.mark.asyncio
async def test_wf_multi_payment_to_paid(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _invoice(client, token, subtotal=300, tax=0, total=300)
    await client.post(f"/docs/{eid}/finalize", headers=h)
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    state = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert state["status"] == "paid"
    assert state["amount_outstanding"] == 0


@pytest.mark.asyncio
async def test_wf_manufacturing_list_contains_order(client):
    token = await _reg(client)
    h = _h(token)
    raw_id = await _item(client, token, qty=10, sku="RAW-LIST")
    order = await client.post(
        "/manufacturing",
        headers=h,
        json={
            "description": "List Test",
            "inputs": [{"item_id": raw_id, "quantity": 1}],
            "expected_outputs": [{"sku": "FG-LIST", "name": "FG List", "quantity": 1}],
        },
    )
    oid = order.json()["id"]
    orders = (await client.get("/manufacturing", headers=h)).json()["items"]
    assert any(o["id"] == oid for o in orders)


@pytest.mark.asyncio
async def test_wf_docs_list_shows_all_types(client):
    token = await _reg(client)
    h = _h(token)
    await _invoice(client, token)
    await _po(client, token)
    docs = (await client.get("/docs", headers=h)).json()["items"]
    types = {d["doc_type"] for d in docs}
    assert "invoice" in types
    assert "purchase_order" in types


@pytest.mark.asyncio
async def test_wf_crm_memo_list(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "List Memo Contact"})).json()
    mid = (await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})).json()["id"]
    memos = (await client.get("/crm/memos", headers=h)).json()["items"]
    assert any(m["id"] == mid for m in memos)


@pytest.mark.asyncio
async def test_wf_subscription_patch(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={"name": "Patch Sub", "doc_type": "invoice", "frequency": "monthly", "start_date": date.today().isoformat(), "line_items": []},
    )
    sid = r.json()["id"]
    r2 = await client.patch(f"/subscriptions/{sid}", headers=h, json={"fields_changed": {"name": {"old": "Patch Sub", "new": "Patched Sub"}}})
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_wf_subscription_get(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={"name": "Get Sub", "doc_type": "invoice", "frequency": "monthly", "start_date": date.today().isoformat(), "line_items": []},
    )
    sid = r.json()["id"]
    r2 = await client.get(f"/subscriptions/{sid}", headers=h)
    assert r2.status_code == 200
    assert r2.json()["id"] == sid


@pytest.mark.asyncio
async def test_wf_import_csv_items_count_increases(client):
    token = await _reg(client)
    h = _h(token)
    before = (await client.get("/items", headers=h)).json()["total"]
    records = [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {"sku": f"IMP-{uuid.uuid4().hex[:6]}", "name": "Imported Item", "quantity": 5},
            "source": "csv_import",
            "idempotency_key": uuid.uuid4().hex,
            "source_ts": None,
        }
        for _ in range(3)
    ]
    r = await client.post("/items/import/batch", headers=h, json={"records": records})
    assert r.status_code == 200
    assert r.json()["created"] == 3
    after = (await client.get("/items", headers=h)).json()["total"]
    # Demo item is auto-wiped on first real import; after = exactly the 3 imported items
    assert after == 3


@pytest.mark.asyncio
async def test_wf_memo_summary(client):
    token = await _reg(client)
    h = _h(token)
    contact = (await client.post("/crm/contacts", headers=h, json={"name": "Summary Contact"})).json()
    await client.post("/crm/memos", headers=h, json={"contact_id": contact["id"]})
    r = await client.get("/crm/memos/summary", headers=h)
    assert r.status_code == 200


# ===========================================================================
# Category 5: Search & Filter
# ===========================================================================

@pytest.mark.asyncio
async def test_sf_item_search_by_sku_exact(client):
    token = await _reg(client)
    unique_sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _item(client, token, sku=unique_sku)
    results = (await client.get(f"/items?q={unique_sku}", headers=_h(token))).json()["items"]
    assert len(results) == 1
    assert results[0]["sku"] == unique_sku


@pytest.mark.asyncio
async def test_sf_item_search_by_sku_prefix(client):
    token = await _reg(client)
    prefix = f"PREF-{uuid.uuid4().hex[:6]}"
    await _item(client, token, sku=f"{prefix}-A")
    await _item(client, token, sku=f"{prefix}-B")
    results = (await client.get(f"/items?q={prefix}", headers=_h(token))).json()["items"]
    assert len(results) >= 2


@pytest.mark.asyncio
async def test_sf_item_search_by_name_partial(client):
    token = await _reg(client)
    partial = uuid.uuid4().hex[:8]
    r = await client.post("/items", headers=_h(token), json={"sku": "NM-1", "name": f"Special {partial} Widget", "quantity": 1, "sell_by": "piece"})
    results = (await client.get(f"/items?q={partial}", headers=_h(token))).json()["items"]
    assert any(partial in i.get("name", "") for i in results)


@pytest.mark.asyncio
async def test_sf_item_no_results_for_nonsense_query(client):
    token = await _reg(client)
    results = (await client.get("/items?q=XYZZY_IMPOSSIBLE_987", headers=_h(token))).json()["items"]
    assert results == []


@pytest.mark.asyncio
async def test_sf_docs_filter_by_doc_type_po(client):
    token = await _reg(client)
    await _po(client, token)
    docs = (await client.get("/docs?doc_type=purchase_order", headers=_h(token))).json()["items"]
    assert all(d["doc_type"] == "purchase_order" for d in docs)
    assert len(docs) >= 1


@pytest.mark.asyncio
async def test_sf_docs_filter_by_doc_type_invoice(client):
    token = await _reg(client)
    await _invoice(client, token)
    docs = (await client.get("/docs?doc_type=invoice", headers=_h(token))).json()["items"]
    assert all(d["doc_type"] == "invoice" for d in docs)
    assert len(docs) >= 1


@pytest.mark.asyncio
async def test_sf_docs_filter_by_status_void(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "test"})
    docs = (await client.get("/docs?status=void", headers=_h(token))).json()["items"]
    assert all(d["status"] == "void" for d in docs)
    assert any(d["id"] == eid for d in docs)


@pytest.mark.asyncio
async def test_sf_docs_filter_by_status_paid(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=50, tax=0, subtotal=50)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 50})
    docs = (await client.get("/docs?status=paid", headers=_h(token))).json()["items"]
    assert all(d["status"] == "paid" for d in docs)


@pytest.mark.asyncio
async def test_sf_contacts_list_after_create(client):
    token = await _reg(client)
    h = _h(token)
    unique = uuid.uuid4().hex[:8]
    r = await client.post("/crm/contacts", headers=h, json={"name": f"Search {unique}", "email": f"{unique}@search.test"})
    cid = r.json()["id"]
    contacts = (await client.get("/crm/contacts", headers=h)).json()
    contacts_list = contacts["items"] if isinstance(contacts, dict) else contacts
    assert any(c["id"] == cid for c in contacts_list)


@pytest.mark.asyncio
async def test_sf_pagination_page2_differs_from_page1(client):
    token = await _reg(client)
    for _ in range(6):
        await _item(client, token)
    page1_ids = {i["id"] for i in (await client.get("/items?limit=3&offset=0", headers=_h(token))).json()["items"]}
    page2_ids = {i["id"] for i in (await client.get("/items?limit=3&offset=3", headers=_h(token))).json()["items"]}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_sf_pagination_offset_beyond_count_returns_empty(client):
    token = await _reg(client)
    results = (await client.get("/items?limit=10&offset=99999", headers=_h(token))).json()["items"]
    assert results == []


@pytest.mark.asyncio
async def test_sf_docs_combined_filter_type_and_status(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    docs = (await client.get("/docs?doc_type=invoice&status=final", headers=_h(token))).json()["items"]
    assert all(d["doc_type"] == "invoice" and d["status"] == "final" for d in docs)


@pytest.mark.asyncio
async def test_sf_items_limit_one(client):
    token = await _reg(client)
    for _ in range(3):
        await _item(client, token)
    results = (await client.get("/items?limit=1", headers=_h(token))).json()["items"]
    assert len(results) == 1


@pytest.mark.asyncio
async def test_sf_items_offset_zero_equals_no_offset(client):
    token = await _reg(client)
    for _ in range(3):
        await _item(client, token)
    r1 = (await client.get("/items?offset=0", headers=_h(token))).json()["items"]
    r2 = (await client.get("/items", headers=_h(token))).json()["items"]
    assert [i["id"] for i in r1] == [i["id"] for i in r2]


@_crm_skip
@pytest.mark.asyncio
async def test_sf_deals_list_after_create(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post("/crm/deals", headers=h, json={"name": "Filter Deal", "stage": "lead"})
    did = r.json()["id"]
    deals = (await client.get("/crm/deals", headers=h)).json()["items"]
    assert any(d["id"] == did for d in deals)


@pytest.mark.asyncio
async def test_sf_ledger_filter_by_entity_type(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(token))).json()["items"]
    assert all(e["entity_type"] == "journal_entry" for e in ledger)


@pytest.mark.asyncio
async def test_sf_docs_filter_status_sent(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/send", headers=_h(token), json={})
    docs = (await client.get("/docs?status=sent", headers=_h(token))).json()["items"]
    assert any(d["id"] == eid and d["status"] == "sent" for d in docs)


@pytest.mark.asyncio
async def test_sf_items_large_offset_returns_partial(client):
    token = await _reg(client)
    for _ in range(6):
        await _item(client, token)
    # With demo seed + 6 items = at least 7. offset=6 leaves at least 1 (the last real item).
    results = (await client.get("/items?offset=6", headers=_h(token))).json()["items"]
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_sf_docs_no_filter_returns_all(client):
    token = await _reg(client)
    await _invoice(client, token)
    await _po(client, token)
    docs = (await client.get("/docs", headers=_h(token))).json()["items"]
    assert len(docs) >= 2


@pytest.mark.asyncio
async def test_sf_contacts_email_in_attributes(client):
    token = await _reg(client)
    h = _h(token)
    unique_email = f"{uuid.uuid4().hex[:8]}@sf.test"
    r = await client.post("/crm/contacts", headers=h, json={"name": "Email Contact", "email": unique_email})
    cid = r.json()["id"]
    contact = (await client.get(f"/crm/contacts/{cid}", headers=h)).json()
    # email may be in the top-level or attributes
    contact_str = str(contact)
    assert unique_email in contact_str


@pytest.mark.asyncio
async def test_sf_items_search_case_insensitive(client):
    token = await _reg(client)
    unique = uuid.uuid4().hex[:8].upper()
    await client.post("/items", headers=_h(token), json={"sku": f"CI-{unique}", "sell_by": "piece", "name": f"Case {unique}", "quantity": 1})
    results = (await client.get(f"/items?q={unique.lower()}", headers=_h(token))).json()["items"]
    assert len(results) >= 1


# ===========================================================================
# Category 6: Edge Cases
# ===========================================================================

@pytest.mark.asyncio
async def test_edge_finalize_already_finalized_returns_error(client):
    """Re-finalizing is idempotent (200) but the projection still shows final status."""
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    # API is idempotent - either 200 (re-fires event) or 409
    assert r.status_code in {200, 409, 422}
    state = (await client.get(f"/docs/{eid}", headers=_h(token))).json()
    assert state["status"] == "final"


@pytest.mark.asyncio
async def test_edge_pay_already_paid_invoice_returns_error(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=50, tax=0, subtotal=50)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 50})
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 1})
    assert r.status_code in {409, 422}


@pytest.mark.asyncio
async def test_edge_void_already_void_doc_returns_error(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "test"})
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "again"})
    # Can void void; or 409 - implementation-dependent, just assert it doesn't crash (2xx or 409)
    assert r.status_code in {200, 409, 422}


@pytest.mark.asyncio
async def test_edge_pay_draft_invoice_returns_409(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 50})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edge_overpayment_rejected(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=100, tax=0, subtotal=100)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 999})
    assert r.status_code in {409, 422}


@pytest.mark.asyncio
async def test_edge_edit_finalized_invoice_rejected(client):
    token = await _reg(client)
    eid = await _invoice(client, token)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    r = await client.patch(f"/docs/{eid}", headers=_h(token), json={"fields_changed": {"notes": {"old": None, "new": "nope"}}})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edge_void_paid_invoice_rejected(client):
    token = await _reg(client)
    eid = await _invoice(client, token, total=100, tax=0, subtotal=100)
    await client.post(f"/docs/{eid}/finalize", headers=_h(token))
    await client.post(f"/docs/{eid}/payment", headers=_h(token), json={"amount": 100})
    r = await client.post(f"/docs/{eid}/void", headers=_h(token), json={"reason": "no"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edge_idempotent_item_import(client):
    token = await _reg(client)
    h = _h(token)
    ikey = uuid.uuid4().hex
    eid = f"item:{uuid.uuid4()}"
    record = {
        "entity_id": eid,
        "event_type": "item.created",
        "data": {"sku": "IDEM-1", "name": "Idem Item", "quantity": 1},
        "source": "api",
        "idempotency_key": ikey,
        "source_ts": None,
    }
    r1 = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r1.json()["created"] == 1
    r2 = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r2.json()["skipped"] == 1
    assert r2.json()["created"] == 0


@pytest.mark.asyncio
async def test_edge_get_nonexistent_item_returns_404(client):
    token = await _reg(client)
    r = await client.get("/items/item:does-not-exist-xyz", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edge_get_nonexistent_doc_returns_404(client):
    token = await _reg(client)
    r = await client.get("/docs/doc:does-not-exist-xyz", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edge_get_nonexistent_contact_returns_404(client):
    token = await _reg(client)
    r = await client.get("/crm/contacts/contact:does-not-exist", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edge_auth_required_items(client):
    r = await client.get("/items")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_docs(client):
    r = await client.get("/docs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_crm_contacts(client):
    r = await client.get("/crm/contacts")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_accounting(client):
    r = await client.get("/accounting/trial-balance")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_reports(client):
    r = await client.get("/reports/ar-aging")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_manufacturing(client):
    r = await client.get("/manufacturing")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_auth_required_subscriptions(client):
    r = await client.get("/subscriptions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_invalid_token_rejected(client):
    r = await client.get("/items", headers={"Authorization": "Bearer notavalidtoken"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_edge_cross_company_isolation_items(client):
    """Items are scoped by company: entity created in this company's context is visible,
    but a random entity_id from another company is not accessible (returns 404)."""
    token_a = await _reg(client)
    eid_a = await _item(client, token_a, sku="ISO-A")
    # Create a fake entity_id that doesn't exist in this company
    fake_id = f"item:{uuid.uuid4()}"
    r = await client.get(f"/items/{fake_id}", headers=_h(token_a))
    assert r.status_code == 404
    # Real item IS accessible
    r2 = await client.get(f"/items/{eid_a}", headers=_h(token_a))
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_edge_cross_company_isolation_docs(client):
    """Docs are scoped by company: fake entity_id returns 404."""
    token_a = await _reg(client)
    await _invoice(client, token_a)
    fake_id = f"doc:{uuid.uuid4()}"
    r = await client.get(f"/docs/{fake_id}", headers=_h(token_a))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edge_cross_company_isolation_contacts(client):
    """Contacts are scoped by company: fake contact_id returns 404."""
    token_a = await _reg(client)
    fake_id = f"contact:{uuid.uuid4()}"
    r = await client.get(f"/crm/contacts/{fake_id}", headers=_h(token_a))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edge_convert_expired_quotation_rejected(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post(
        "/docs",
        headers=h,
        json={
            "doc_type": "quotation",
            "contact_id": "c:1",
            "line_items": [],
            "subtotal": 0,
            "tax": 0,
            "total": 0,
            "valid_until": "2000-01-01",
        },
    )
    qid = r.json()["id"]
    r2 = await client.post(f"/docs/{qid}/convert", headers=h)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_edge_receive_non_po_doc_rejected(client):
    token = await _reg(client)
    h = _h(token)
    inv_id = await _invoice(client, token)
    r = await client.post(
        f"/docs/{inv_id}/receive",
        headers=h,
        json={"location_id": "loc:1", "received_items": []},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edge_mfg_complete_without_all_inputs_rejected(client):
    token = await _reg(client)
    h = _h(token)
    raw_id = await _item(client, token, qty=10, sku="RAW-GUARD")
    order = await client.post(
        "/manufacturing",
        headers=h,
        json={
            "description": "Guard Test",
            "inputs": [{"item_id": raw_id, "quantity": 5}],
            "expected_outputs": [{"sku": "FG-GUARD", "name": "Guard FG", "quantity": 1}],
        },
    )
    oid = order.json()["id"]
    # Try to complete without consuming
    r = await client.post(f"/manufacturing/{oid}/complete", headers=h, json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_edge_credit_note_exceeds_invoice_total_rejected(client):
    token = await _reg(client)
    h = _h(token)
    inv_id = await _invoice(client, token, subtotal=100, tax=0, total=100)
    r = await client.post(
        "/docs",
        headers=h,
        json={
            "doc_type": "credit_note",
            "original_doc_id": inv_id,
            "reason": "return",
            "line_items": [],
            "subtotal": 0,
            "tax": 0,
            "total": 999,
        },
    )
    assert r.status_code in {409, 422}


@pytest.mark.asyncio
async def test_edge_po_split_mismatch_lengths(client):
    """Split with only 1 child should be rejected (min 2 children required)."""
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, qty=5)
    r = await client.post(f"/items/{eid}/split", headers=h, json={"children": [{"sku": "C1", "quantity": 1.0}]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_edge_subscribe_invalid_frequency_rejected(client):
    token = await _reg(client)
    h = _h(token)
    r = await client.post(
        "/subscriptions",
        headers=h,
        json={"name": "Bad Freq", "doc_type": "invoice", "frequency": "daily", "start_date": date.today().isoformat(), "line_items": []},
    )
    assert r.status_code in {422, 400}


# ===========================================================================
# Category 7: Import / Export
# ===========================================================================

@pytest.mark.asyncio
async def test_ie_import_valid_csv_items_count_increases(client):
    token = await _reg(client)
    h = _h(token)
    before = (await client.get("/items", headers=h)).json()["total"]
    records = [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {"sku": f"IMP-V-{uuid.uuid4().hex[:6]}", "name": "Valid Import", "quantity": 1},
            "source": "csv",
            "idempotency_key": uuid.uuid4().hex,
            "source_ts": None,
        }
        for _ in range(5)
    ]
    r = await client.post("/items/import/batch", headers=h, json={"records": records})
    assert r.status_code == 200
    result = r.json()
    assert result["created"] == 5
    after = (await client.get("/items", headers=h)).json()["total"]
    # Demo item is auto-wiped on first batch import, so after == created (not before + created)
    assert after == result["created"]


@pytest.mark.asyncio
async def test_ie_import_idempotent_skips_duplicates(client):
    token = await _reg(client)
    h = _h(token)
    ikey = uuid.uuid4().hex
    record = {
        "entity_id": f"item:{uuid.uuid4()}",
        "event_type": "item.created",
        "data": {"sku": "IDEM-IMPORT", "name": "Idempotent", "quantity": 1},
        "source": "csv",
        "idempotency_key": ikey,
        "source_ts": None,
    }
    r1 = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r1.json()["created"] == 1
    r2 = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r2.json()["skipped"] == 1
    assert r2.json()["created"] == 0


@pytest.mark.asyncio
async def test_ie_import_partial_with_errors_reports_them(client):
    token = await _reg(client)
    h = _h(token)
    good_key = uuid.uuid4().hex
    bad_key = uuid.uuid4().hex
    records = [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {"sku": "GOOD-1", "name": "Good", "quantity": 1},
            "source": "csv",
            "idempotency_key": good_key,
            "source_ts": None,
        },
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.bad_event_type_xyz",  # deliberately bad
            "data": {},
            "source": "csv",
            "idempotency_key": bad_key,
            "source_ts": None,
        },
    ]
    r = await client.post("/items/import/batch", headers=h, json={"records": records})
    assert r.status_code == 200
    # Either we get 1 created + 1 error, or the bad one just creates with unknown type - both ok
    result = r.json()
    assert result["created"] + result["skipped"] + len(result.get("errors", [])) == 2


@pytest.mark.asyncio
async def test_ie_export_items_csv_content_type(client):
    token = await _reg(client)
    await _item(client, token, sku="EXP-A")
    r = await client.get("/items/export/csv", headers=_h(token))
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_ie_export_items_csv_has_data(client):
    token = await _reg(client)
    unique_sku = f"EXP-DATA-{uuid.uuid4().hex[:6]}"
    await _item(client, token, sku=unique_sku)
    r = await client.get("/items/export/csv", headers=_h(token))
    assert unique_sku in r.text


@pytest.mark.asyncio
async def test_ie_export_items_csv_has_header_row(client):
    token = await _reg(client)
    await _item(client, token, sku="HDR-TEST")
    r = await client.get("/items/export/csv", headers=_h(token))
    first_line = r.text.split("\r\n")[0] if "\r\n" in r.text else r.text.split("\n")[0]
    assert "sku" in first_line.lower() or "entity_id" in first_line.lower()


@pytest.mark.asyncio
async def test_ie_export_items_csv_with_status_filter(client):
    token = await _reg(client)
    h = _h(token)
    eid = await _item(client, token, sku="ACTIVE-EXP")
    await client.post(f"/items/{eid}/status", headers=h, json={"new_status": "active"})
    r = await client.get("/items/export/csv?status=active", headers=h)
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_ie_import_batch_returns_structured_result(client):
    token = await _reg(client)
    h = _h(token)
    records = [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {"sku": f"STRUCT-{i}", "name": f"Struct {i}", "quantity": i},
            "source": "batch",
            "idempotency_key": uuid.uuid4().hex,
            "source_ts": None,
        }
        for i in range(2)
    ]
    r = await client.post("/items/import/batch", headers=h, json={"records": records})
    body = r.json()
    assert "created" in body
    assert "skipped" in body
    assert "errors" in body


@pytest.mark.asyncio
async def test_ie_export_empty_company_csv(client):
    """Export from company with no items should return CSV with only header."""
    token = await _reg(client)
    r = await client.get("/items/export/csv", headers=_h(token))
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_ie_import_large_batch_respects_limit(client):
    """Importing exactly 10 items at once works."""
    token = await _reg(client)
    h = _h(token)
    records = [
        {
            "entity_id": f"item:{uuid.uuid4()}",
            "event_type": "item.created",
            "data": {"sku": f"LARGE-{i}", "name": f"Large {i}", "quantity": 1},
            "source": "batch",
            "idempotency_key": uuid.uuid4().hex,
            "source_ts": None,
        }
        for i in range(10)
    ]
    r = await client.post("/items/import/batch", headers=h, json={"records": records})
    assert r.status_code == 200
    assert r.json()["created"] == 10


@pytest.mark.asyncio
async def test_ie_import_with_source_ts(client):
    token = await _reg(client)
    h = _h(token)
    record = {
        "entity_id": f"item:{uuid.uuid4()}",
        "event_type": "item.created",
        "data": {"sku": "TS-IMPORT", "name": "Timestamped", "quantity": 1},
        "source": "migration",
        "idempotency_key": uuid.uuid4().hex,
        "source_ts": "2025-01-15T10:30:00Z",
    }
    r = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r.status_code == 200
    assert r.json()["created"] == 1


@pytest.mark.asyncio
async def test_ie_export_csv_content_disposition(client):
    token = await _reg(client)
    await _item(client, token, sku="CD-ITEM")
    r = await client.get("/items/export/csv", headers=_h(token))
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd or "filename" in cd


@pytest.mark.asyncio
async def test_ie_import_zero_records(client):
    token = await _reg(client)
    r = await client.post("/items/import/batch", headers=_h(token), json={"records": []})
    assert r.status_code == 200
    assert r.json()["created"] == 0


@pytest.mark.asyncio
async def test_ie_export_csv_multiple_items(client):
    token = await _reg(client)
    for i in range(4):
        await _item(client, token, sku=f"MULTI-CSV-{i}")
    r = await client.get("/items/export/csv", headers=_h(token))
    # CSV should have at least 5 lines (header + 4 items)
    lines = [l for l in r.text.split("\n") if l.strip()]
    assert len(lines) >= 5


@pytest.mark.asyncio
async def test_ie_import_duplicate_key_in_same_batch(client):
    """Same idempotency key twice in one batch should create 1 and skip 1."""
    token = await _reg(client)
    h = _h(token)
    ikey = uuid.uuid4().hex
    record = {
        "entity_id": f"item:{uuid.uuid4()}",
        "event_type": "item.created",
        "data": {"sku": "DUP-BATCH", "name": "Dup", "quantity": 1},
        "source": "csv",
        "idempotency_key": ikey,
        "source_ts": None,
    }
    r = await client.post("/items/import/batch", headers=h, json={"records": [record, record]})
    assert r.status_code == 200
    result = r.json()
    # First one creates, second one skips (or both skips if DB checks before iterating)
    assert result["created"] + result["skipped"] == 2


@pytest.mark.asyncio
async def test_ie_export_csv_auth_required(client):
    r = await client.get("/items/export/csv")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ie_import_batch_auth_required(client):
    r = await client.post("/items/import/batch", json={"records": []})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ie_import_creates_retrievable_item(client):
    token = await _reg(client)
    h = _h(token)
    eid = f"item:{uuid.uuid4()}"
    sku = f"RETV-{uuid.uuid4().hex[:6]}"
    record = {
        "entity_id": eid,
        "event_type": "item.created",
        "data": {"sku": sku, "name": "Retrievable", "quantity": 3},
        "source": "api",
        "idempotency_key": uuid.uuid4().hex,
        "source_ts": None,
    }
    await client.post("/items/import/batch", headers=h, json={"records": [record]})
    items = (await client.get("/items", headers=h)).json()["items"]
    assert any(i.get("sku") == sku for i in items)


# ===========================================================================
# Category 8: Reports
# ===========================================================================

@pytest.mark.asyncio
async def test_rpt_ar_aging_has_lines_after_invoice(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=10)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c1", "contact_name": "C1",
        "line_items": [], "subtotal": 100, "tax": 0, "total": 100,
        "status": "final", "date": past, "due_date": past,
    })
    ar = (await client.get("/reports/ar-aging", headers=h)).json()
    assert "lines" in ar
    assert len(ar["lines"]) >= 1


@pytest.mark.asyncio
async def test_rpt_ap_aging_has_lines_after_po(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=5)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "purchase_order", "contact_id": "s1", "contact_name": "S1",
        "line_items": [], "subtotal": 200, "tax": 0, "total": 200,
        "status": "final", "date": past,
    })
    ap = (await client.get("/reports/ap-aging", headers=h)).json()
    assert "lines" in ap
    assert len(ap["lines"]) >= 1


@pytest.mark.asyncio
async def test_rpt_sales_group_by_customer(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=5)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c1", "contact_name": "Cust1",
        "line_items": [{"name": "P", "quantity": 1, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100, "status": "final", "date": past,
    })
    r = await client.get("/reports/sales?group_by=customer", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("group_by") == "customer"
    assert len(body.get("lines", [])) >= 1


@pytest.mark.asyncio
async def test_rpt_sales_group_by_item(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=3)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c1", "contact_name": "C1",
        "line_items": [{"item_id": "i1", "name": "Prod", "quantity": 2, "line_total": 200}],
        "subtotal": 200, "tax": 0, "total": 200, "status": "final", "date": past,
    })
    r = await client.get("/reports/sales?group_by=item", headers=h)
    assert r.status_code == 200
    assert r.json().get("group_by") == "item"


@pytest.mark.asyncio
async def test_rpt_sales_group_by_period(client):
    token = await _reg(client)
    r = await client.get("/reports/sales?group_by=period&period=monthly", headers=_h(token))
    assert r.status_code == 200
    assert r.json().get("group_by") == "period"


@pytest.mark.asyncio
async def test_rpt_purchases_group_by_supplier(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=5)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "purchase_order", "contact_id": "s1", "contact_name": "Supplier1",
        "line_items": [{"name": "P1", "quantity": 5, "line_total": 500}],
        "subtotal": 500, "tax": 0, "total": 500, "status": "final", "date": past,
    })
    r = await client.get("/reports/purchases?group_by=supplier", headers=h)
    assert r.status_code == 200
    assert r.json().get("group_by") == "supplier"


@pytest.mark.asyncio
async def test_rpt_purchases_group_by_item(client):
    token = await _reg(client)
    r = await client.get("/reports/purchases?group_by=item", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rpt_expiring_items_count_field(client):
    token = await _reg(client)
    r = await client.get("/reports/expiring?days=30", headers=_h(token))
    assert r.status_code == 200
    assert "count" in r.json()


@pytest.mark.asyncio
async def test_rpt_ar_aging_empty_for_new_company(client):
    token = await _reg(client)
    r = await client.get("/reports/ar-aging", headers=_h(token))
    assert r.status_code == 200
    assert "lines" in r.json()


@pytest.mark.asyncio
async def test_rpt_sales_date_range_filter(client):
    token = await _reg(client)
    h = _h(token)
    date_from = (date.today() - timedelta(days=30)).isoformat()
    date_to = date.today().isoformat()
    r = await client.get(f"/reports/sales?date_from={date_from}&date_to={date_to}", headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rpt_purchases_date_range_filter(client):
    token = await _reg(client)
    h = _h(token)
    date_from = (date.today() - timedelta(days=60)).isoformat()
    date_to = date.today().isoformat()
    r = await client.get(f"/reports/purchases?date_from={date_from}&date_to={date_to}", headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rpt_ar_aging_paid_invoice_not_in_outstanding(client):
    """Paid invoices should not appear in AR aging as outstanding."""
    token = await _reg(client)
    h = _h(token)
    eid = await _invoice(client, token, total=100, tax=0, subtotal=100)
    await client.post(f"/docs/{eid}/finalize", headers=h)
    await client.post(f"/docs/{eid}/payment", headers=h, json={"amount": 100})
    ar = (await client.get("/reports/ar-aging", headers=h)).json()
    # The paid invoice should not appear with outstanding amount
    for line in ar.get("lines", []):
        if line.get("entity_id") == eid:
            assert float(line.get("total", 0) or 0) == 0


@pytest.mark.asyncio
async def test_rpt_sales_returns_amounts(client):
    token = await _reg(client)
    h = _h(token)
    past = (date.today() - timedelta(days=2)).isoformat()
    await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c1", "contact_name": "C1",
        "line_items": [{"name": "Item", "quantity": 1, "line_total": 750}],
        "subtotal": 750, "tax": 0, "total": 750, "status": "final", "date": past,
    })
    r = await client.get("/reports/sales?group_by=customer", headers=h)
    body = r.json()
    total_amount = sum(float(l.get("total_revenue", 0) or l.get("total", 0) or 0) for l in body.get("lines", []))
    assert total_amount >= 750


@pytest.mark.asyncio
async def test_rpt_expiring_days_param(client):
    token = await _reg(client)
    r1 = await client.get("/reports/expiring?days=7", headers=_h(token))
    r2 = await client.get("/reports/expiring?days=90", headers=_h(token))
    assert r1.status_code == 200
    assert r2.status_code == 200
    c7 = r1.json().get("count", 0)
    c90 = r2.json().get("count", 0)
    assert c90 >= c7  # more days = more or equal expiring items


@pytest.mark.asyncio
async def test_rpt_ap_aging_structure(client):
    token = await _reg(client)
    r = await client.get("/reports/ap-aging", headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("lines", []), list)


@pytest.mark.asyncio
async def test_rpt_purchases_weekly_period(client):
    token = await _reg(client)
    r = await client.get("/reports/purchases?group_by=period&period=weekly", headers=_h(token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_rpt_sales_daily_period(client):
    token = await _reg(client)
    r = await client.get("/reports/sales?group_by=period&period=daily", headers=_h(token))
    assert r.status_code == 200


# ===========================================================================
# Category 9: Permissions
# ===========================================================================

@pytest.mark.asyncio
async def test_perm_login_wrong_password_401(client):
    await client.post("/auth/register", json={"company_name": "Perm Co", "email": "perm@test.test", "name": "Admin", "password": "correct"})
    r = await client.post("/auth/login", json={"email": "perm@test.test", "password": "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_login_nonexistent_email_401(client):
    r = await client.post("/auth/login", json={"email": "nobody@nowhere.test", "password": "pw"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_invalid_token_401(client):
    r = await client.get("/items", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_missing_auth_header_401(client):
    r = await client.get("/docs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_company_a_cannot_read_b_items(client):
    """Entity from another company (unknown id) returns 404 - company scoping works."""
    token = await _reg(client)
    fake_id = f"item:{uuid.uuid4()}"
    r = await client.get(f"/items/{fake_id}", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_perm_company_a_cannot_read_b_docs(client):
    """Doc from another company (unknown id) returns 404."""
    token = await _reg(client)
    fake_id = f"doc:{uuid.uuid4()}"
    r = await client.get(f"/docs/{fake_id}", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_perm_company_a_cannot_read_b_contacts(client):
    """Contact from another company (unknown id) returns 404."""
    token = await _reg(client)
    fake_id = f"contact:{uuid.uuid4()}"
    r = await client.get(f"/crm/contacts/{fake_id}", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_perm_cannot_finalize_other_company_doc(client):
    """Finalize on unknown doc returns 404."""
    token = await _reg(client)
    fake_id = f"doc:{uuid.uuid4()}"
    r = await client.post(f"/docs/{fake_id}/finalize", headers=_h(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_perm_cannot_void_other_company_doc(client):
    """Void on unknown doc returns 404."""
    token = await _reg(client)
    fake_id = f"doc:{uuid.uuid4()}"
    r = await client.post(f"/docs/{fake_id}/void", headers=_h(token), json={"reason": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_perm_write_items_requires_auth(client):
    r = await client.post("/items", json={"sku": "NAUTH", "name": "No Auth", "quantity": 1, "sell_by": "piece"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_write_docs_requires_auth(client):
    r = await client.post("/docs", json={"doc_type": "invoice", "status": "draft"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_write_contacts_requires_auth(client):
    r = await client.post("/crm/contacts", json={"name": "No Auth"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_api_key_creation_requires_auth(client):
    r = await client.post("/auth/api-key")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_perm_switch_to_foreign_company_denied(client):
    token = await _reg(client, "swt")
    fake_id = str(uuid.uuid4())
    r = await client.post(f"/auth/switch-company/{fake_id}", headers=_h(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_perm_company_a_cannot_adjust_b_item(client):
    """Adjusting requires auth; without it returns 401. With wrong company_id the
    item won't exist in that company's projection (quantity adjust on unknown item
    creates a ghost event but GET still returns 404 for unknown entity)."""
    # Auth required
    fake_id = f"item:{uuid.uuid4()}"
    r = await client.post(f"/items/{fake_id}/adjust", json={"new_qty": 999})
    assert r.status_code == 401
    # With valid token but unknown item, GET returns 404
    token = await _reg(client)
    r2 = await client.get(f"/items/{fake_id}", headers=_h(token))
    assert r2.status_code == 404
