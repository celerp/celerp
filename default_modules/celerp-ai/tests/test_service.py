# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/service.py - query orchestration.

Model routing and LLM calls are tested in test_models.py and test_llm.py.
This file tests:
  - _sanitize_error: all branches
  - _select_tools: LLM-driven tool selection
  - run_query: happy path, memory inclusion, API error, tool failure, timeout
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.models.base import Base
from celerp.models.company import Company
from celerp.ai.service import (
    AIResponse,
    _sanitize_error,
    _select_tools,
    run_query,
)
from celerp.ai.models import TEXT_QUERY, COMPLEX
from celerp.ai import memory as ai_memory

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
    c = Company(name="AICo", slug="aico", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


# -- _sanitize_error --------------------------------------------------------

def test_sanitize_error_api_key():
    msg = _sanitize_error(RuntimeError("OPENROUTER_API_KEY is not configured."))
    assert "OPENROUTER" not in msg
    assert "contact support" in msg.lower()


def test_sanitize_error_rate_limit():
    msg = _sanitize_error(RuntimeError("LLM rate limit exceeded after 3 retries."))
    assert "busy" in msg.lower()


def test_sanitize_error_timeout():
    msg = _sanitize_error(RuntimeError("connection timeout"))
    assert "took too long" in msg.lower()


def test_sanitize_error_llm_api():
    msg = _sanitize_error(RuntimeError('LLM API error 500: {"type":"overloaded_error"}'))
    assert "overloaded_error" not in msg


def test_sanitize_error_generic():
    msg = _sanitize_error(RuntimeError("some random internal error"))
    assert "random internal error" not in msg
    assert len(msg) > 0


# -- _select_tools (async, LLM-driven) -------------------------------------

@pytest.mark.asyncio
async def test_select_tools_llm_picks_tools():
    """LLM returns relevant tool names."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value='["low_stock_items"]'):
        tools = await _select_tools("show me low stock items")
    assert "low_stock_items" in tools


@pytest.mark.asyncio
async def test_select_tools_llm_multiple():
    """LLM can return multiple tools (up to 4)."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value='["outstanding_invoices", "low_stock_items", "dashboard_kpis"]'):
        tools = await _select_tools("compare invoices and stock")
    assert len(tools) == 3


@pytest.mark.asyncio
async def test_select_tools_llm_failure_fallback():
    """On LLM failure, falls back to dashboard_kpis."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("network error")):
        tools = await _select_tools("anything")
    assert "dashboard_kpis" in tools


@pytest.mark.asyncio
async def test_select_tools_files_always_get_contacts_items():
    """File queries always include contacts and items."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value='["dashboard_kpis"]'):
        tools = await _select_tools("process these receipts", has_files=True)
    assert "active_contacts_list" in tools
    assert "active_items_list" in tools


@pytest.mark.asyncio
async def test_select_tools_validates_names():
    """Invalid tool names from LLM are filtered out."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value='["fake_tool", "low_stock_items"]'):
        tools = await _select_tools("inventory check")
    assert "low_stock_items" in tools
    assert "fake_tool" not in tools


@pytest.mark.asyncio
async def test_select_tools_max_four():
    """Cap at 4 tools even if LLM returns more."""
    with patch("celerp.ai.service.call_llm", new_callable=AsyncMock,
               return_value='["dashboard_kpis", "low_stock_items", "outstanding_invoices", "top_items_by_value", "active_deals_summary"]'):
        tools = await _select_tools("everything")
    assert len(tools) <= 4


# -- run_query --------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_query_happy_path(session, company):
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value="You have 3 items."):
            result = await run_query("how many items", session, company.id)

    assert result.error is None
    assert result.answer == "You have 3 items."
    assert result.model_used == TEXT_QUERY


@pytest.mark.asyncio
async def test_run_query_complex_model(session, company):
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value="Here's my analysis."):
            result = await run_query("analyse cash flow trends", session, company.id)

    assert result.model_used == COMPLEX


@pytest.mark.asyncio
async def test_run_query_with_memory(session, company):
    await ai_memory.add_note(session, company.id, "Preferred supplier: ABC Co")
    await ai_memory.set_kv(session, company.id, "currency", "THB")
    await session.commit()

    captured_args: dict = {}

    async def mock_call_llm(model, system, user_text, files=None, max_tokens=2048, history=None, timeout=45.0):
        captured_args["user_text"] = user_text
        return "Answer with memory."

    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", side_effect=mock_call_llm):
            result = await run_query("give me a business overview", session, company.id)

    assert "Preferred supplier: ABC Co" in captured_args["user_text"]
    assert "currency: THB" in captured_args["user_text"]


@pytest.mark.asyncio
async def test_run_query_api_error_captured(session, company):
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("timeout")):
            result = await run_query("how many items", session, company.id)

    assert result.error is not None
    assert "timeout" not in result.error
    assert result.answer == ""


@pytest.mark.asyncio
async def test_run_query_tool_failure_graceful(session, company):
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["low_stock_items"]):
        with patch("celerp.ai.service.execute_tool", new_callable=AsyncMock, side_effect=Exception("DB error")):
            with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value="Could not fetch data."):
                result = await run_query("low stock items", session, company.id)

    assert result.error is None
    assert result.tools_called == []


@pytest.mark.asyncio
async def test_run_query_timeout(session, company):
    """Query that exceeds timeout returns friendly error."""
    async def slow_llm(*args, **kwargs):
        await asyncio.sleep(100)
        return "never"

    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", side_effect=slow_llm):
            # Override the timeout to something very short for testing
            with patch("celerp.ai.service.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await run_query("test", session, company.id)

    assert result.error is not None
    assert "too long" in result.error.lower()


@pytest.mark.asyncio
async def test_run_query_pending_bills(session, company):
    """LLM output with JSON block returns pending_bills instead of executing."""
    llm_answer = (
        "Here are the bills:\n"
        '```json\n{"create_draft_bills": [{"vendor_name": "Acme", "date": "2026-01-01", '
        '"total": 100.0, "source_file_id": "f1", '
        '"line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0}]}]}\n```'
    )
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value=llm_answer):
            result = await run_query("process receipt", session, company.id)

    assert result.error is None
    assert result.pending_bills is not None
    assert len(result.pending_bills) == 1
    assert result.pending_bills[0]["vendor_name"] == "Acme"
    # JSON block should be stripped from answer
    assert "```json" not in result.answer


@pytest.mark.asyncio
async def test_run_query_invalid_bill_json(session, company):
    """Invalid JSON in code block doesn't crash, pending_bills is None."""
    llm_answer = "Bills:\n```json\n{invalid json}\n```"
    with patch("celerp.ai.service._select_tools", new_callable=AsyncMock, return_value=["dashboard_kpis"]):
        with patch("celerp.ai.service.call_llm", new_callable=AsyncMock, return_value=llm_answer):
            result = await run_query("process receipts", session, company.id)
    assert result.error is None
    assert result.pending_bills is None
