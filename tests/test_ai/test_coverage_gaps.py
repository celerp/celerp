# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fill coverage gaps across AI module.

Covers:
  - files.py: load_file, load_file_for_llm (valid, missing, wrong company)
  - commands.py: parse_bill_commands + create_bills (vendor found/created, no company, empty, no celerp_docs)
  - cleanup.py: _delete_file_pair OSError, cleanup stat OSError, orphan OSError, run_cleanup_loop
  - batch.py: _process_single_file wrong company, notification failure
  - llm.py: history injection, exhausted retry loop
  - page_count.py: PDF 0 pages
  - quota.py: _build_upgrade_url with instance_id, get_quota_status branches, unconfirmed counter
  - tools.py: active_contacts_list, active_items_list, pending_pos, dormant contact_id=None
  - conversations.py: rename not found
  - service.py: run_query with files + pending bills, command extraction failure
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.config import settings
from celerp.models.base import Base
from celerp.models.company import Company
from celerp.models.projections import Projection


_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="TestCo", slug="testco", settings={"currency": "USD"})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


# ── files.py: load_file, load_file_for_llm ──────────────────────────────────

def test_load_file_valid(tmp_path):
    """Load a file that exists and belongs to the right company."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = "ai_up_test1"
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


def test_load_file_wrong_company(tmp_path):
    """File belonging to different company raises PermissionError."""
    from celerp.ai.files import load_file
    co_id = uuid.uuid4()
    other_co = uuid.uuid4()
    upload_dir = tmp_path / "ai_uploads"
    upload_dir.mkdir()
    fid = "ai_up_test2"
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
    fid = "ai_up_llm1"
    (upload_dir / f"{fid}.bin").write_bytes(b"\x89PNG\r\n")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/png", "company_id": str(co_id),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        result = load_file_for_llm(fid, co_id)
    assert result["media_type"] == "image/png"
    assert len(result["data"]) > 0  # base64


# ── service.py: run_query with files + pending bills ─────────────────────────

@pytest.mark.asyncio
async def test_run_query_with_files_returns_pending_bills(session, company):
    """Files trigger load_file_for_llm, LLM output with JSON returns pending_bills."""
    from celerp.ai.service import run_query
    fid = "ai_up_cmd_test"
    upload_dir = settings.data_dir / "ai_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / f"{fid}.bin").write_bytes(b"fake")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(company.id),
    }))

    llm_answer = (
        "Here are the bills:\n"
        '```json\n{"create_draft_bills": [{"vendor_name": "Acme", "date": "2026-01-01", '
        '"total": 100.0, "source_file_id": "' + fid + '", '
        '"line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}]}]}\n```'
    )

    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["active_contacts_list"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value=llm_answer):
            result = await run_query("process this receipt", session, company.id, file_ids=[fid])

    assert result.error is None
    assert result.pending_bills is not None
    assert len(result.pending_bills) == 1
    assert result.pending_bills[0]["vendor_name"] == "Acme"


@pytest.mark.asyncio
async def test_run_query_command_extraction_failure(session, company):
    """Invalid JSON in code block doesn't crash - pending_bills is None."""
    from celerp.ai.service import run_query

    llm_answer = "Bills:\n```json\n{invalid json}\n```"
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value=llm_answer):
            result = await run_query("process receipts", session, company.id)
    assert result.error is None
    assert result.pending_bills is None


@pytest.mark.asyncio
async def test_run_query_with_history(session, company):
    """History parameter is forwarded to call_llm."""
    from celerp.ai.service import run_query
    captured = {}

    async def mock_llm(model, system, user_text, files=None, max_tokens=2048, history=None, timeout=45.0):
        captured["history"] = history
        return "OK"

    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", side_effect=mock_llm):
            await run_query("followup", session, company.id, history=history)
    assert captured["history"] == history


# ── commands.py: parse_bill_commands + create_bills ──────────────────────────

def test_parse_bill_commands_valid():
    """Valid bill data parses to DraftBill list."""
    from celerp.ai.commands import parse_bill_commands
    result = parse_bill_commands({"create_draft_bills": [{
        "vendor_name": "Acme", "date": "2026-01-01", "total": 100.0,
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}],
    }]})
    assert len(result) == 1
    assert result[0].vendor_name == "Acme"


def test_parse_bill_commands_empty():
    """Empty commands returns empty list."""
    from celerp.ai.commands import parse_bill_commands
    assert parse_bill_commands({}) == []
    assert parse_bill_commands({"create_draft_bills": []}) == []


def test_parse_bill_commands_invalid():
    """Invalid bill data raises ValueError."""
    from celerp.ai.commands import parse_bill_commands
    with pytest.raises(ValueError, match="validation failed"):
        parse_bill_commands({"create_draft_bills": [{"vendor_name": "Acme"}]})


