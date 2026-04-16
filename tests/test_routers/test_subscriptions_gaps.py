# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Coverage gap closers for routers/subscriptions.py:
  - _next_run_date: biweekly, quarterly, annually (leap/non-leap), custom branches (lines 79, 86-99)
  - list subscriptions with status filter (line 121)
  - get_subscription not found → 404 (line 171)
  - patch_subscription not found → 404 (line 185)
  - pause already-paused subscription → 409 (line 213)
  - resume not-paused subscription → 409 (line 242)
  - GET /subscriptions/import/template (line 271)
  - batch import: sub.created entity_id skip + error path (lines 310-311, 330-332)
  - generate_now not found → 404 (line 348)
  - generate_now with total+subtotal on subscription (lines 379-380)
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client) -> str:
    addr = f"subs-{uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post("/auth/register", json={"company_name": "SubCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _sub(client, tok, frequency="monthly", **kwargs) -> str:
    body = {
        "name": "Test Sub",
        "doc_type": "invoice",
        "frequency": frequency,
        "start_date": "2026-01-15",
        "line_items": [{"name": "Item", "quantity": 1, "unit_price": 100}],
        **kwargs,
    }
    r = await client.post("/subscriptions", headers=_h(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# _next_run_date unit tests (biweekly, quarterly, annually, custom)
# ---------------------------------------------------------------------------

def test_next_run_date_all_branches():
    """_next_run_date: biweekly, quarterly, annually (non-leap/leap), custom (lines 79, 86-99)."""
    from celerp_subscriptions.routes import _next_run_date

    # biweekly (line 79)
    assert _next_run_date("biweekly", None, "2026-01-01") == "2026-01-15"

    # quarterly (lines 86-91)
    assert _next_run_date("quarterly", None, "2026-01-15") == "2026-04-15"
    assert _next_run_date("quarterly", None, "2025-11-30") == "2026-02-28"  # date clamping

    # annually non-leap (lines 92-94)
    assert _next_run_date("annually", None, "2025-02-01") == "2026-02-01"

    # annually leap year edge: Feb 29 → Feb 28 on non-leap (lines 95-96)
    assert _next_run_date("annually", None, "2024-02-29") == "2025-02-28"

    # custom (lines 97-99)
    assert _next_run_date("custom", 10, "2026-01-01") == "2026-01-11"
    assert _next_run_date("custom", None, "2026-01-01") == "2026-01-31"  # defaults to 30 days


# ---------------------------------------------------------------------------
# list subscriptions with status filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_list_status_filter(client):
    """GET /subscriptions?status=active (line 121)."""
    tok = await _reg(client)
    await _sub(client, tok)

    r = await client.get("/subscriptions?status=active", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    for item in body["items"]:
        assert item.get("status") == "active"

    r2 = await client.get("/subscriptions?status=cancelled", headers=_h(tok))
    assert r2.status_code == 200
    assert r2.json()["items"] == []


# ---------------------------------------------------------------------------
# get_subscription not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_get_not_found(client):
    """GET /subscriptions/{id} with unknown id → 404 (line 171)."""
    tok = await _reg(client)
    r = await client.get("/subscriptions/sub:nonexistent", headers=_h(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# patch_subscription not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_patch_not_found(client):
    """PATCH /subscriptions/{id} with unknown id → 404 (line 185)."""
    tok = await _reg(client)
    r = await client.patch("/subscriptions/sub:nonexistent", headers=_h(tok), json={"fields_changed": {}})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# pause already-paused subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_pause_already_paused(client):
    """POST /subscriptions/{id}/pause on a paused sub → 409 (line 213)."""
    tok = await _reg(client)
    sid = await _sub(client, tok)

    # Pause once (succeeds)
    r1 = await client.post(f"/subscriptions/{sid}/pause", headers=_h(tok))
    assert r1.status_code == 200

    # Pause again → 409
    r2 = await client.post(f"/subscriptions/{sid}/pause", headers=_h(tok))
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# resume not-paused subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_resume_not_paused(client):
    """POST /subscriptions/{id}/resume on an active sub → 409 (line 242)."""
    tok = await _reg(client)
    sid = await _sub(client, tok)

    # Resume without pausing first → 409
    r = await client.post(f"/subscriptions/{sid}/resume", headers=_h(tok))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# import template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_import_template(client):
    """GET /subscriptions/import/template (line 271)."""
    tok = await _reg(client)
    r = await client.get("/subscriptions/import/template", headers=_h(tok))
    assert r.status_code == 200
    assert "entity_id" in r.text
    assert "frequency" in r.text


# ---------------------------------------------------------------------------
# batch import: entity_id skip + error path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_batch_import_entity_skip(client):
    """Batch import: sub.created skipped when entity_id already exists (lines 310-311)."""
    tok = await _reg(client)
    entity_id = f"sub:{uuid.uuid4()}"
    ik1 = str(uuid.uuid4())

    r1 = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "sub.created",
        "data": {"doc_type": "invoice", "frequency": "monthly", "start_date": "2026-01-01", "name": "S1"},
        "source": "test",
        "idempotency_key": ik1,
    }]})
    assert r1.json()["created"] == 1

    # Same entity_id with sub.created + new key → skipped (line 310-311)
    r2 = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "sub.created",
        "data": {"doc_type": "invoice", "frequency": "monthly", "start_date": "2026-01-01", "name": "S2"},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r2.status_code == 200
    assert r2.json()["skipped"] >= 1


@pytest.mark.asyncio
async def test_subscriptions_batch_import_idempotency_key_skip(client):
    """Batch import: skipped on duplicate idempotency_key."""
    tok = await _reg(client)
    ik = str(uuid.uuid4())
    entity_id = f"sub:{uuid.uuid4()}"

    r1 = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "sub.created",
        "data": {"doc_type": "invoice", "frequency": "monthly", "start_date": "2026-01-01", "name": "Sub"},
        "source": "test",
        "idempotency_key": ik,
    }]})
    assert r1.json()["created"] == 1

    # Same key → skipped
    r2 = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": f"sub:{uuid.uuid4()}",
        "event_type": "sub.created",
        "data": {"doc_type": "invoice", "frequency": "monthly", "start_date": "2026-01-01", "name": "Sub2"},
        "source": "test",
        "idempotency_key": ik,
    }]})
    assert r2.json()["skipped"] >= 1


# ---------------------------------------------------------------------------
# generate_now not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_generate_not_found(client):
    """POST /subscriptions/{id}/generate with unknown id → 404 (line 348)."""
    tok = await _reg(client)
    r = await client.post("/subscriptions/sub:nonexistent/generate", headers=_h(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# generate_now: subscription with total+subtotal (lines 379-380)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscriptions_generate_with_total(client):
    """generate_now with total > 0 on subscription → uses stored total (lines 379-380)."""
    tok = await _reg(client)
    # Import a sub with total + subtotal stored in state
    entity_id = f"sub:{uuid.uuid4()}"
    r = await client.post("/subscriptions/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "sub.created",
        "data": {
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "name": "Sub with total",
            "total": 500.0,
            "subtotal": 450.0,
            "tax": 50.0,
            "line_items": [],
        },
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.json()["created"] == 1

    rg = await client.post(f"/subscriptions/{entity_id}/generate", headers=_h(tok))
    assert rg.status_code == 200
    body = rg.json()
    assert body.get("doc_id", "").startswith("doc:")
    assert "next_run" in body
