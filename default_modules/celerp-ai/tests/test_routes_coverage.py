# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for routes.py, filling coverage gaps.

Covers uncovered route lines:
  - POST /ai/upload: success, >20 files, >10MB file, unsupported type
  - GET /ai/file/{file_id}: success, 404, 403
  - GET /ai/quota-status: with status, local mode
  - POST /ai/conversations: create
  - GET /ai/conversations: list
  - GET /ai/conversations/{id}: get, 404
  - DELETE /ai/conversations/{id}: success, 404
  - PATCH /ai/conversations/{id}: rename, 404
  - POST /ai/conversations/{id}/query: agent reads, pending actions, 404, error, validation
  - POST /ai/conversations/{id}/confirm: execute pending, not-pending, capability-unavailable
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
from celerp.ai.service import AgentResult, PendingAction
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
            "name": "Admin", "password": "validpass1",
        })
        r = await c.post("/auth/login", json={"email": "route@test.com", "password": "validpass1"})
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


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_type(auth_client):
    """A content type the agent cannot read is refused with 400."""
    c, h = auth_client
    r = await c.post("/ai/upload", headers=h, files={
        "files": ("payload.exe", b"MZ\x90\x00", "application/x-msdownload"),
    })
    assert r.status_code == 400
    assert "unsupported type" in r.json()["detail"]


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
    fid = f"ai_up_{uuid.uuid4().hex}"
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

def _pending(name="create_contact", call_id="call_1"):
    """A PendingAction as run_agent would return one."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return PendingAction(
        id=call_id, name=name, arguments={"body": {"name": "Acme"}},
        created_at=now.isoformat(), expires_at=(now + timedelta(minutes=15)).isoformat(),
        title="Create contact",
    )


@pytest.mark.asyncio
async def test_conversation_query_reads_only(auth_client):
    """A read-only agent turn stores both messages and returns no pending action."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]

    result = AgentResult(
        answer="42 items in stock", model_used="glm", tools_called=["dashboard_kpis"],
        pending_actions=[],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "how many items"})
    assert r.status_code == 200
    data = r.json()
    assert data["answer"] == "42 items in stock"
    assert data["tools_called"] == ["dashboard_kpis"]
    assert data["pending_actions"] == []

    r2 = await c.get(f"/ai/conversations/{conv_id}", headers=h)
    msgs = r2.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_conversation_query_returns_pending_action(auth_client):
    """A proposed mutation comes back as a pending action carrying its message_id."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]

    result = AgentResult(
        answer="I can create that contact.", model_used="glm", tools_called=[],
        pending_actions=[_pending()],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "add Acme"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["pending_actions"]) == 1
    pa = data["pending_actions"][0]
    assert pa["name"] == "create_contact"
    assert pa["id"] == "call_1"
    assert pa["message_id"]

    # The stored assistant message projects the proposed action for the UI.
    r2 = await c.get(f"/ai/conversations/{conv_id}", headers=h)
    assistant = r2.json()["messages"][1]
    assert [p["name"] for p in assistant["pending_actions"]] == ["create_contact"]


@pytest.mark.asyncio
async def test_conversation_query_requires_query_or_files(auth_client):
    """An empty query with no files is rejected before the agent runs."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]
    r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "   "})
    assert r.status_code == 422


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

    result = AgentResult(
        answer="", model_used="glm", tools_called=[], pending_actions=[],
        error="The AI service took too long to respond. Please try again.",
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "test"})
    assert r.status_code == 200
    assert r.json()["error"] == result.error
    assert r.json()["answer"] == ""

    # The failure is kept in the thread as an assistant message flagged as an error.
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    roles = [(m["role"], m["error"]) for m in thread["messages"]]
    assert roles == [("user", False), ("assistant", True)]
    assert thread["messages"][1]["content"] == result.error
    assert thread["messages"][1]["pending_actions"] == []


# ── POST /ai/conversations/{id}/confirm ──────────────────────────────────────

async def _propose_action(c, h, conv_id, name="create_contact", call_id="call_1"):
    """Run a query that yields a pending action; return (message_id, tool_call_id)."""
    result = AgentResult(
        answer="I can create that contact.", model_used="glm", tools_called=[],
        pending_actions=[_pending(name=name, call_id=call_id)],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "add Acme"})
    pa = r.json()["pending_actions"][0]
    return pa["message_id"], pa["id"]


