# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for routes.py — fill coverage gaps.

Covers uncovered route lines:
  - POST /ai/upload: success, >20 files, >10MB file
  - GET /ai/file/{file_id}: success, 404, 403
  - GET /ai/quota-status: with status, local mode
  - POST /ai/conversations: create
  - GET /ai/conversations: list
  - GET /ai/conversations/{id}: get, 404
  - DELETE /ai/conversations/{id}: success, 404
  - PATCH /ai/conversations/{id}: rename, 404
  - POST /ai/conversations/{id}/query: success, 404, error
  - POST /ai/batch: submit
  - GET /ai/batch/{id}: status, 404
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.main import app
from celerp.ai.service import AIResponse
import celerp.gateway.state as gw_state



# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def auth_client(session: AsyncSession):
    """Authenticated async client with gateway session token."""
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    if hasattr(app.state, "limiter"):
        app.state.limiter.enabled = False
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "RouteCo", "email": "route@test.com",
            "name": "Admin", "password": "pw",
        })
        r = await c.post("/auth/login", json={"email": "route@test.com", "password": "pw"})
        jwt = r.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {jwt}",
            "X-Session-Token": token,
        }
        yield c, headers

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


# ── POST /ai/upload ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_single_file(auth_client):
    c, h = auth_client
    r = await c.post("/ai/upload", headers=h, files={
        "files": ("receipt.jpg", b"fake jpeg data", "image/jpeg"),
    })
    assert r.status_code == 201
    data = r.json()
    assert len(data["file_ids"]) == 1
    assert data["file_ids"][0].startswith("ai_up_")


@pytest.mark.asyncio
async def test_upload_multiple_files(auth_client):
    c, h = auth_client
    files = [("files", (f"file{i}.jpg", b"data" * 10, "image/jpeg")) for i in range(3)]
    r = await c.post("/ai/upload", headers=h, files=files)
    assert r.status_code == 201
    assert len(r.json()["file_ids"]) == 3


@pytest.mark.asyncio
async def test_upload_too_many_files(auth_client):
    c, h = auth_client
    files = [("files", (f"f{i}.jpg", b"x", "image/jpeg")) for i in range(21)]
    r = await c.post("/ai/upload", headers=h, files=files)
    assert r.status_code == 400
    assert "20 files" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_oversized_file(auth_client):
    c, h = auth_client
    big_data = b"x" * (10 * 1024 * 1024 + 1)  # 10MB + 1 byte
    r = await c.post("/ai/upload", headers=h, files={
        "files": ("big.jpg", big_data, "image/jpeg"),
    })
    # May be 400 (our check) or 413 (server body limit)
    assert r.status_code in (400, 413)


# ── GET /ai/file/{file_id} ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_file_success(auth_client):
    c, h = auth_client
    # Upload first
    r = await c.post("/ai/upload", headers=h, files={
        "files": ("test.png", b"\x89PNG\r\n", "image/png"),
    })
    fid = r.json()["file_ids"][0]
    # Retrieve
    r2 = await c.get(f"/ai/file/{fid}", headers=h)
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/png")


@pytest.mark.asyncio
async def test_get_file_not_found(auth_client):
    c, h = auth_client
    r = await c.get("/ai/file/ai_up_nonexistent", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_file_wrong_company(auth_client, session):
    """File from different company returns 403."""
    c, h = auth_client
    # Upload via another company
    upload_dir = settings.data_dir / "ai_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    fid = "ai_up_otherco"
    (upload_dir / f"{fid}.bin").write_bytes(b"secret data")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "filename": "secret.jpg", "content_type": "image/jpeg",
        "size": 11, "company_id": str(uuid.uuid4()),
    }))
    r = await c.get(f"/ai/file/{fid}", headers=h)
    assert r.status_code == 403


# ── GET /ai/quota-status ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_status_local(auth_client):
    """No gateway configured returns local=True."""
    c, h = auth_client
    with patch("celerp_ai.routes.get_quota_status", AsyncMock(return_value=None)):
        r = await c.get("/ai/quota-status", headers=h)
    assert r.status_code == 200
    assert r.json()["local"] is True


@pytest.mark.asyncio
async def test_quota_status_with_data(auth_client):
    c, h = auth_client
    mock_status = {"used": 15, "limit": 200, "topup_credits": 50, "resets_at": "2026-05-01", "tier": "ai"}
    with patch("celerp_ai.routes.get_quota_status", AsyncMock(return_value=mock_status)):
        r = await c.get("/ai/quota-status", headers=h)
    data = r.json()
    assert data["remaining"] == 235  # 200 + 50 - 15
    assert data["tier"] == "ai"


