# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/intent.py"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

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
async def test_keyword_hit_returns_routing():
    result = await classify_intent("file this invoice", has_files=True)
    assert result == Intent.ROUTING


@pytest.mark.asyncio
async def test_ambiguous_defaults_to_comprehension():
    result = await classify_intent("put the receipt somewhere safe", has_files=True)
    assert result == Intent.COMPREHENSION


@pytest.mark.asyncio
async def test_question_defaults_to_comprehension():
    result = await classify_intent("what does this contract say about termination?", has_files=True)
    assert result == Intent.COMPREHENSION


@pytest.mark.asyncio
async def test_routing_keyword_with_files():
    result = await classify_intent("save this to contacts", has_files=True)
    assert result == Intent.ROUTING