@pytest.mark.asyncio
async def test_confirm_action_executes_pending(auth_client):
    """Confirming a pending action executes it once; a second confirm is 409."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]
    message_id, tool_call_id = await _propose_action(c, h, conv_id)

    exec_mock = AsyncMock(return_value={"ok": True, "status": 201, "data": {"id": "new"}, "error": None})
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value={"create_contact": {"method": "POST"}}), \
         patch("celerp_ai.routes.execute_agent_capability", exec_mock):
        r = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": message_id, "tool_call_id": tool_call_id},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["status"] == 201
    assert exec_mock.await_count == 1

    # Re-confirming the same action finds nothing pending.
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value={"create_contact": {"method": "POST"}}), \
         patch("celerp_ai.routes.execute_agent_capability", exec_mock):
        r = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": message_id, "tool_call_id": tool_call_id},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "action_not_pending"
    assert exec_mock.await_count == 1  # never executed twice


@pytest.mark.asyncio
async def test_confirm_action_names_action_and_route_rejection(auth_client):
    """The reply carries the action title, and a route that rejects the write
    with a plain detail string becomes the error message the user reads."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]
    message_id, tool_call_id = await _propose_action(c, h, conv_id)

    rejected = {"ok": False, "status": 422, "data": {"detail": "Invalid currency code: ZZZZ"}}
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value={"create_contact": {"method": "POST"}}), \
         patch("celerp_ai.routes.execute_agent_capability", AsyncMock(return_value=rejected)):
        r = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": message_id, "tool_call_id": tool_call_id},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["title"] == "Create contact"
    assert body["error"] == {"code": "route_error", "message": "Invalid currency code: ZZZZ"}


@pytest.mark.asyncio
async def test_confirm_action_capability_unavailable(auth_client):
    """A confirmed action whose capability is gone fails closed with 409."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]
    message_id, tool_call_id = await _propose_action(c, h, conv_id)

    with patch("celerp_ai.routes.compile_agent_capabilities", return_value={}):
        r = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": message_id, "tool_call_id": tool_call_id},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "capability_unavailable"

    # The action is finalized (failed), so it is no longer pending or claimable.
    r2 = await c.get(f"/ai/conversations/{conv_id}", headers=h)
    assert r2.json()["messages"][1]["pending_actions"] == []


@pytest.mark.asyncio
async def test_confirm_action_unknown_message(auth_client):
    """Confirming against a message that holds no such action is 409, not a 500."""
    c, h = auth_client
    r = await c.post("/ai/conversations", headers=h, json={"title": None})
    conv_id = r.json()["id"]
    r = await c.post(
        f"/ai/conversations/{conv_id}/confirm", headers=h,
        json={"message_id": str(uuid.uuid4()), "tool_call_id": "call_missing"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "action_not_pending"


# ── GET /ai/batch/{id} ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_submit_and_status(auth_client):
    """Batch status returns job info, 404 for an unknown job."""
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
    mock_job.conversation_id = None
    mock_job.error = None
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


# ── Attachments route to a reading job ───────────────────────────────────────

async def _upload(c, h, name, data, content_type):
    r = await c.post("/ai/upload", headers=h, files={"files": (name, data, content_type)})
    assert r.status_code == 201
    return r.json()["file_ids"][0]


@pytest.mark.asyncio
async def test_query_missing_file_is_404_before_store(auth_client):
    """An unknown file id fails before any message is stored."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    r = await c.post(
        f"/ai/conversations/{conv_id}/query", headers=h,
        json={"query": "read this", "file_ids": ["ai_up_missing"]},
    )
    assert r.status_code == 404
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert thread["messages"] == []


