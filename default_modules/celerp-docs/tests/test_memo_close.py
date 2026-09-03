# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Terminal Close/Reopen for memo documents.

A fully-resolved memo (no line still On Memo / memo_out) can be Closed so it
leaves the "partially fulfilled" limbo; Close is memo-only, live-status-only,
and refuses with a product count when any line is still out at the customer.
Reopen restores the pre-close status. Both are gated by finalize_documents.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@memoclose.test"
    r = await client.post("/auth/register", json={"company_name": "MemoClose Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@memoclose.test"
    r = await client.post(
        "/companies/me/users",
        json={"name": role.title(), "email": addr, "password": "testpass123", "role": role},
        headers=_h(admin_token),
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def _item(client, h, sku, qty=1) -> str:
    r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _memo(client, h, item_ids: list[str]) -> str:
    line_items = [{"entity_id": i, "sku": f"S-{n}", "name": f"S-{n}", "quantity": 1, "unit_price": 10, "sell_by": "piece"}
                  for n, i in enumerate(item_ids)]
    r = await client.post("/docs", headers=h, json={"doc_type": "memo", "line_items": line_items})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _finalize(client, h, doc_id):
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=h)).status_code == 200


async def _events(client, h, doc_id):
    r = await client.get("/ledger", params={"entity_id": doc_id, "limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    return [e["event_type"] for e in r.json()["items"]]


@pytest.mark.asyncio
async def test_close_settles_resolved_memo(client):
    """A memo with no line still memo_out closes: status -> closed, doc.closed logged."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-A")
    memo = await _memo(client, h, [a])
    await _finalize(client, h, memo)
    # Ship the stone then sell it back so nothing is left memo_out (resolved).
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200
    assert (await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_entity_ids": [a]})).status_code == 200

    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == "closed"
    assert "doc.closed" in await _events(client, h, memo)


@pytest.mark.asyncio
async def test_close_refused_counts_pending(client):
    """Close with N lines still memo_out returns 409 naming N; no doc.closed emitted."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-P1")
    b = await _item(client, h, "MC-P2")
    memo = await _memo(client, h, [a, b])
    await _finalize(client, h, memo)
    # Ship both; both are now memo_out (out at the customer, unresolved).
    assert (await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [a, b]})).status_code == 200

    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409, r.text
    assert "2" in r.json()["detail"]
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") != "closed"
    assert "doc.closed" not in await _events(client, h, memo)


@pytest.mark.asyncio
async def test_close_rejects_non_memo(client):
    """Close on a non-memo doc (invoice) returns 422."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-INV")
    doc = (await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"entity_id": a, "sku": "MC-INV", "name": "A", "quantity": 1, "unit_price": 10, "sell_by": "piece"}]})).json()["id"]
    await _finalize(client, h, doc)
    r = await client.post(f"/docs/{doc}/close", headers=h, json={})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_close_rejects_unissued(client):
    """Close on a draft (un-issued) memo returns 409."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-DRAFT")
    memo = await _memo(client, h, [a])
    # Not finalized: still draft.
    r = await client.post(f"/docs/{memo}/close", headers=h, json={})
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_reopen_restores_prior_status(client):
    """Reopen a closed memo restores the pre-close status and drops pre_close_status."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-RE")
    memo = await _memo(client, h, [a])
    await _finalize(client, h, memo)
    pre = (await client.get(f"/docs/{memo}", headers=h)).json().get("status")
    assert pre == "final"
    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200

    r = await client.post(f"/docs/{memo}/reopen", headers=h, json={})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{memo}", headers=h)).json()
    assert doc.get("status") == pre
    assert "pre_close_status" not in doc
    assert "doc.reopened" in await _events(client, h, memo)


@pytest.mark.asyncio
async def test_reopen_rejects_non_closed(client):
    """Reopen on a memo that is not closed returns 409."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-NR")
    memo = await _memo(client, h, [a])
    await _finalize(client, h, memo)
    r = await client.post(f"/docs/{memo}/reopen", headers=h, json={})
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_close_requires_finalize_permission(client, session):
    """Close and Reopen without finalize_documents (viewer role) return 403."""
    token = await _register(client)
    h = _h(token)
    a = await _item(client, h, "MC-PERM")
    memo = await _memo(client, h, [a])
    await _finalize(client, h, memo)

    viewer = await _user_with_role(client, session, token, "viewer")
    assert (await client.post(f"/docs/{memo}/close", headers=_h(viewer), json={})).status_code == 403
    # Admin closes so we can probe reopen's gate on a genuinely closed memo.
    assert (await client.post(f"/docs/{memo}/close", headers=h, json={})).status_code == 200
    assert (await client.post(f"/docs/{memo}/reopen", headers=_h(viewer), json={})).status_code == 403
