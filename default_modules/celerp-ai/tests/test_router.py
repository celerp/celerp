# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/routers/ai.py — 100% line coverage.

Covers:
  - POST /ai/query: success, AI error → 502
  - GET  /ai/memory: returns notes + kv
  - DELETE /ai/memory: clears memory
  - POST /ai/memory/notes: appends note
  - POST /ai/memory/kv: sets kv pair
  - All routes: auth required, session token required
"""

from __future__ import annotations

import os
import secrets
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.main import app
from celerp.ai.service import AgentResult
import celerp.gateway.state as gw_state



# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def auth_client(session: AsyncSession):
    """Authenticated async client with gateway session token pre-set."""
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "AICo", "email": "ai@test.com",
            "name": "Admin", "password": "validpass1",
        })
        r = await c.post("/auth/login", json={"email": "ai@test.com", "password": "validpass1"})
        jwt = r.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {jwt}",
            "X-Session-Token": token,
        }
        yield c, headers

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


# ── Authentication guard ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_query_requires_auth(auth_client):
    c, headers = auth_client
    r = await c.post("/ai/query", json={"query": "test"})
    # No auth header → 401 from JWT
    r2 = await c.post("/ai/query", json={"query": "test"},
                      headers={"X-Session-Token": gw_state.get_session_token()})
    assert r2.status_code == 401  # JWT missing


@pytest.mark.asyncio
async def test_ai_query_requires_session_token(session):
    """Valid JWT but no session token → 401."""
    from celerp.services.session_tracker import clear as _clear_tracker
    app.dependency_overrides[get_session] = lambda: session
    saved = gw_state.get_session_token()
    gw_state.set_session_token("")
    await _clear_tracker(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post("/auth/register", json={
                "company_name": "Co2", "email": "t2@test.com",
                "name": "U", "password": "validpass1",
            })
            await _clear_tracker(session)  # clear JTI registered by /register so /login can proceed
            r = await c.post("/auth/login", json={"email": "t2@test.com", "password": "validpass1"})
            jwt = r.json()["access_token"]
            resp = await c.post(
                "/ai/query",
                json={"query": "test"},
                headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": "wrong"},
            )
        assert resp.status_code == 401
    finally:
        gw_state.set_session_token(saved)
        app.dependency_overrides.clear()


# ── POST /ai/query ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_query_success(auth_client):
    c, headers = auth_client
    mock_result = AgentResult(answer="You have 3 items.", model_used="claude-haiku-4-5", tools_called=["dashboard_kpis"], pending_actions=[])
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=mock_result)):
        r = await c.post("/ai/query", json={"query": "how many items"}, headers=headers)
    if r.status_code != 200:
        print(f"\nFAIL detail: {r.status_code} {r.text[:300]}")
    assert r.status_code == 200
    data = r.json()
    assert data["answer"] == "You have 3 items."
    assert data["model_used"] == "claude-haiku-4-5"
    assert data["tools_called"] == ["dashboard_kpis"]


@pytest.mark.asyncio
async def test_ai_query_error_502(auth_client):
    c, headers = auth_client
    mock_result = AgentResult(answer="", model_used="claude-haiku-4-5", tools_called=[], pending_actions=[], error="API timeout")
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=mock_result)):
        r = await c.post("/ai/query", json={"query": "test"}, headers=headers)
    assert r.status_code == 502
    assert "timeout" in r.json()["detail"]


@pytest.mark.asyncio
async def test_ai_query_empty_string_rejected(auth_client):
    """Empty query string → 422 validation error."""
    c, headers = auth_client
    r = await c.post("/ai/query", json={"query": ""}, headers=headers)
    assert r.status_code == 422


# ── GET /ai/memory ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_memory_empty(auth_client):
    c, headers = auth_client
    r = await c.get("/ai/memory", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["notes"] == []
    assert data["kv"] == {}


# ── POST /ai/memory/notes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_memory_note(auth_client):
    c, headers = auth_client
    r = await c.post("/ai/memory/notes", json={"content": "Supplier ABC has 30-day terms"}, headers=headers)
    assert r.status_code == 201
    assert r.json()["ok"] is True

    r2 = await c.get("/ai/memory", headers=headers)
    assert len(r2.json()["notes"]) == 1
    assert r2.json()["notes"][0]["content"] == "Supplier ABC has 30-day terms"


@pytest.mark.asyncio
async def test_add_memory_note_empty_rejected(auth_client):
    c, headers = auth_client
    r = await c.post("/ai/memory/notes", json={"content": ""}, headers=headers)
    assert r.status_code == 422


# ── POST /ai/memory/kv ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_memory_kv(auth_client):
    c, headers = auth_client
    r = await c.post("/ai/memory/kv", json={"key": "currency", "value": "THB"}, headers=headers)
    assert r.status_code == 201
    assert r.json()["ok"] is True

    r2 = await c.get("/ai/memory", headers=headers)
    assert r2.json()["kv"]["currency"] == "THB"


# ── DELETE /ai/memory ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_memory(auth_client):
    c, headers = auth_client

    # Add some memory first
    await c.post("/ai/memory/notes", json={"content": "test note"}, headers=headers)
    await c.post("/ai/memory/kv", json={"key": "k", "value": "v"}, headers=headers)

    r = await c.delete("/ai/memory", headers=headers)
    assert r.status_code == 204

    r2 = await c.get("/ai/memory", headers=headers)
    assert r2.json()["notes"] == []
    assert r2.json()["kv"] == {}


# ── Multi-file queries ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_tier_multi_file_allowed(auth_client):
    """Several files can go with one question."""
    c, headers = auth_client
    mock_result = AgentResult(answer="Done.", model_used="claude-sonnet-4-5", tools_called=[], pending_actions=[])

    upload = await c.post(
        "/ai/upload",
        headers=headers,
        files=[
            ("files", ("test_a.jpg", b"fake image data", "image/jpeg")),
            ("files", ("test_b.jpg", b"fake image data", "image/jpeg")),
        ],
    )
    assert upload.status_code == 201
    file_ids = upload.json()["file_ids"]

    mocked = AsyncMock(return_value=mock_result)
    with patch("celerp_ai.routes.run_agent", mocked):
        r = await c.post(
            "/ai/query",
            json={"query": "process these", "file_ids": file_ids},
            headers=headers,
        )
    assert r.status_code == 200
    assert mocked.await_args.kwargs["file_ids"] == file_ids
    assert mocked.await_args.kwargs["read_only"] is True


# ── POST /ai/estimate-credits ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_estimate_credits_images(auth_client):
    """Estimate credits for 3 image files → 3 credits (1 per image)."""
    c, headers = auth_client
    with patch("celerp_ai.routes._load_file_http") as mock_load:
        mock_load.return_value = (b"fake jpeg data", {
            "content_type": "image/jpeg",
            "filename": "receipt.jpg",
        })
        r = await c.post(
            "/ai/estimate-credits",
            json={"file_ids": ["ai_up_1", "ai_up_2", "ai_up_3"]},
            headers=headers,
        )
    assert r.status_code == 200
    data = r.json()
    assert data["total_credits"] == 3
    assert len(data["files"]) == 3
    assert all(f["pages"] == 1 for f in data["files"])
    assert all(f["credits"] == 1 for f in data["files"])


@pytest.mark.asyncio
async def test_estimate_credits_is_per_unique_file_not_pages(auth_client):
    """Page count is informational; one unique file is one billed model call."""
    c, headers = auth_client
    with (
        patch("celerp_ai.routes._load_file_http") as mock_load,
        patch("celerp_ai.routes.count_pages", return_value=15),
    ):
        mock_load.return_value = (b"pdf", {
            "content_type": "application/pdf",
            "filename": "statement.pdf",
        })
        r = await c.post(
            "/ai/estimate-credits",
            json={"file_ids": ["ai_up_pdf", "ai_up_pdf"]},
            headers=headers,
        )
    assert r.status_code == 200
    assert r.json() == {
        "total_credits": 1,
        "files": [{
            "file_id": "ai_up_pdf",
            "filename": "statement.pdf",
            "pages": 15,
            "credits": 1,
        }],
    }


@pytest.mark.asyncio
async def test_estimate_credits_empty_file_set_is_zero(auth_client):
    c, headers = auth_client
    r = await c.post("/ai/estimate-credits", json={"file_ids": []}, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"total_credits": 0, "files": []}