@pytest.mark.asyncio
async def test_mixed_attachments_run_agent_in_chat_mode(auth_client):
    """Mixed files are valid chat context; only explicit receipt mode starts a batch."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    jpg = await _upload(c, h, "receipt.jpg", b"fake jpeg", "image/jpeg")
    csv = await _upload(c, h, "statement.csv", b"date,amount\n2026-09-01,10\n", "text/csv")
    mocked = AsyncMock(return_value=AgentResult(
        answer="Reviewed both files.", model_used="fake", tools_called=[], pending_actions=[],
    ))
    with patch("celerp_ai.routes.run_agent", mocked):
        r = await c.post(
            f"/ai/conversations/{conv_id}/query", headers=h,
            json={"query": "review these together", "file_ids": [jpg, csv]},
        )
    assert r.status_code == 200, r.text
    assert mocked.await_args.kwargs["file_ids"] == [jpg, csv]
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_query_with_images_creates_job(auth_client):
    """Images go to a reading job: 202 with the job id, the job listed on the thread."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    ids = [await _upload(c, h, f"r{i}.jpg", b"fake jpeg", "image/jpeg") for i in range(2)]
    run_batch = AsyncMock()
    with patch("celerp_ai.routes.run_batch", run_batch), \
         patch("celerp_ai.routes.run_agent", AsyncMock(side_effect=AssertionError("agent must not run"))):
        r = await c.post(
            f"/ai/conversations/{conv_id}/query", headers=h,
            json={"query": "", "file_ids": ids, "document_mode": "receipts"},
        )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert run_batch.await_count == 1
    assert list(run_batch.await_args.args[4]) == ids

    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert [m["role"] for m in thread["messages"]] == ["user"]
    assert thread["messages"][0]["file_ids"] == ids
    assert [j["id"] for j in thread["jobs"]] == [job_id]
    assert thread["jobs"][0]["status"] == "pending"
    assert thread["jobs"][0]["total_files"] == 2

    status = (await c.get(f"/ai/batch/{job_id}", headers=h)).json()
    assert status["conversation_id"] == conv_id
    assert status["proposal_message_id"] is None


# ── Bill proposals from a finished job ───────────────────────────────────────

_RECEIPT = {
    "document_kind": "receipt", "vendor_name": "Acme Supplies", "date": "2026-09-14",
    "currency": "USD", "total": 27.5, "tax": 2.5, "reference": "R-1001",
    "line_items": [
        {"description": "Paper A4", "quantity": 2, "unit_price": 10},
        {"description": "Stapler", "quantity": 1, "unit_price": 5},
    ],
}


async def _finished_job(session, c, h, conv_id, files):
    """A completed reading job on the conversation, with the given per-file results."""
    from celerp.ai.batch import create_batch_job
    from celerp.models.ai import AIConversation
    conv = await session.get(AIConversation, uuid.UUID(conv_id))
    job = await create_batch_job(
        session, conv.company_id, conv.user_id, "", [f["file_id"] for f in files],
        conversation_id=conv.id,
    )
    job.status = "completed"
    job.completed_files = len(files)
    job.results = {"files": files}
    await session.commit()
    return str(job.id)


def _capabilities(*names):
    return {n: {"name": n} for n in names}


def _executor(contacts=(), items=()):
    """An execute_agent_capability stand-in answering list lookups from fixtures."""
    calls = []

    async def _exec(app, authorization, capability, arguments, tool_call_id, **kw):
        calls.append((capability["name"], arguments))
        name = capability["name"]
        if name == "list_contacts_crm_contacts_get":
            return {"ok": True, "status": 200, "data": {"items": list(contacts), "total": len(contacts)}}
        if name == "list_items_items_get":
            q = arguments["query"]
            rows = [i for i in items if i.get("sku") == q.get("sku") or q.get("q")]
            return {"ok": True, "status": 200, "data": {"items": rows, "total": len(rows)}}
        return {"ok": True, "status": 201, "data": {"id": "created"}, "error": None}
    _exec.calls = calls
    return _exec


