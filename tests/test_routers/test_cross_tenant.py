# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Cross-tenant isolation tests.

Pattern per entity:
  1. Register company A (bootstrap user).
  2. Create company B (additional company via /companies).
  3. Company A creates an entity.
  4. Company B tries GET by ID → 404.
  5. Company B lists → company A's entity absent.
  6. Company B tries PATCH/DELETE → 403 or 404.

The extended journal at the end does not fit that pattern: it is a report, not an
entity, and the thing that must not cross is item detail rather than a row. Its
own comments say what it asserts instead.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update


async def _register_a(client) -> tuple[str, dict]:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Tenant A", "email": "a@cross.test", "name": "Admin A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


async def _create_tenant_b(client, headers_a: dict) -> tuple[str, dict]:
    r = await client.post("/companies", json={"name": "Tenant B"}, headers=headers_a)
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


def _items(payload) -> list[dict]:
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return []


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_item_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/items", json={"sku": "CT-ITEM-A", "name": "Tenant A item", "quantity": 1, "sell_by": "piece"}, headers=ha)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/items/{item_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"

    # List from B → item absent
    r = await client.get("/items", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != item_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_item_patch_delete_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/items", json={"sku": "CT-ITEM-B", "name": "A item B", "quantity": 5, "sell_by": "piece"}, headers=ha)
    assert r.status_code == 200
    item_id = r.json()["id"]

    # PATCH from B uses B's company_id: the event targets B's namespace,
    # so company A's item is NOT modified (isolation is enforced at projection layer).
    r_patch = await client.patch(f"/items/{item_id}", json={"fields_changed": {"name": "Hijacked"}}, headers=hb)
    # API may return 200 (event emitted to B's namespace) or 404 — either is acceptable
    # What matters: company A's item is UNCHANGED
    r_a_item = await client.get(f"/items/{item_id}", headers=ha)
    if r_a_item.status_code == 200:
        item_data = r_a_item.json()
        assert item_data.get("name") != "Hijacked", "Cross-tenant PATCH modified company A's item"

    r = await client.delete(f"/items/{item_id}", headers=hb)
    # DELETE may return 404 or 405 (method not allowed) — just not 200 success on another tenant's item
    assert r.status_code in (403, 404, 405, 422), f"DELETE returned unexpected status: {r.status_code}"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contact_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/crm/contacts", json={"name": "Alice from A"}, headers=ha)
    assert r.status_code == 200, r.text
    contact_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/crm/contacts/{contact_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → contact absent
    r = await client.get("/crm/contacts", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != contact_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_contact_patch_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/crm/contacts", json={"name": "Bob from A"}, headers=ha)
    assert r.status_code == 200
    contact_id = r.json()["id"]

    # CRM PATCH uses company_id from token, so B's patch targets B's namespace.
    # Company A's contact must remain unchanged.
    r_patch = await client.patch(f"/crm/contacts/{contact_id}", json={"name": "Hijacked"}, headers=hb)
    # API returns 404 because B can't find contact in B's namespace, or 403/422.
    if r_patch.status_code == 200:
        # If API returned 200, verify A's contact is unchanged (projection isolation)
        r_a = await client.get(f"/crm/contacts/{contact_id}", headers=ha)
        if r_a.status_code == 200:
            assert r_a.json().get("name") != "Hijacked", "Cross-tenant PATCH modified company A's contact"
    else:
        assert r_patch.status_code in (403, 404, 422), (
            f"Expected blocked PATCH, got {r_patch.status_code}: {r_patch.text}"
        )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "CT-INV-001", "currency": "USD",
              "customer_name": "Cust", "subtotal": 10.0, "total": 10.0,
              "line_items": [{"description": "x", "qty": 1, "unit_price": 10.0}]},
        headers=ha,
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/docs/{doc_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → doc absent
    r = await client.get("/docs", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != doc_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_doc_finalize_void_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "CT-INV-002", "currency": "USD",
              "customer_name": "Cust", "subtotal": 10.0, "total": 10.0,
              "line_items": [{"description": "x", "qty": 1, "unit_price": 10.0}]},
        headers=ha,
    )
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r = await client.post(f"/docs/{doc_id}/finalize", headers=hb)
    assert r.status_code in (403, 404), f"finalize should be blocked, got {r.status_code}"

    r = await client.post(f"/docs/{doc_id}/void", json={"reason": "hijack"}, headers=hb)
    assert r.status_code in (403, 404), f"void should be blocked, got {r.status_code}"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/lists",
        json={"list_type": "sale", "ref_id": "CT-LIST-001", "customer_name": "Cust", "total": 1.0},
        headers=ha,
    )
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/lists/{list_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → list absent
    r = await client.get("/lists", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != list_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_list_action_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/lists",
        json={"list_type": "sale", "ref_id": "CT-LIST-002", "customer_name": "Cust", "total": 1.0},
        headers=ha,
    )
    assert r.status_code == 200
    list_id = r.json()["id"]

    # Try to patch via field endpoint
    r = await client.patch(f"/lists/{list_id}", json={"notes": "hijacked"}, headers=hb)
    assert r.status_code in (403, 404, 405), f"PATCH should be blocked, got {r.status_code}"


# ---------------------------------------------------------------------------
# Extended journal
#
# The extended journal names the item behind each posting, which it does by
# reading the document the entry came from. That makes it the one report whose
# rows are assembled from a second entity, so it gets its own coverage: an entity
# test proves a document is not listed, and these prove its contents do not
# arrive through a report that goes looking for them.
# ---------------------------------------------------------------------------

_PERIOD = "date_from=2026-01-01&date_to=2026-12-31"


async def _sell_an_item(client, headers, sku: str) -> str:
    """Finalize an invoice for `sku` and return its document id.

    Finalizing is what posts the entry, and an invoice posts its revenue as one
    line for the whole document, which is the shape the extended journal splits
    back into a row per item. So this is the case with item detail to leak.
    """
    r = await client.post("/accounting/chart/seed", headers=headers)
    assert r.status_code in (200, 409), r.text
    r = await client.post(
        "/crm/contacts", json={"name": "Cust", "contact_type": "customer"}, headers=headers)
    assert r.status_code == 200, r.text
    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "contact_id": r.json()["id"], "issue_date": "2026-05-04",
              "line_items": [{"sku": sku, "name": sku, "quantity": 3, "unit_price": 12.5,
                              "line_total": 37.5}],
              "subtotal": 37.5, "tax": 0.0, "total": 37.5},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=headers)
    assert r.status_code == 200, r.text
    return doc_id


