# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


async def _register(client):
    r = await client.post("/auth/register", json={"company_name": "Doc Extra", "email": f"x-{uuid.uuid4().hex[:8]}@doc.test", "name": "Admin", "password": "pw"})
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_docs_list_filters_and_refund_guard_and_import_paths(client):
    token = await _register(client)
    inv = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "contact_id": "c", "line_items": [], "subtotal": 10, "tax": 0, "total": 10})).json()["id"]
    po = (await client.post("/docs", headers=_h(token), json={"doc_type": "purchase_order", "contact_id": "s", "line_items": [], "subtotal": 5, "tax": 0, "total": 5})).json()["id"]

    docs = (await client.get("/docs?doc_type=invoice", headers=_h(token))).json()["items"]
    assert all(d.get("doc_type") == "invoice" for d in docs)

    await client.post(f"/docs/{inv}/send", headers=_h(token), json={})
    await client.post(f"/docs/{inv}/finalize", headers=_h(token))
    await client.post(f"/docs/{inv}/payment", headers=_h(token), json={"amount": 4})
    bad_refund = await client.post(f"/docs/{inv}/refund", headers=_h(token), json={"amount": 5})
    assert bad_refund.status_code == 409

    # import single + batch (skip existing key)
    imp1 = await client.post("/docs/import", headers=_h(token), json={"entity_id": "doc:IMP-1", "event_type": "doc.created", "data": {"doc_type": "invoice", "total": 1}, "source": "test", "idempotency_key": "doc-import-1"})
    assert imp1.status_code == 200
    imp2 = await client.post("/docs/import/batch", headers=_h(token), json={"records": [
        {"entity_id": "doc:IMP-1", "event_type": "doc.created", "data": {"doc_type": "invoice", "total": 1}, "source": "test", "idempotency_key": "doc-import-1"},
        {"entity_id": "doc:IMP-2", "event_type": "doc.created", "data": {"doc_type": "invoice", "total": 2}, "source": "test", "idempotency_key": "doc-import-2"},
    ]})
    assert imp2.status_code == 200
    body = imp2.json()
    assert body["created"] == 1 and body["skipped"] == 1

    # receive wrong type guard
    wrong = await client.post(f"/docs/{inv}/receive", headers=_h(token), json={"location_id": "x", "received_items": []})
    assert wrong.status_code == 409

    # convert unsupported doc guard
    unsupported = await client.post(f"/docs/{po}/convert", headers=_h(token))
    assert unsupported.status_code == 409


@pytest.mark.asyncio
async def test_doc_note_added(client):
    token = await _register(client)
    doc_id = (await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "contact_id": "c", "line_items": [], "subtotal": 0, "tax": 0, "total": 0})).json()["id"]

    # Add a note
    r = await client.post(f"/docs/{doc_id}/notes", headers=_h(token), json={"text": "Test internal note"})
    assert r.status_code == 200

    # Verify it's stored in projection
    doc = (await client.get(f"/docs/{doc_id}", headers=_h(token))).json()
    notes = doc.get("internal_notes", [])
    assert len(notes) == 1
    assert notes[0]["text"] == "Test internal note"
    assert notes[0]["created_at"]
    assert notes[0]["created_by"]

    # Empty note should fail
    bad = await client.post(f"/docs/{doc_id}/notes", headers=_h(token), json={"text": "  "})
    assert bad.status_code == 422

    # Add a second note - verify order (newest last in list, newest first when reversed for display)
    await client.post(f"/docs/{doc_id}/notes", headers=_h(token), json={"text": "Second note"})
    doc2 = (await client.get(f"/docs/{doc_id}", headers=_h(token))).json()
    notes2 = doc2.get("internal_notes", [])
    assert len(notes2) == 2
    assert notes2[0]["text"] == "Test internal note"
    assert notes2[1]["text"] == "Second note"
