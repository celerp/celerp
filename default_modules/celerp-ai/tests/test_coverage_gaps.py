# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fill coverage gaps across AI module.

Covers:
  - files.py: load_file, load_file_for_llm (valid, missing, wrong company)
  - cleanup.py: _delete_file_pair OSError, cleanup stat OSError, orphan OSError, run_cleanup_loop
  - batch.py: _process_single_file wrong company, notification failure
  - llm.py: history injection through the gateway
  - page_count.py: PDF 0 pages
  - quota.py: get_quota_status branches
  - conversations.py: rename not found
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio

from celerp.config import settings
from celerp.models.company import Company




# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="TestCo", slug="testco", settings={"currency": "USD"})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


# ── files.py: load_file, load_file_for_llm ──────────────────────────────────

def _upload_id(digit: str) -> str:
    return f"ai_up_{digit * 32}"


def test_load_file_valid(tmp_path):
    """Load a file that exists and belongs to the right company."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("1")
    (upload_dir / f"{fid}.bin").write_bytes(b"\x89PNG\r\n")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/png", "company_id": str(co_id),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        data, meta = load_file(fid, co_id)
    assert data == b"\x89PNG\r\n"
    assert meta["content_type"] == "image/png"


def test_load_file_missing(tmp_path):
    """Missing file raises FileNotFoundError."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(FileNotFoundError):
            load_file("ai_up_nonexistent", co_id)


def test_load_file_rejects_path_traversal(tmp_path):
    """Caller-controlled file ids cannot escape the transient upload directory."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    (tmp_path / "escape.bin").write_bytes(b"secret")
    (tmp_path / "escape.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(co_id),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(FileNotFoundError):
            load_file("../escape", co_id)


def test_load_file_wrong_company(tmp_path):
    """File belonging to different company raises PermissionError."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    other_co = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("2")
    (upload_dir / f"{fid}.bin").write_bytes(b"data")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(other_co),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(PermissionError):
            load_file(fid, co_id)


def test_load_file_for_llm_valid(tmp_path):
    """load_file_for_llm returns base64 dict."""
    from celerp.ai.files import load_file_for_llm
    co_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("3")
    (upload_dir / f"{fid}.bin").write_bytes(b"\x89PNG\r\n")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/png", "company_id": str(co_id),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        result = load_file_for_llm(fid, co_id)
    assert result["media_type"] == "image/png"
    assert len(result["data"]) > 0  # base64


def test_load_tabular_for_llm_does_not_base64_body(tmp_path):
    """CSV is represented by file metadata only; local tools read its bytes on demand."""
    from celerp.ai.files import load_file_for_llm
    co_id = uuid.uuid4()
    user_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("4")
    (upload_dir / f"{fid}.bin").write_bytes(b"sku,name\nA,Alpha\n")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "text/csv", "company_id": str(co_id),
        "user_id": str(user_id), "filename": "catalog.csv",
    }))
    with patch.object(settings, "data_dir", tmp_path):
        result = load_file_for_llm(fid, co_id, user_id)
    assert result == {
        "media_type": "text/csv", "data": "", "filename": "catalog.csv", "file_id": fid,
    }


def test_load_file_wrong_user(tmp_path):
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("5")
    (upload_dir / f"{fid}.bin").write_bytes(b"data")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(co_id), "user_id": str(owner_id),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(PermissionError):
            load_file(fid, co_id, uuid.uuid4())


# ── cleanup.py: edge cases ───────────────────────────────────────────────────

def test_cleanup_delete_file_pair_oserror(tmp_path):
    """OSError in _delete_file_pair is caught and logged."""
    from celerp.ai.cleanup import _delete_file_pair
    meta = tmp_path / "test.meta"
    meta.write_text("{}")
    with patch("pathlib.Path.unlink", side_effect=OSError("mock delete error")):
        _delete_file_pair(meta)  # Should not raise


def test_cleanup_stat_oserror(tmp_path):
    """OSError on stat is handled (continue)."""
    from celerp.ai.cleanup import cleanup_uploads
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    meta = upload_dir / "ai_up_broken.meta"
    meta.write_text("{}")
    with patch.object(settings, "data_dir", tmp_path):
        with patch("pathlib.Path.stat", side_effect=OSError("mock stat error")):
            deleted = cleanup_uploads()
    assert deleted == 0


def test_cleanup_orphan_bin_oserror(tmp_path):
    """Orphan .bin that can't be deleted is handled gracefully."""
    from celerp.ai.cleanup import cleanup_uploads
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    orphan = upload_dir / "ai_up_orphan.bin"
    orphan.write_bytes(b"orphan data")
    original_unlink = Path.unlink

    def selective_unlink(self, *args, **kwargs):
        if self.name.endswith(".bin"):
            raise OSError("mock delete error")
        return original_unlink(self, *args, **kwargs)

    with patch.object(settings, "data_dir", tmp_path):
        with patch.object(Path, "unlink", selective_unlink):
            deleted = cleanup_uploads()
    assert deleted == 0


@pytest.mark.asyncio
async def test_run_cleanup_loop_one_iteration():
    """run_cleanup_loop calls cleanup_uploads after sleep."""
    from celerp.ai.cleanup import run_cleanup_loop

    call_count = 0

    def mock_cleanup(**kwargs):
        nonlocal call_count
        call_count += 1
        raise KeyboardInterrupt

    with patch("celerp.ai.cleanup.cleanup_uploads", side_effect=mock_cleanup):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(KeyboardInterrupt):
                await run_cleanup_loop()
    assert call_count == 1