@pytest.mark.asyncio
async def test_proposals_idempotent_and_vendor_resolved(auth_client, session):
    """A known vendor becomes contact_id, a matched line carries item_id, and a
    second call returns the same proposals without rebuilding them."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    job_id = await _finished_job(session, c, h, conv_id, [
        {"file_id": "ai_up_1", "filename": "r1.jpg", "status": "success", "answer": "", "extraction": _RECEIPT, "credits": 1},
        {"file_id": "ai_up_2", "filename": "r2.jpg", "status": "error", "error": "The file could not be read."},
    ])
    caps = _capabilities(
        "create_doc_docs_post", "list_contacts_crm_contacts_get",
        "create_contact_crm_contacts_post", "list_items_items_get",
    )
    executor = _executor(
        contacts=[{"id": "c-1", "name": "acme supplies", "contact_type": "vendor"}],
        items=[{"id": "i-1", "name": "Paper A4", "sku": "PAP-A4"}],
    )
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value=caps), \
         patch("celerp_ai.routes.execute_agent_capability", executor):
        r = await c.post(f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h)
        assert r.status_code == 200, r.text
        first = r.json()
        r2 = await c.post(f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h)
    assert r2.status_code == 200
    assert r2.json() == first

    actions = first["pending_actions"]
    assert [a["name"] for a in actions] == ["create_doc_docs_post"]
    bill = actions[0]["arguments"]["body"]
    assert bill["doc_type"] == "bill"
    assert bill["contact_id"] == "c-1"
    assert bill["contact_name"] == "Acme Supplies"
    assert bill["issue_date"] == "2026-09-14"
    assert bill["currency"] == "USD"
    assert bill["total"] == 27.5 and bill["tax"] == 2.5 and bill["subtotal"] == 25.0
    assert bill["line_items"][0]["item_id"] == "i-1"
    assert "item_id" not in bill["line_items"][1]
    assert "R-1001" in bill["notes"] and "ai_up_1" in bill["notes"]
    assert actions[0]["warnings"] == []
    assert actions[0]["title"] == "Create bill from Acme Supplies"
    assert actions[0]["message_id"] == first["message_id"]
    assert "r2.jpg: could not be read." in first["answer"]
    # No write ran while proposing: every capability call was a list lookup.
    assert all(n.startswith("list_") for n, _ in executor.calls)

    status = (await c.get(f"/ai/batch/{job_id}", headers=h)).json()
    assert status["proposal_message_id"] == first["message_id"]


@pytest.mark.asyncio
async def test_proposal_flags_total_mismatch(auth_client, session):
    """An unknown vendor becomes a vendor proposal followed by a bound draft bill;
    total mismatch, missing date and a non-receipt file are all still surfaced."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    receipt = {**_RECEIPT, "vendor_name": "New Vendor", "total": 30, "date": None}
    job_id = await _finished_job(session, c, h, conv_id, [
        {"file_id": "ai_up_1", "filename": "r1.jpg", "status": "success", "answer": "", "extraction": receipt, "credits": 1},
        {"file_id": "ai_up_3", "filename": "photo.jpg", "status": "success", "answer": "", "extraction": {"document_kind": "other"}, "credits": 1},
    ])
    caps = _capabilities(
        "create_doc_docs_post", "list_contacts_crm_contacts_get",
        "create_contact_crm_contacts_post",
    )
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value=caps), \
         patch("celerp_ai.routes.execute_agent_capability", _executor()):
        r = await c.post(f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h)
    assert r.status_code == 200, r.text
    actions = r.json()["pending_actions"]
    assert [a["name"] for a in actions] == [
        "create_contact_crm_contacts_post", "create_doc_docs_post",
    ]
    vendor, bill_action = actions
    assert vendor["arguments"]["body"] == {"name": "New Vendor", "contact_type": "vendor"}
    bill = bill_action["arguments"]["body"]
    assert "contact_id" not in bill and bill["contact_name"] == "New Vendor"
    assert bill_action["bindings"] == [{
        "source_action_id": vendor["id"],
        "source_result_key": "id",
        "target_path": ["body", "contact_id"],
    }]
    assert "issue_date" not in bill
    warnings = bill_action["warnings"]
    assert any("27.50" in w and "30.00" in w for w in warnings)
    assert any("No date" in w for w in warnings)
    assert "photo.jpg: read as a other" in r.json()["answer"]


