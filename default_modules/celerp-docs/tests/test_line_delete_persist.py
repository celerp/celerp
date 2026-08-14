# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Issue #153: deleting the last/only line of a document must persist (the line was
silently reverting on refresh), and an empty document must not be issuable.

Behaviour (Option 3): emptying a document is allowed and sticks; finalizing or sending
a zero-line document is blocked with a clear error, for every issue-type doc."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@linedel.test"
    r = await client.post("/auth/register", json={
        "company_name": "LineDel Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.asyncio
async def test_deleting_the_only_line_persists(client):
    """The core bug: delete the sole line (autosave sends line_items: []), and a fresh
    read still shows it gone - it must not reappear."""
    t = await _register(client)
    line = {"sku": "DEMO-PRL-001", "name": "Pearl", "description": "Pearl",
            "quantity": 1, "unit_price": 100}
    doc = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "invoice", "line_items": [line], "total": 100})).json()["id"]

    r = await client.patch(f"/docs/{doc}", headers=_h(t),
                           json={"fields_changed": {"line_items": {"old": [line], "new": []}}})
    assert r.status_code == 200, r.text

    # Fresh read: the deletion stuck (before the fix, the line reappeared here).
    assert (await client.get(f"/docs/{doc}", headers=_h(t))).json().get("line_items") == []


@pytest.mark.asyncio
async def test_finalize_blocked_when_empty(client):
    t = await _register(client)
    doc = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "invoice", "line_items": []})).json()["id"]
    r = await client.post(f"/docs/{doc}/finalize", headers=_h(t))
    assert r.status_code == 422
    assert "at least one line" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_send_blocked_when_empty(client):
    t = await _register(client)
    doc = (await client.post("/docs", headers=_h(t), json={
        "doc_type": "invoice", "line_items": []})).json()["id"]
    r = await client.post(f"/docs/{doc}/send", headers=_h(t), json={"sent_to": "buyer@x.test"})
    assert r.status_code == 422
    assert "at least one line" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_finalize_guard_applies_to_all_issue_types(client):
    """The >=1-line rule is not invoice-specific - it guards every issue-type doc."""
    t = await _register(client)
    for dt in ("invoice", "quotation", "purchase_order", "credit_note"):
        doc = (await client.post("/docs", headers=_h(t), json={
            "doc_type": dt, "line_items": []})).json()["id"]
        r = await client.post(f"/docs/{doc}/finalize", headers=_h(t))
        assert r.status_code == 422, f"{dt}: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_finalize_succeeds_with_a_line(client):
    """Regression: the guard must not block the normal path - a doc with a line finalizes."""
    t = await _register(client)
    item = (await client.post("/items", headers=_h(t), json={
        "status": "available", "sku": "OK-1", "name": "Widget", "quantity": 5, "sell_by": "piece"})).json()["id"]
    doc = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
        {"entity_id": item, "sku": "OK-1", "name": "Widget", "quantity": 1, "unit_price": 10,
         "sell_by": "piece"}], "total": 10})).json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=_h(t))).status_code == 200
