# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/llm.py - gateway LLM client.

All HTTP is mocked with respx (assert_all_mocked), so an accidental live call
fails inside the test. The 200 envelope, tool-call shape and error bodies come
from the recorded relay fixtures.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest
import respx
from fastapi import HTTPException

from celerp.ai import llm as llm_mod
from celerp.ai.files import XLSX_CONTENT_TYPE
from celerp.ai.llm import ModelResult, RelayError, _build_user_content, call_llm, complete

from _fixtures import load_relay

_RELAY = "https://relay.test"


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setattr(llm_mod, "relay_http_url", lambda: _RELAY)
    monkeypatch.setattr(
        llm_mod, "relay_session_headers",
        lambda: {"X-Session-Token": "stok", "X-Instance-ID": "iid"},
    )


# -- _build_user_content ----------------------------------------------------

def test_build_user_content_text_only():
    assert _build_user_content("hello") == "hello"


def test_image_part():
    files = [{"media_type": "image/png", "data": "abc123", "filename": "p.png", "file_id": "ai_up_1"}]
    result = _build_user_content("describe this", files)
    assert result[0]["type"] == "image_url"
    assert "data:image/png;base64,abc123" in result[0]["image_url"]["url"]
    assert result[1] == {"type": "text", "text": "describe this"}


def test_pdf_file_part_with_filename():
    files = [{"media_type": "application/pdf", "data": "cGRm", "filename": "invoice.pdf", "file_id": "ai_up_2"}]
    result = _build_user_content("read it", files)
    assert result[0]["type"] == "file"
    assert result[0]["file"]["filename"] == "invoice.pdf"
    assert result[0]["file"]["file_data"] == "data:application/pdf;base64,cGRm"


def test_csv_xlsx_metadata_only_no_bytes():
    files = [
        {"media_type": "text/csv", "data": "c2hvdWxkbm90YXBwZWFy", "filename": "stock.csv", "file_id": "ai_up_3"},
        {"media_type": XLSX_CONTENT_TYPE, "data": "eGxzeGJ5dGVz", "filename": "cat.xlsx", "file_id": "ai_up_4"},
    ]
    result = _build_user_content("import these", files)
    assert result[0] == {"type": "text", "text": "Attached file: stock.csv (file_id ai_up_3)"}
    assert result[1] == {"type": "text", "text": "Attached file: cat.xlsx (file_id ai_up_4)"}
    # The raw base64 bytes are never forwarded to the model.
    blob = json.dumps(result)
    assert "c2hvdWxkbm90YXBwZWFy" not in blob
    assert "eGxzeGJ5dGVz" not in blob


def test_unsupported_type_raises():
    files = [{"media_type": "application/zip", "data": "x", "filename": "a.zip", "file_id": "ai_up_5"}]
    with pytest.raises(ValueError, match="unsupported file type"):
        _build_user_content("hi", files)


def test_file_only_no_empty_text_part():
    files = [{"media_type": "image/jpeg", "data": "aaa", "filename": "p.jpg", "file_id": "ai_up_6"}]
    result = _build_user_content("", files)
    assert len(result) == 1
    assert result[0]["type"] == "image_url"


# -- complete ---------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_complete_sends_tools_and_reservation_only_when_set(relay):
    route = respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=load_relay("text_completion")))

    await complete(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
        tool_choice="auto",
        reservation_id="resv-1",
    )
    body = json.loads(route.calls.last.request.content)
    assert body["tools"][0]["function"]["name"] == "f"
    assert body["tool_choice"] == "auto"
    assert body["reservation_id"] == "resv-1"

    await complete([{"role": "user", "content": "hi"}])
    body2 = json.loads(route.calls.last.request.content)
    assert "tools" not in body2
    assert "tool_choice" not in body2
    assert "reservation_id" not in body2


@pytest.mark.asyncio
@respx.mock
async def test_complete_parses_message_model_usage(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=load_relay("text_completion")))
    result = await complete([{"role": "user", "content": "how many items"}])
    assert result.message["content"].startswith("You currently have 42 items")
    assert result.model_used == "z-ai/glm-5.3-flash"
    assert result.usage["total_tokens"] == 187
    assert result.reservation_id == "11111111-1111-4111-8111-111111111111"
    assert result.remaining == 199


@pytest.mark.asyncio
@respx.mock
async def test_complete_without_message_is_unexpected_reply(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json={"answer": "no message key", "model_used": "old"}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "hi"}])
    assert ei.value.code == "unexpected_reply"
    assert ei.value.status == 200


@pytest.mark.asyncio
@respx.mock
async def test_complete_parses_recorded_relay_envelope(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=load_relay("read_tool_call")))
    result = await complete([{"role": "user", "content": "find widgets"}])
    calls = result.message["tool_calls"]
    assert calls[0]["type"] == "function"
    assert calls[0]["id"] == "call_read_0001"
    # Arguments arrive as a JSON string, not a nested object.
    assert isinstance(calls[0]["function"]["arguments"], str)
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": {"q": "widget"}}


@pytest.mark.asyncio
@respx.mock
async def test_complete_maps_recorded_402_409_429(relay):
    route = respx.post(f"{_RELAY}/ai/complete")

    route.mock(return_value=httpx.Response(402, json=load_relay("error_402")))
    with pytest.raises(HTTPException) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "quota_exceeded"

    route.mock(return_value=httpx.Response(409, json=load_relay("error_409")))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("continuation_expired", 409)

    route.mock(return_value=httpx.Response(429, json=load_relay("error_429")))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("busy", 429)


@pytest.mark.asyncio
async def test_complete_no_session(monkeypatch):
    monkeypatch.setattr(llm_mod, "relay_session_headers", lambda: {"X-Session-Token": "", "X-Instance-ID": ""})
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert ei.value.code == "no_session"


@pytest.mark.asyncio
@respx.mock
async def test_complete_gateway_error(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("gateway_error", 500)


# -- call_llm ---------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_call_llm_returns_content(relay):
    route = respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=load_relay("text_completion")))
    result = await call_llm("advisory-model", "system", "how many items")
    assert result.message["content"].startswith("You currently have 42 items")
    assert result.usage
    body = json.loads(route.calls.last.request.content)
    assert body["messages"][0]["role"] == "system"
    assert body["hints"]["file_count"] == 0
    assert "tools" not in body


@pytest.mark.asyncio
@respx.mock
async def test_call_llm_quota_exceeded_raises_402(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(402, json=load_relay("error_402")))
    with pytest.raises(HTTPException) as ei:
        await call_llm("m", "s", "u")
    assert ei.value.status_code == 402


@pytest.mark.asyncio
async def test_call_llm_no_session(monkeypatch):
    monkeypatch.setattr(llm_mod, "relay_session_headers", lambda: {"X-Session-Token": ""})
    with pytest.raises(RelayError) as ei:
        await call_llm("m", "s", "u")
    assert ei.value.code == "no_session"


def test_ai_deadline_hierarchy_leaves_transport_margin():
    from celerp.ai.llm import MODEL_CALL_TIMEOUT_S
    from celerp.ai.service import AGENT_RUN_TIMEOUT_S
    from ui.api_client import AI_QUERY_TIMEOUT_S

    assert MODEL_CALL_TIMEOUT_S < AGENT_RUN_TIMEOUT_S < AI_QUERY_TIMEOUT_S