@pytest.mark.asyncio
async def test_unknown_vendor_dependency_resolves_across_confirmations(auth_client, session):
    """The bill cannot run before its vendor, then resolves the persisted vendor id
    after that canonical create succeeds, including across separate confirm calls."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    receipt = {**_RECEIPT, "vendor_name": "New Vendor"}
    job_id = await _finished_job(session, c, h, conv_id, [
        {"file_id": "ai_up_1", "filename": "r1.jpg", "status": "success",
         "answer": "", "extraction": receipt, "credits": 1},
    ])
    caps = _capabilities(
        "create_doc_docs_post", "list_contacts_crm_contacts_get",
        "create_contact_crm_contacts_post",
    )
    calls = []

    async def _exec(app, authorization, capability, arguments, tool_call_id, **kw):
        calls.append((capability["name"], arguments, tool_call_id))
        if capability["name"] == "list_contacts_crm_contacts_get":
            return {"ok": True, "status": 200, "data": {"items": [], "total": 0}}
        if capability["name"] == "create_contact_crm_contacts_post":
            return {"ok": True, "status": 201, "data": {"id": "contact:new-vendor"}}
        if capability["name"] == "create_doc_docs_post":
            return {"ok": True, "status": 201, "data": {"id": "doc:bill-1"}}
        raise AssertionError(capability["name"])

    with patch("celerp_ai.routes.compile_agent_capabilities", return_value=caps), \
         patch("celerp_ai.routes.execute_agent_capability", _exec):
        proposed = (await c.post(
            f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h,
        )).json()
        vendor, bill = proposed["pending_actions"]
        # Out-of-order single confirmation is non-terminal: the bill remains pending.
        early = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": proposed["message_id"], "tool_call_id": bill["id"]},
        )
        assert early.status_code == 200
        assert early.json()["action_status"] == "pending"
        assert early.json()["error"]["code"] == "action_dependency_not_ready"
        vendor_done = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": proposed["message_id"], "tool_call_id": vendor["id"]},
        )
        assert vendor_done.status_code == 200
        bill_done = await c.post(
            f"/ai/conversations/{conv_id}/confirm", headers=h,
            json={"message_id": proposed["message_id"], "tool_call_id": bill["id"]},
        )
        assert bill_done.status_code == 200

    writes = [(name, args) for name, args, _ in calls if not name.startswith("list_")]
    assert [name for name, _ in writes] == [
        "create_contact_crm_contacts_post", "create_doc_docs_post",
    ]
    assert writes[1][1]["body"]["contact_id"] == "contact:new-vendor"
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert thread["messages"][-1]["pending_actions"] == []


@pytest.mark.asyncio
async def test_proposals_require_finished_job_and_documents(auth_client, session):
    """A running job is 409 job_not_finished; a foreign job is 404; no documents module is 409."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    job_id = await _finished_job(session, c, h, conv_id, [
        {"file_id": "ai_up_1", "filename": "r1.jpg", "status": "success", "answer": "", "extraction": _RECEIPT, "credits": 1},
    ])
    from celerp.models.ai import AIBatchJob
    job = await session.get(AIBatchJob, uuid.UUID(job_id))
    job.status = "running"
    await session.commit()
    r = await c.post(f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "job_not_finished"

    job.status = "completed"
    await session.commit()
    other = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    r = await c.post(f"/ai/conversations/{other}/jobs/{job_id}/proposals", headers=h)
    assert r.status_code == 404

    with patch("celerp_ai.routes.compile_agent_capabilities", return_value={}):
        r = await c.post(f"/ai/conversations/{conv_id}/jobs/{job_id}/proposals", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "capability_unavailable"


@pytest.mark.asyncio
async def test_dismiss_all_is_atomic_and_idempotent(auth_client):
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    result = AgentResult(
        answer="Three changes.", model_used="glm", tools_called=[],
        pending_actions=[_pending(call_id="call_a"), _pending(call_id="call_b"), _pending(call_id="call_c")],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        created = await c.post(
            f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "do all"},
        )
    message_id = created.json()["pending_actions"][0]["message_id"]
    url = f"/ai/conversations/{conv_id}/dismiss-all"
    first = await c.post(url, headers=h, json={
        "message_id": message_id, "tool_call_ids": ["call_a", "call_c"],
    })
    again = await c.post(url, headers=h, json={
        "message_id": message_id, "tool_call_ids": ["call_a", "call_c"],
    })
    assert first.status_code == 200
    assert first.json()["dismissed"] == ["call_a", "call_c"]
    assert again.status_code == 200 and again.json()["dismissed"] == []
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert [a["id"] for a in thread["messages"][-1]["pending_actions"]] == ["call_b"]


# ── POST /ai/conversations/{id}/confirm-all ──────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_all_executes_in_order(auth_client):
    """Every pending action on the message runs in proposal order; one failure
    does not stop the others, and a second call finds nothing pending."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    result = AgentResult(
        answer="Two changes.", model_used="glm", tools_called=[],
        pending_actions=[_pending(name="create_contact", call_id="call_a"),
                         _pending(name="create_doc", call_id="call_b")],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "do both"})
    message_id = r.json()["pending_actions"][0]["message_id"]

    from celerp_ai.routes import _confirmed_action_identity
    expected_a = _confirmed_action_identity(uuid.UUID(message_id), "call_a")
    expected_b = _confirmed_action_identity(uuid.UUID(message_id), "call_b")
    seen = []

    async def _exec(app, authorization, capability, arguments, tool_call_id, **kw):
        seen.append(tool_call_id)
        if tool_call_id == expected_a:
            return {"ok": False, "status": 422, "error": {"code": "invalid", "message": "Name is required."}}
        return {"ok": True, "status": 201, "data": {"id": "doc-1"}}

    caps = {"create_contact": {"method": "POST"}, "create_doc": {"method": "POST"}}
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value=caps), \
         patch("celerp_ai.routes.execute_agent_capability", _exec):
        r = await c.post(f"/ai/conversations/{conv_id}/confirm-all", headers=h, json={"message_id": message_id})
        assert r.status_code == 200, r.text
        body = r.json()
        r2 = await c.post(f"/ai/conversations/{conv_id}/confirm-all", headers=h, json={"message_id": message_id})
    assert seen == [expected_a, expected_b]
    assert body["completed"] == 1 and body["failed"] == 1
    assert [x["tool_call_id"] for x in body["results"]] == ["call_a", "call_b"]
    assert body["results"][0]["error"]["message"] == "Name is required."
    assert body["results"][1]["data"] == {"id": "doc-1"}
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "action_not_pending"
    thread = (await c.get(f"/ai/conversations/{conv_id}", headers=h)).json()
    assert thread["messages"][1]["pending_actions"] == []


@pytest.mark.asyncio
async def test_confirm_all_selection_runs_only_selected_in_order(auth_client):
    """A selection runs only those ids, in proposal order; an id that is not
    pending is reported as a failed row without stopping the others; a
    selection with nothing pending is 409."""
    c, h = auth_client
    conv_id = (await c.post("/ai/conversations", headers=h, json={"title": None})).json()["id"]
    result = AgentResult(
        answer="Three changes.", model_used="glm", tools_called=[],
        pending_actions=[_pending(call_id="call_a"), _pending(call_id="call_b"), _pending(call_id="call_c")],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        r = await c.post(f"/ai/conversations/{conv_id}/query", headers=h, json={"query": "do all"})
    message_id = r.json()["pending_actions"][0]["message_id"]

    from celerp_ai.routes import _confirmed_action_identity
    expected_a = _confirmed_action_identity(uuid.UUID(message_id), "call_a")
    expected_b = _confirmed_action_identity(uuid.UUID(message_id), "call_b")
    expected_c = _confirmed_action_identity(uuid.UUID(message_id), "call_c")
    seen = []

    async def _exec(app, authorization, capability, arguments, tool_call_id, **kw):
        seen.append(tool_call_id)
        return {"ok": True, "status": 201, "data": {"id": f"doc-{tool_call_id}"}}

    caps = {"create_contact": {"method": "POST"}}
    url = f"/ai/conversations/{conv_id}/confirm-all"
    with patch("celerp_ai.routes.compile_agent_capabilities", return_value=caps), \
         patch("celerp_ai.routes.execute_agent_capability", _exec):
        r = await c.post(url, headers=h, json={"message_id": message_id, "tool_call_ids": ["call_c", "call_a", "ghost"]})
        assert r.status_code == 200, r.text
        body = r.json()
        r2 = await c.post(url, headers=h, json={"message_id": message_id, "tool_call_ids": ["call_b"]})
        r3 = await c.post(url, headers=h, json={"message_id": message_id, "tool_call_ids": ["ghost"]})
    assert seen == [expected_a, expected_c, expected_b]
    assert [x["tool_call_id"] for x in body["results"]] == ["call_a", "call_c", "ghost"]
    assert body["results"][0]["name"] == "create_contact"
    assert body["results"][2]["ok"] is False
    assert body["results"][2]["error"]["code"] == "action_not_pending"
    assert body["completed"] == 2 and body["failed"] == 1
    assert r2.status_code == 200 and r2.json()["completed"] == 1
    assert r3.status_code == 409 and r3.json()["detail"]["code"] == "action_not_pending"


@pytest.mark.asyncio
async def test_list_conversations_reports_pending_count(auth_client):
    """The list carries how many proposals each conversation still has open."""
    c, h = auth_client
    quiet = (await c.post("/ai/conversations", headers=h, json={"title": "quiet"})).json()["id"]
    busy = (await c.post("/ai/conversations", headers=h, json={"title": "busy"})).json()["id"]
    result = AgentResult(
        answer="Two changes.", model_used="glm", tools_called=[],
        pending_actions=[_pending(call_id="call_a"), _pending(call_id="call_b")],
    )
    with patch("celerp_ai.routes.run_agent", AsyncMock(return_value=result)):
        await c.post(f"/ai/conversations/{busy}/query", headers=h, json={"query": "do both"})
    counts = {x["id"]: x["pending_count"] for x in (await c.get("/ai/conversations", headers=h)).json()}
    assert counts[busy] == 2 and counts[quiet] == 0