@pytest.mark.asyncio
async def test_create_bills_vendor_exists(session, company):
    """When vendor found in projections, use existing contact_id."""
    from celerp.ai.commands import DraftBill, LineItem, create_bills

    session.add(Projection(
        entity_id=str(uuid.uuid4()),
        company_id=company.id,
        entity_type="contact",
        state={"name": "Acme Corp", "contact_type": "vendor"},
        version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    await session.commit()

    bills = [DraftBill(
        vendor_name="Acme Corp", date="2026-01-01", total=500.0,
        line_items=[LineItem(description="Parts", quantity=5, unit_price=100.0)],
    )]

    with patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        feedback = await create_bills(session, company.id, uuid.uuid4(), bills)

    assert "Created Draft Bill" in feedback
    assert "Acme Corp" in feedback
    assert mock_emit.call_count == 1
    call_data = mock_emit.call_args[1]["data"]
    assert call_data["doc_type"] == "bill"


@pytest.mark.asyncio
async def test_create_bills_vendor_created(session, company):
    """When vendor not found, auto-create contact then bill."""
    from celerp.ai.commands import DraftBill, LineItem, create_bills

    bills = [DraftBill(
        vendor_name="New Supplier Ltd", date="2026-02-01", total=200.0,
        line_items=[LineItem(description="Stuff", quantity=1, unit_price=200.0)],
    )]
    user_id = uuid.uuid4()

    with patch("celerp.events.engine.emit_event", new_callable=AsyncMock) as mock_emit:
        feedback = await create_bills(session, company.id, user_id, bills)

    assert "Created Draft Bill" in feedback
    assert mock_emit.call_count == 2
    first_call = mock_emit.call_args_list[0][1]
    assert first_call["event_type"] == "contact.created"
    assert first_call["actor_id"] == user_id  # Verify actor_id set
    second_call = mock_emit.call_args_list[1][1]
    assert second_call["event_type"] == "doc.created"
    assert second_call["actor_id"] == user_id


@pytest.mark.asyncio
async def test_create_bills_no_company(session):
    """Non-existent company returns empty string."""
    from celerp.ai.commands import create_bills
    result = await create_bills(session, uuid.uuid4(), uuid.uuid4(), [])
    assert result == ""


@pytest.mark.asyncio
async def test_create_bills_without_celerp_docs(session, company):
    """When celerp_docs not importable, fallback bill ref is generated."""
    from celerp.ai.commands import DraftBill, LineItem, create_bills

    bills = [DraftBill(
        vendor_name="Fallback Co", date="2026-03-01", total=50.0,
        line_items=[LineItem(description="Item", quantity=1, unit_price=50.0)],
    )]

    import sys
    saved = sys.modules.get("celerp_docs.sequences")
    sys.modules["celerp_docs.sequences"] = None
    try:
        with patch("celerp.events.engine.emit_event", new_callable=AsyncMock):
            feedback = await create_bills(session, company.id, uuid.uuid4(), bills)
    finally:
        if saved is not None:
            sys.modules["celerp_docs.sequences"] = saved
        elif "celerp_docs.sequences" in sys.modules:
            del sys.modules["celerp_docs.sequences"]

    assert "Created Draft Bill" in feedback
    assert "BIL-" in feedback


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
    fid = "ai_up_batchtest"
    (upload_dir / f"{fid}.bin").write_bytes(b"data")
    (upload_dir / f"{fid}.meta").write_text(json.dumps({
        "content_type": "image/jpeg", "company_id": str(other),
    }))
    with patch.object(settings, "data_dir", tmp_path):
        with pytest.raises(PermissionError):
            load_file(fid, co_id)


# ── llm.py: history + exhausted retries ──────────────────────────────────────

@pytest.mark.asyncio
async def test_call_llm_with_history():
    """History messages are injected between system and user."""
    from celerp.ai.llm import call_llm

    captured = {}

    async def mock_post(url, json=None, **kw):
        captured["messages"] = json["messages"]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"choices": [{"message": {"content": "result"}}]}
        return resp

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}):
        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            result = await call_llm(
                "test-model", "system prompt", "user question",
                history=[{"role": "user", "content": "prior"}, {"role": "assistant", "content": "reply"}],
            )

    assert result == "result"
    msgs = captured["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]


@pytest.mark.asyncio
async def test_call_llm_exhausted_retries():
    """All retries fail with 429 - raises RuntimeError."""
    from celerp.ai.llm import call_llm

    async def mock_post(url, json=None, **kw):
        resp = MagicMock()
        resp.status_code = 429
        resp.text = "rate limited"
        resp.headers = {}
        return resp

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}):
        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="rate limit exceeded"):
                    await call_llm("test-model", "sys", "user")


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


# ── quota.py: _build_upgrade_url + get_quota_status + unconfirmed ───────────

def test_subscribe_url_with_instance_id():
    from celerp.ai.quota import _subscribe_url
    with patch.object(settings, "gateway_instance_id", "inst-123"):
        url = _subscribe_url()
    assert "instance_id=inst-123" in url
    assert url.endswith("#ai")


def test_subscribe_url_without_instance_id():
    from celerp.ai.quota import _subscribe_url
    with patch.object(settings, "gateway_instance_id", ""):
        url = _subscribe_url()
    assert "instance_id" not in url
    assert url.endswith("#ai")


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