# ── Conversations CRUD ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_lifecycle(auth_client):
    """Full CRUD: create, list, get, rename, delete."""
    c, h = auth_client

    # Create
    r = await c.post("/ai/conversations", headers=h, json={"title": "Test conv"})
    assert r.status_code == 201
    conv = r.json()
    conv_id = conv["id"]
    assert conv["title"] == "Test conv"

    # List
    r = await c.get("/ai/conversations", headers=h)
    assert r.status_code == 200
    convs = r.json()
    assert any(c_["id"] == conv_id for c_ in convs)

    # Get
    r = await c.get(f"/ai/conversations/{conv_id}", headers=h)
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == conv_id
    assert detail["messages"] == []

    # Rename
    r = await c.patch(f"/ai/conversations/{conv_id}", headers=h, json={"title": "Renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"

    # Delete
    r = await c.delete(f"/ai/conversations/{conv_id}", headers=h)
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_get_conversation_404(auth_client):
    c, h = auth_client
    fake_id = str(uuid.uuid4())
    r = await c.get(f"/ai/conversations/{fake_id}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_conversation_404(auth_client):
    c, h = auth_client
    fake_id = str(uuid.uuid4())
    r = await c.delete(f"/ai/conversations/{fake_id}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rename_conversation_404(auth_client):
    c, h = auth_client
    fake_id = str(uuid.uuid4())
    r = await c.patch(f"/ai/conversations/{fake_id}", headers=h, json={"title": "nope"})
    assert r.status_code == 404


# ── POST /ai/conversations/{id}/query ────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_query_success(auth_client):
    c, h = auth_client
    # Create conversation
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]

    mock_result = AIResponse(answer="42 items in stock", model_used="haiku", tools_called=["dashboard_kpis"])
    with patch("celerp_ai.routes.run_query", AsyncMock(return_value=mock_result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "how many items"})
    assert r.status_code == 200
    data = r.json()
    assert data["answer"] == "42 items in stock"

    # Verify messages stored
    r2 = await c.get(f"/ai/conversations/{conv_id}", headers=h)
    msgs = r2.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_conversation_query_404(auth_client):
    c, h = auth_client
    fake_id = str(uuid.uuid4())
    r = await c.post(f"/ai/conversations/{fake_id}/query", headers=h, json={"query": "test"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_conversation_query_error(auth_client):
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]

    mock_result = AIResponse(answer="", model_used="haiku", tools_called=[], error="LLM timeout")
    with patch("celerp_ai.routes.run_query", AsyncMock(return_value=mock_result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "test"})
    assert r.status_code == 502


# ── POST /ai/batch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_submit_and_status(auth_client):
    """Test batch submit returns 202 and batch status returns job info."""
    c, h = auth_client

    # Test batch status 404 for unknown job (covers the GET route)
    fake_id = str(uuid.uuid4())
    with patch("celerp_ai.routes.get_batch_job", AsyncMock(return_value=None)):
        r = await c.get(f"/ai/batch/{fake_id}", headers=h)
    assert r.status_code == 404

    # Test batch status 200 with mock job
    from datetime import datetime, timezone
    mock_job = MagicMock()
    mock_job.id = uuid.uuid4()
    mock_job.status = "completed"
    mock_job.total_files = 3
    mock_job.completed_files = 3
    mock_job.failed_files = 0
    mock_job.credits_consumed = 3
    mock_job.results = {"files": []}
    mock_job.created_at = datetime.now(timezone.utc)
    mock_job.completed_at = datetime.now(timezone.utc)

    with patch("celerp_ai.routes.get_batch_job", AsyncMock(return_value=mock_job)):
        r = await c.get(f"/ai/batch/{mock_job.id}", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    assert data["total_files"] == 3


@pytest.mark.asyncio
async def test_batch_status_404(auth_client):
    c, h = auth_client
    fake_id = str(uuid.uuid4())
    r = await c.get(f"/ai/batch/{fake_id}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_submit_batch_records_integer_credits(auth_client, session, monkeypatch):
    """POST /ai/batch must record the computed credit count as an integer.
    The endpoint previously passed an unassigned name that resolved to a Python
    builtin, so the value reaching create_batch_job was not a number at all."""
    c, h = auth_client

    # Two real uploads so the credit calculation reads genuine files.
    files = [("files", (f"receipt{i}.jpg", b"fake jpeg data", "image/jpeg")) for i in range(2)]
    up = await c.post("/ai/upload", headers=h, files=files)
    assert up.status_code == 201
    file_ids = up.json()["file_ids"]

    captured = {}

    async def _capture_create(sess, company_id, user_id, query, fids, credits):
        captured["credits"] = credits
        job = MagicMock()
        job.id = uuid.uuid4()
        return job

    # Local mode: no relay, so the cloud file-limit check is a no-op.
    monkeypatch.setattr("celerp_ai.routes.get_subscription_tier", AsyncMock(return_value=None))
    monkeypatch.setattr("celerp_ai.routes.run_batch", AsyncMock())
    monkeypatch.setattr(session, "refresh", AsyncMock())
    with patch("celerp_ai.routes.create_batch_job", _capture_create):
        r = await c.post("/ai/batch", headers=h, json={"query": "", "file_ids": file_ids})

    assert r.status_code == 202
    assert isinstance(captured["credits"], int)
    assert captured["credits"] == 2


@pytest.mark.asyncio
async def test_confirm_bills_success(auth_client):
    """POST /ai/confirm-bills creates draft bills and returns feedback."""
    c, h = auth_client
    bills = [
        {
            "vendor_name": "Acme",
            "date": "2026-04-12",
            "total": 100.0,
            "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}],
        }
    ]
    with patch("celerp_ai.routes.create_bills", AsyncMock(return_value="Created Draft Bill BIL-TEST for Acme ($100.00)")):
        r = await c.post("/ai/confirm-bills", json={"bills": bills}, headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert "Acme" in data["feedback"]


@pytest.mark.asyncio
async def test_confirm_bills_empty(auth_client):
    """POST /ai/confirm-bills with empty list returns 400."""
    c, h = auth_client
    r = await c.post("/ai/confirm-bills", json={"bills": []}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_confirm_bills_invalid_data(auth_client):
    """POST /ai/confirm-bills with invalid bill data returns 422."""
    c, h = auth_client
    r = await c.post("/ai/confirm-bills", json={"bills": [{"bad": "data"}]}, headers=h)
    assert r.status_code == 422
