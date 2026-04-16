# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/intent.py"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest

from celerp.ai.intent import Intent, classify_intent, _keyword_match


# ── keyword match ────────────────────────────────────────────────────────────

def test_keyword_match_file_this():
    assert _keyword_match("file this invoice") == Intent.ROUTING


def test_keyword_match_save_this():
    assert _keyword_match("save this document") == Intent.ROUTING


def test_keyword_match_attach_this():
    assert _keyword_match("attach this to the order") == Intent.ROUTING


def test_keyword_match_store_this():
    assert _keyword_match("store this receipt") == Intent.ROUTING


def test_keyword_match_case_insensitive():
    assert _keyword_match("File This contract") == Intent.ROUTING


def test_keyword_no_match():
    assert _keyword_match("analyze these invoices") is None


def test_keyword_no_match_read():
    assert _keyword_match("what's in this contract?") is None


# ── classify_intent ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_files_always_comprehension():
    result = await classify_intent("file this", has_files=False)
    assert result == Intent.COMPREHENSION


@pytest.mark.asyncio
async def test_keyword_hit_skips_llm():
    # Should return ROUTING without calling LLM
    with patch("celerp.ai.intent.call_llm", new_callable=AsyncMock) as mock:
        result = await classify_intent("file this invoice", has_files=True)
    assert result == Intent.ROUTING
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_llm_fallback_routing():
    with patch("celerp.ai.intent.call_llm", new_callable=AsyncMock, return_value="ROUTING"):
        result = await classify_intent("put the receipt somewhere safe", has_files=True)
    assert result == Intent.ROUTING


@pytest.mark.asyncio
async def test_llm_fallback_comprehension():
    with patch("celerp.ai.intent.call_llm", new_callable=AsyncMock, return_value="COMPREHENSION"):
        result = await classify_intent("what does this contract say about termination?", has_files=True)
    assert result == Intent.COMPREHENSION


@pytest.mark.asyncio
async def test_llm_fallback_error_defaults_comprehension():
    with patch("celerp.ai.intent.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("timeout")):
        result = await classify_intent("do something with this file", has_files=True)
    assert result == Intent.COMPREHENSION


@pytest.mark.asyncio
async def test_routing_zero_credits():
    """Routing intent queries should consume 0 credits (tested at service level,
    but we verify the intent value here)."""
    result = await classify_intent("save this to contacts", has_files=True)
    assert result == Intent.ROUTING
