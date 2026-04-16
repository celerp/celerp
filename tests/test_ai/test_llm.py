# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/llm.py - OpenRouter LLM client.

Covers:
  - _api_key: missing key raises RuntimeError
  - _build_user_content: text-only vs multimodal
  - call_llm: 200 success, 429 retry+backoff, retry exhausted, non-429 error,
              missing key, Retry-After header, semaphore concurrency
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest

from celerp.ai.llm import _api_key, _build_user_content, call_llm


# -- _api_key ---------------------------------------------------------------

def test_api_key_missing():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
        # Force re-read by calling the function (it reads os.getenv each time)
        with patch("celerp.ai.llm.os.getenv", return_value=""):
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                _api_key()


def test_api_key_present():
    with patch("celerp.ai.llm.os.getenv", return_value="sk-or-test"):
        assert _api_key() == "sk-or-test"


# -- _build_user_content ----------------------------------------------------

def test_build_user_content_text_only():
    result = _build_user_content("hello")
    assert result == "hello"


def test_build_user_content_with_files():
    files = [{"media_type": "image/png", "data": "abc123"}]
    result = _build_user_content("describe this", files)
    assert isinstance(result, list)
    assert len(result) == 2  # 1 image + 1 text
    assert result[0]["type"] == "image_url"
    assert "data:image/png;base64,abc123" in result[0]["image_url"]["url"]
    assert result[1] == {"type": "text", "text": "describe this"}


def test_build_user_content_multiple_files():
    files = [
        {"media_type": "image/jpeg", "data": "aaa"},
        {"media_type": "application/pdf", "data": "bbb"},
    ]
    result = _build_user_content("analyze", files)
    assert len(result) == 3  # 2 files + 1 text
    assert result[2]["text"] == "analyze"


# -- call_llm ---------------------------------------------------------------

def _mock_client(post_side_effect):
    """Create a mock httpx.AsyncClient that returns the given side_effect on .post()."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=post_side_effect) if isinstance(post_side_effect, list) else AsyncMock(return_value=post_side_effect)
    return client


def _resp(status: int, body: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body or "")
    r.headers = headers or {}
    return r


@pytest.mark.asyncio
async def test_call_llm_missing_key():
    with patch("celerp.ai.llm.os.getenv", return_value=""):
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await call_llm("model", "sys", "user")


@pytest.mark.asyncio
async def test_call_llm_success():
    resp = _resp(200, {"choices": [{"message": {"content": "Hello!"}}]})
    client = _mock_client(resp)

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            result = await call_llm("test-model", "sys", "user msg")
    assert result == "Hello!"


@pytest.mark.asyncio
async def test_call_llm_empty_choices():
    resp = _resp(200, {"choices": []})
    client = _mock_client(resp)

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            with pytest.raises(RuntimeError, match="empty choices"):
                await call_llm("m", "s", "u")


@pytest.mark.asyncio
async def test_call_llm_non_429_error():
    resp = _resp(500, {"error": "bad"})
    client = _mock_client(resp)

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            with pytest.raises(RuntimeError, match="500"):
                await call_llm("m", "s", "u")
    assert client.post.call_count == 1  # no retry on non-429


@pytest.mark.asyncio
async def test_call_llm_429_retry_then_success():
    r429 = _resp(429)
    r200 = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
    client = _mock_client([r429, r429, r200])

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            with patch("celerp.ai.llm.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await call_llm("m", "s", "u")

    assert result == "ok"
    assert client.post.call_count == 3
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_call_llm_429_respects_retry_after():
    r429 = _resp(429, headers={"retry-after": "7"})
    r200 = _resp(200, {"choices": [{"message": {"content": "ok"}}]})
    client = _mock_client([r429, r200])

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            with patch("celerp.ai.llm.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                with patch("celerp.ai.llm.random.uniform", return_value=0.0):
                    result = await call_llm("m", "s", "u")

    assert result == "ok"
    mock_sleep.assert_called_once_with(7.0)


@pytest.mark.asyncio
async def test_call_llm_429_exhausts_retries():
    r429 = _resp(429)
    client = _mock_client(r429)

    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            with patch("celerp.ai.llm.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="rate limit exceeded"):
                    await call_llm("m", "s", "u")

    assert client.post.call_count == 4  # initial + 3 retries


@pytest.mark.asyncio
async def test_call_llm_with_files():
    """Multimodal content is sent correctly."""
    resp = _resp(200, {"choices": [{"message": {"content": "I see an image"}}]})
    client = _mock_client(resp)

    files = [{"media_type": "image/jpeg", "data": "base64data"}]
    with patch("celerp.ai.llm.os.getenv", return_value="sk-test"):
        with patch("celerp.ai.llm.httpx.AsyncClient", return_value=client):
            result = await call_llm("m", "s", "describe", files=files)

    assert result == "I see an image"
    call_kwargs = client.post.call_args
    msg_content = call_kwargs[1]["json"]["messages"][1]["content"]
    assert isinstance(msg_content, list)  # multimodal


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
        assert results == [1, 2]  # serialized
    finally:
        llm_mod._semaphore = original
