# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/llm.py - gateway LLM client.

Covers:
  - _build_user_content: text-only vs multimodal
  - call_llm: success, no session, quota (402 -> HTTPException), busy, error,
              multimodal payload, semaphore concurrency
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from fastapi import HTTPException

from celerp.ai.llm import _build_user_content, call_llm

_HDRS = {"X-Session-Token": "stok", "X-Instance-ID": "iid"}
_NO_SESSION = {"X-Session-Token": "", "X-Instance-ID": ""}


# -- _build_user_content ----------------------------------------------------

def test_build_user_content_text_only():
    assert _build_user_content("hello") == "hello"


def test_build_user_content_with_files():
    files = [{"media_type": "image/png", "data": "abc123"}]
    result = _build_user_content("describe this", files)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "image_url"
    assert "data:image/png;base64,abc123" in result[0]["image_url"]["url"]
    assert result[1] == {"type": "text", "text": "describe this"}


def test_build_user_content_multiple_files():
    files = [
        {"media_type": "image/jpeg", "data": "aaa"},
        {"media_type": "application/pdf", "data": "bbb"},
    ]
    result = _build_user_content("analyze", files)
    assert len(result) == 3
    assert result[2]["text"] == "analyze"


# -- call_llm ---------------------------------------------------------------

def _mock_client(resp):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return client


def _resp(status: int, body: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body or "")
    return r


def _patches(headers=_HDRS, client=None):
    return (
        patch("celerp.ai.llm.relay_session_headers", return_value=headers),
        patch("celerp.ai.llm.relay_http_url", return_value="https://relay"),
        patch("celerp.ai.llm.httpx.AsyncClient", return_value=client),
    )


@pytest.mark.asyncio
async def test_call_llm_no_session():
    with patch("celerp.ai.llm.relay_session_headers", return_value=_NO_SESSION):
        with pytest.raises(RuntimeError, match="no active cloud session"):
            await call_llm("m", "s", "u")


@pytest.mark.asyncio
async def test_call_llm_success():
    client = _mock_client(_resp(200, {"answer": "Hello!"}))
    p1, p2, p3 = _patches(client=client)
    with p1, p2, p3:
        result = await call_llm("test-model", "sys", "user msg")
    assert result == "Hello!"
    assert client.post.call_args[0][0] == "https://relay/ai/complete"


@pytest.mark.asyncio
async def test_call_llm_quota_exceeded_raises_402():
    client = _mock_client(_resp(402, {"detail": {"code": "quota_exceeded", "message": "no credits"}}))
    p1, p2, p3 = _patches(client=client)
    with p1, p2, p3:
        with pytest.raises(HTTPException) as ei:
            await call_llm("m", "s", "u")
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_call_llm_busy():
    client = _mock_client(_resp(503, {}))
    p1, p2, p3 = _patches(client=client)
    with p1, p2, p3:
        with pytest.raises(RuntimeError, match="busy"):
            await call_llm("m", "s", "u")


@pytest.mark.asyncio
async def test_call_llm_gateway_error():
    client = _mock_client(_resp(500, {}))
    p1, p2, p3 = _patches(client=client)
    with p1, p2, p3:
        with pytest.raises(RuntimeError, match="gateway error"):
            await call_llm("m", "s", "u")
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_call_llm_with_files_multimodal():
    client = _mock_client(_resp(200, {"answer": "I see an image"}))
    files = [{"media_type": "image/jpeg", "data": "base64data"}]
    p1, p2, p3 = _patches(client=client)
    with p1, p2, p3:
        result = await call_llm("m", "s", "describe", files=files)
    assert result == "I see an image"
    body = client.post.call_args[1]["json"]
    assert isinstance(body["messages"][1]["content"], list)
    assert body["hints"]["file_count"] == 1


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    import celerp.ai.llm as llm_mod

    original = llm_mod._semaphore
    llm_mod._semaphore = asyncio.Semaphore(1)
    results: list[int] = []

    async def task(n: int) -> None:
        async with llm_mod._semaphore:
            results.append(n)
            await asyncio.sleep(0.02)

    try:
        await asyncio.gather(task(1), task(2))
        assert results == [1, 2]
    finally:
        llm_mod._semaphore = original
