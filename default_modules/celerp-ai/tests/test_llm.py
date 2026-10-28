# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/llm.py - gateway LLM client.

All HTTP is mocked with respx (assert_all_mocked), so an accidental live call
fails inside the test. The 200 envelope, tool-call shape and error bodies come
from the recorded relay fixtures.
"""

from __future__ import annotations

import json
import uuid
import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import httpx
import pytest
import respx
from fastapi import HTTPException

from celerp.ai import llm as llm_mod
from celerp.ai.files import XLSX_CONTENT_TYPE
from celerp.ai import deadlines, jobs
from celerp.ai.llm import ModelResult, RelayError, _build_user_content

from _fixtures import synthetic_response

_RELAY = "https://relay.test"


async def complete(*args, **kwargs) -> ModelResult:
    """``llm.complete`` as every caller runs it: inside a slot of the app's model gate."""
    async with jobs.gate.slot("interactive"):
        return await llm_mod.complete(*args, **kwargs)


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
    with pytest.raises(ValueError, match="can't read this kind of file"):
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
        return_value=httpx.Response(200, json=synthetic_response("text_completion")))

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
async def test_complete_names_the_question_on_its_first_turn_only(relay):
    import uuid

    route = respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=synthetic_response("text_completion")))
    question = uuid.uuid4()
    await complete([{"role": "user", "content": "hi"}], operation_id=question)
    body = json.loads(route.calls.last.request.content)
    assert body["operation_id"] == str(question)
    assert "reservation_id" not in body

    await complete([{"role": "user", "content": "hi"}], reservation_id="resv-1")
    body = json.loads(route.calls.last.request.content)
    assert "operation_id" not in body and body["reservation_id"] == "resv-1"

    calls = len(route.calls)
    with pytest.raises(ValueError):
        await complete([{"role": "user", "content": "hi"}], reservation_id="resv-1", operation_id=question)
    assert len(route.calls) == calls


@pytest.mark.parametrize("status,detail,code", [
    (503, {"code": "service_unavailable", "message": "m"}, "service_unavailable"),
    (503, {"code": "busy", "message": "m"}, "busy"),
    (429, {"code": "rate_limited", "message": "m"}, "busy"),
    (429, {"code": "too_many_concurrent", "message": "m"}, "busy"),
    (409, {"code": "continuation_expired", "message": "m"}, "continuation_expired"),
    (409, {"code": "operation_expired", "message": "m"}, "operation_expired"),
    (422, {"code": "operation_mismatch", "message": "m"}, "operation_mismatch"),
    (409, "no code", "continuation_expired"),
    (429, "no code", "busy"),
])
@pytest.mark.asyncio
@respx.mock
async def test_complete_maps_typed_failures(relay, status, detail, code):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(status, json={"detail": detail}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status, ei.value.credits, ei.value.retry_after) == (code, status, 0, None)


@pytest.mark.asyncio
@respx.mock
async def test_lost_answer_carries_its_charge(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(
        503, json={"detail": {"code": "outcome_unknown", "message": "lost", "credits": 2}}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status, ei.value.credits) == ("outcome_unknown", 503, 2)


@pytest.mark.parametrize("detail,headers,wait", [
    ({"code": "operation_in_progress", "message": "m", "retry_after": 3}, {"Retry-After": "7"}, 3.0),
    ({"code": "operation_in_progress", "message": "m"}, {"Retry-After": "7"}, 7.0),
    ({"code": "operation_in_progress", "message": "m"}, {}, 1.0),
    ({"code": "operation_in_progress", "message": "m", "retry_after": "soon"}, {"Retry-After": "-1"}, 1.0),
])
@pytest.mark.asyncio
@respx.mock
async def test_question_in_progress_says_when_to_ask_again(relay, detail, headers, wait):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(409, json={"detail": detail}, headers=headers))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.retry_after, ei.value.credits) == ("operation_in_progress", wait, 0)


@pytest.mark.parametrize("code", ["not_available", "plan_limit", "quota_exceeded"])
@pytest.mark.asyncio
@respx.mock
async def test_typed_402_passes_through(relay, code):
    detail = {"code": code, "message": "m"}
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(402, json={"detail": detail}))
    with pytest.raises(HTTPException) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.status_code, ei.value.detail) == (402, detail)


@pytest.mark.asyncio
@respx.mock
async def test_complete_parses_message_model_usage(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=synthetic_response("text_completion")))
    result = await complete([{"role": "user", "content": "how many items"}])
    assert result.message["content"].startswith("You currently have 42 items")
    assert result.model_used == "test-model"
    assert result.usage["total_tokens"] == 187
    assert result.reservation_id == "test-reservation"
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
async def test_complete_parses_structured_tool_call(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(200, json=synthetic_response("read_tool_call")))
    result = await complete([{"role": "user", "content": "find widgets"}])
    calls = result.message["tool_calls"]
    assert calls[0]["type"] == "function"
    assert calls[0]["id"] == "call_read_0001"
    assert isinstance(calls[0]["function"]["arguments"], str)
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": {"q": "widget"}}