@pytest.mark.asyncio
async def test_run_cleanup_loop_exception_continues():
    """cleanup_uploads exception is caught; loop continues."""
    from celerp.ai.cleanup import run_cleanup_loop

    calls = []

    def mock_cleanup(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk error")
        raise KeyboardInterrupt

    with patch("celerp.ai.cleanup.cleanup_uploads", side_effect=mock_cleanup):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(KeyboardInterrupt):
                await run_cleanup_loop()
    assert len(calls) == 2


# ── batch.py: _process_single_file wrong company ────────────────────────────

def test_batch_load_file_wrong_company(tmp_path):
    """batch load_file raises PermissionError for wrong company."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    other = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = _upload_id("6")
    (upload_dir / f"{fid}.bin").write_bytes(b"data")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(other),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(PermissionError):
            load_file(fid, co_id)


# ── llm.py: history injection through the gateway ────────────────────────────

@pytest.mark.asyncio
async def test_call_llm_with_history():
    """History messages are injected between system and user."""
    from celerp.ai.llm import call_llm

    captured = {}

    async def mock_post(url, json=None, **kw):
        captured["messages"] = json["messages"]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"message": {"role": "assistant", "content": "result"}}
        return resp

    with patch("celerp.ai.llm.relay_session_headers", return_value={"X-Session-Token": "s", "X-Instance-ID": "i"}):
        with patch("celerp.ai.llm.relay_http_url", return_value="https://relay"):
            with patch("httpx.AsyncClient.post", side_effect=mock_post):
                result = await call_llm(
                    "test-model", "system prompt", "user question",
                    history=[{"role": "user", "content": "prior"}, {"role": "assistant", "content": "reply"}],
                )

    assert result.message["content"] == "result"
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


# ── page_count.py: PDF 0 pages ──────────────────────────────────────────────

def test_count_pages_pdf_zero_pages():
    """PDF with 0 pages raises ValueError."""
    from celerp.ai.page_count import count_pages

    mock_reader = MagicMock()
    mock_reader.pages = []

    mock_pypdf = MagicMock()
    mock_pypdf.PdfReader.return_value = mock_reader

    with patch.dict("sys.modules", {"pypdf": mock_pypdf}):
        with pytest.raises(ValueError, match="0 pages"):
            count_pages(b"fake pdf", "application/pdf")


# ── quota.py: get_quota_status ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quota_status_no_gateway():
    from celerp.ai.quota import get_quota_status
    with patch.object(settings, "gateway_token", ""):
        result = await get_quota_status()
    assert result is None


@pytest.mark.asyncio
async def test_get_quota_status_no_instance_id():
    from celerp.ai.quota import get_quota_status
    with patch.object(settings, "gateway_token", "tok"):
        with patch("celerp.ai.quota.get_session_token", return_value="sess"):
            with patch.object(settings, "gateway_instance_id", ""):
                result = await get_quota_status()
    assert result is None


# ── conversations.py: rename not found ───────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_conversation_not_found(session, company):
    from celerp.ai.conversations import rename_conversation
    result = await rename_conversation(session, uuid.uuid4(), company.id, uuid.uuid4(), "new title")
    assert result is None


# ── proposal refresh / confirmation identity regressions ─────────────────────

def test_refresh_expired_proposals_rebinds_dependency_ids():
    from celerp_ai.routes import _refresh_expired_proposals

    now = datetime.now(timezone.utc)
    expired = (now - timedelta(seconds=1)).isoformat()
    records = [
        {
            "id": "vendor-old", "name": "create_contact", "arguments": {},
            "created_at": expired, "expires_at": expired,
        },
        {
            "id": "bill-old", "name": "create_bill", "arguments": {"body": {}},
            "created_at": expired, "expires_at": expired,
            "bindings": [{
                "source_action_id": "vendor-old", "source_result_key": "id",
                "target_path": ["body", "contact_id"],
            }],
        },
    ]

    refreshed = _refresh_expired_proposals(records, now)
    assert len(refreshed) == 2
    assert refreshed[0]["id"] != "vendor-old"
    assert refreshed[1]["id"] != "bill-old"
    assert refreshed[1]["bindings"][0]["source_action_id"] == refreshed[0]["id"]


def test_refresh_expired_proposals_keeps_completed_dependency_context():
    from celerp_ai.routes import _refresh_expired_proposals, _resolve_action_arguments

    now = datetime.now(timezone.utc)
    expired = (now - timedelta(seconds=1)).isoformat()
    records = [
        {
            "id": "vendor-done", "name": "create_contact", "arguments": {},
            "status": "completed", "result_summary": {"id": "contact:123"},
            "created_at": expired, "expires_at": expired,
        },
        {
            "id": "bill-old", "name": "create_bill", "arguments": {"body": {}},
            "created_at": expired, "expires_at": expired,
            "bindings": [{
                "source_action_id": "vendor-done", "source_result_key": "id",
                "target_path": ["body", "contact_id"],
            }],
        },
    ]

    refreshed = _refresh_expired_proposals(records, now)
    source = next(r for r in refreshed if r["id"] == "vendor-done")
    bill = next(r for r in refreshed if r["name"] == "create_bill")
    assert source["status"] == "completed"
    arguments, error = _resolve_action_arguments(refreshed, bill)
    assert error is None
    assert arguments["body"]["contact_id"] == "contact:123"


def test_confirmed_action_identity_is_stable_and_message_scoped():
    from celerp_ai.routes import _confirmed_action_identity

    first = uuid.uuid4()
    second = uuid.uuid4()
    assert _confirmed_action_identity(first, "call_1") == _confirmed_action_identity(first, "call_1")
    assert _confirmed_action_identity(first, "call_1") != _confirmed_action_identity(second, "call_1")