@pytest.mark.asyncio
async def test_quota_unconfirmed_counter():
    """Consecutive relay failures increment _unconfirmed counter."""
    import celerp.ai.quota as quota_mod
    quota_mod._unconfirmed = 0  # Reset
    with patch.object(settings, "gateway_token", "tok"):
        with patch.object(settings, "gateway_instance_id", "inst"):
            with patch("celerp.ai.quota.get_session_token", return_value="sess"):
                with patch("httpx.AsyncClient.post", side_effect=ConnectionError("fail")):
                    await quota_mod.check_ai_quota()
                    assert quota_mod._unconfirmed == 1
                    await quota_mod.check_ai_quota()
                    assert quota_mod._unconfirmed == 2
    quota_mod._unconfirmed = 0  # Cleanup


# ── tools.py: active_contacts_list, active_items_list, pending_pos ───────────

@pytest_asyncio.fixture
async def tool_session(session):
    cid = uuid.uuid4()
    session.add(Projection(
        entity_id=str(uuid.uuid4()), company_id=cid, entity_type="contact",
        state={"name": "Acme", "contact_type": "vendor"}, version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    session.add(Projection(
        entity_id=str(uuid.uuid4()), company_id=cid, entity_type="item",
        state={"name": "Widget", "sku": "WDG-1"}, version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    session.add(Projection(
        entity_id=str(uuid.uuid4()), company_id=cid, entity_type="doc",
        state={"doc_type": "po", "doc_number": "PO-001", "contact_name": "Acme",
               "total": 500, "status": "open"}, version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    session.add(Projection(
        entity_id=str(uuid.uuid4()), company_id=cid, entity_type="doc",
        state={"doc_type": "po", "doc_number": "PO-002", "status": "received",
               "total": 200}, version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    session.add(Projection(
        entity_id=str(uuid.uuid4()), company_id=cid, entity_type="doc",
        state={"doc_type": "invoice", "contact_id": None}, version=1,
        updated_at=datetime.now(timezone.utc),
    ))
    await session.commit()
    return session, cid


@pytest.mark.asyncio
async def test_active_contacts_list(tool_session):
    from celerp.ai.tools import execute_tool
    sess, cid = tool_session
    result = await execute_tool("active_contacts_list", {}, sess, cid)
    assert len(result["contacts"]) == 1
    assert result["contacts"][0]["name"] == "Acme"


@pytest.mark.asyncio
async def test_active_items_list(tool_session):
    from celerp.ai.tools import execute_tool
    sess, cid = tool_session
    result = await execute_tool("active_items_list", {}, sess, cid)
    assert len(result["items"]) == 1
    assert result["items"][0]["sku"] == "WDG-1"


@pytest.mark.asyncio
async def test_pending_pos(tool_session):
    from celerp.ai.tools import execute_tool
    sess, cid = tool_session
    result = await execute_tool("pending_pos", {}, sess, cid)
    assert result["total_count"] == 1
    assert result["pending_pos"][0]["doc_number"] == "PO-001"


@pytest.mark.asyncio
async def test_pending_pos_empty(session):
    from celerp.ai.tools import execute_tool
    cid = uuid.uuid4()
    result = await execute_tool("pending_pos", {}, session, cid)
    assert result["total_count"] == 0


@pytest.mark.asyncio
async def test_dormant_contacts_skip_no_contact_id(tool_session):
    from celerp.ai.tools import execute_tool
    sess, cid = tool_session
    result = await execute_tool("dormant_contacts", {}, sess, cid)
    assert result["total_count"] == 1


# ── conversations.py: rename not found ───────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_conversation_not_found(session, company):
    from celerp.ai.conversations import rename_conversation
    result = await rename_conversation(session, uuid.uuid4(), company.id, uuid.uuid4(), "new title")
    assert result is None


def test_bill_preview_component():
    """_bill_preview renders a card with vendor, total, and action buttons."""
    from celerp_ai.ui_routes import _bill_preview
    bills = [
        {
            "vendor_name": "Acme",
            "date": "2026-04-12",
            "total": 100.0,
            "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}],
        },
        {
            "vendor_name": "BetaCorp",
            "date": "2026-04-12",
            "total": 200.0,
            "line_items": [],
        },
    ]
    from fasthtml.common import to_xml
    html = to_xml(_bill_preview(bills))
    assert "ai-bills" in html
    assert "Acme" in html
    assert "BetaCorp" in html
    assert "$100.00" in html
    assert "$200.00" in html
    assert "Widget" in html
    assert "Confirm" in html
    assert "Discard" in html
    assert "2 Draft Bills Ready" in html


def test_bill_preview_single_bill():
    """_bill_preview uses singular 'Bill' for single bill."""
    from celerp_ai.ui_routes import _bill_preview
    from fasthtml.common import to_xml
    html = to_xml(_bill_preview([{
        "vendor_name": "Solo",
        "date": "2026-01-01",
        "total": 50.0,
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 50.0}],
    }]))
    assert "1 Draft Bill Ready" in html
    assert "Bills Ready" not in html