@pytest.mark.asyncio
@respx.mock
async def test_complete_maps_structured_402_409_429(relay):
    route = respx.post(f"{_RELAY}/ai/complete")

    route.mock(return_value=httpx.Response(402, json=synthetic_response("error_402")))
    with pytest.raises(HTTPException) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "quota_exceeded"

    route.mock(return_value=httpx.Response(409, json=synthetic_response("error_409")))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("continuation_expired", 409)

    route.mock(return_value=httpx.Response(429, json=synthetic_response("error_429")))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("busy", 429)


@pytest.mark.asyncio
@respx.mock
async def test_structured_503_maps_to_service_unavailable(relay):
    message = (
        "Celerp AI is temporarily unavailable due to a service issue. "
        "Our team has already been notified of the issue. Please try again later."
    )
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(
        503, json={"detail": {"code": "service_unavailable", "message": message}},
    ))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("service_unavailable", 503)
    assert str(ei.value) == message


@pytest.mark.asyncio
@respx.mock
async def test_plain_503_remains_busy(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(
        return_value=httpx.Response(503, json={"detail": "AI busy, retry shortly"}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("busy", 503)


@pytest.mark.parametrize("case", ["connected", "no_key", "disconnected"])
@pytest.mark.asyncio
async def test_complete_without_session_header(monkeypatch, case):
    """With no Connect session the call uses the install Bearer, or names why it cannot."""
    from unittest.mock import AsyncMock, patch
    from celerp.config import settings
    monkeypatch.setattr(llm_mod, "relay_session_headers", lambda: {"X-Session-Token": "", "X-Instance-ID": ""})
    monkeypatch.setattr(settings, "cloud_disconnected", case == "disconnected")
    reply = httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"},
                                      "model_used": "m", "usage": {}, "remaining": 29})
    bearer = AsyncMock(return_value=None if case == "no_key" else reply)
    with patch("celerp.services.cloud_entitlement.authenticated_request", new=bearer):
        if case == "connected":
            result = await complete([{"role": "user", "content": "x"}])
            assert result.remaining == 29
            bearer.assert_awaited_once()
            args, kwargs = bearer.await_args
            assert args == ("POST", "/ai/complete")
            body = b"".join([chunk async for chunk in kwargs["content"]()])
            assert json.loads(body)["messages"] == [{"role": "user", "content": "x"}]
            assert kwargs["total_s"] == deadlines.call_budget(await deadlines.limits(), len(body)).total_s
            return
        with pytest.raises(RelayError) as ei:
            await complete([{"role": "user", "content": "x"}])
    if case == "no_key":
        assert ei.value.code == "no_account"
        bearer.assert_awaited_once()
    else:
        assert ei.value.code == "no_session"
        bearer.assert_not_awaited()


_FILES_402 = ("This plan allows 1 file per question. Remove the extra files, "
              "or choose Connect + AI to attach more.")


@pytest.mark.parametrize("status,body,expected", [
    (402, {"detail": "AI is not available on this plan"},
     {"code": "not_available", "message": "AI is not available on this plan"}),
    (402, {"detail": _FILES_402}, {"code": "plan_limit", "message": _FILES_402}),
    (413, {"detail": {"code": "free_text_too_large", "message": "too long"}},
     {"code": "free_text_too_large", "message": "too long"}),
    (503, {"detail": {"code": "free_paused", "message": "paused",
                      "resets_at": "2026-11-01T00:00:00+00:00"}},
     {"code": "free_paused", "message": "paused", "resets_at": "2026-11-01T00:00:00+00:00"}),
])
@pytest.mark.asyncio
@respx.mock
async def test_complete_maps_free_codes(relay, status, body, expected):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(HTTPException) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert ei.value.status_code == status
    assert ei.value.detail == expected


@pytest.mark.asyncio
@respx.mock
async def test_complete_remaining_null_stays_unknown(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(
        200, json={"message": {"role": "assistant", "content": "hi"}, "remaining": None}))
    result = await complete([{"role": "user", "content": "x"}])
    assert result.remaining is None


@pytest.mark.asyncio
@respx.mock
async def test_complete_gateway_error(relay):
    respx.post(f"{_RELAY}/ai/complete").mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(RelayError) as ei:
        await complete([{"role": "user", "content": "x"}])
    assert (ei.value.code, ei.value.status) == ("gateway_error", 500)