@pytest.mark.asyncio
async def test_extended_journal_item_detail_not_visible_to_other_tenant(client):
    """Company A's sale, item and all, is absent from company B's extended journal.

    Company A is checked first, and for the item detail specifically rather than
    for a 200: this report can decline to attach item detail
    (`items_status` other than `expanded`) when the figures do not tie, and an absence proves nothing against a
    report that attached none for anybody.
    """
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    sku = "CT-XJ-SOLD-BY-A"
    doc_id = await _sell_an_item(client, ha, sku)

    ra = await client.get(f"/accounting/extended-journal?{_PERIOD}", headers=ha)
    assert ra.status_code == 200, ra.text
    assert any(e["items_status"] == "expanded" for e in ra.json()["entries"]), (
        f"company A's own sale was not expanded to its item, so the absence "
        f"below would prove nothing: {ra.text}"
    )
    assert sku in ra.text

    rb = await client.get(f"/accounting/extended-journal?{_PERIOD}", headers=hb)
    assert rb.status_code == 200, rb.text
    assert rb.json()["entries"] == [], f"company A's entry reached company B: {rb.text}"
    assert sku not in rb.text
    assert doc_id not in rb.text


@pytest.mark.asyncio
async def test_extended_journal_will_not_resolve_another_tenants_document(client, session):
    """An entry pointing at another tenant's document resolves to no item detail.

    The test above cannot show this. Company B has no entries there, so an empty
    report passes whether the document lookup is scoped by company or not. Here
    company B posts an entry of its own and the document id stored with it is
    repointed at company A's invoice, which is the one shape this leak can take:
    the reference is read back from the entry, never supplied with the request, so
    no caller can ask for another tenant's document directly.

    The lookup filters on company id (`_je_doc_refs`), so the reference has to come
    back unresolved. What that looks like from outside: no item, no quantity, no
    price, and the reference shown is the raw id the entry carries rather than
    company A's document number.
    """
    from celerp.models.ledger import LedgerEntry

    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    sku = "CT-XJ-NOT-FOR-B"
    doc_id = await _sell_an_item(client, ha, sku)
    a_doc_ref = (await client.get(f"/docs/{doc_id}", headers=ha)).json().get("ref_id")

    r = await client.post("/accounting/chart/seed", headers=hb)
    assert r.status_code in (200, 409), r.text
    r = await client.post(
        "/accounting/journal-entries",
        json={"ts": "2026-05-05", "memo": "Company B's own entry",
              "entries": [{"account": "1111", "debit": 37.5, "credit": 0},
                          {"account": "4100", "debit": 0, "credit": 37.5}],
              "idempotency_token": uuid.uuid4().hex},
        headers=hb,
    )
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]

    await session.execute(
        update(LedgerEntry)
        .where(LedgerEntry.entity_id == je_id,
               LedgerEntry.event_type == "acc.journal_entry.created")
        .values(metadata_={"doc_id": doc_id})
    )
    await session.commit()

    rb = await client.get(f"/accounting/extended-journal?{_PERIOD}", headers=hb)
    assert rb.status_code == 200, rb.text
    assert sku not in rb.text, f"company A's item arrived through the reference: {rb.text}"
    if a_doc_ref:
        assert a_doc_ref not in rb.text

    entry = next(e for e in rb.json()["entries"] if e["je_id"] == je_id)
    assert entry["items_status"] == "no_document", (
        "an entry whose document the company cannot read is reported as such"
    )
    assert entry["source_doc"]["doc_ref"] == doc_id, (
        "the reference resolved to company A's document number"
    )
    for line in entry["lines"]:
        assert "item" not in line, f"item detail attached from another tenant: {line}"
