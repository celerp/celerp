# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""HTMX route coverage for the conversation-backed AI chat UI.

The chat page opens a conversation from the URL, the composer posts into that
conversation (creating one on the first turn), pending mutations render as action
cards the user confirms, and every visible string resolves from the catalog. An
unknown or foreign conversation id degrades to the empty state, never an error.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from ui.api_client import APIError
from test_helpers import make_test_token


def _authed(role: str = "owner", lang: str | None = None) -> dict:
    cookies = {"celerp_token": make_test_token(role=role)}
    if lang:
        cookies["celerp_lang"] = lang
    return cookies


_CLOUD_STATUS = {"tier": "ai", "remaining": 50, "used": 10, "base_limit": 200}
_ACTION = {
    "id": "call-1",
    "name": "create_item_items_post",
    "arguments": {"body": {"sku": "AGENT-1", "name": "Agent Widget"}},
}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    from celerp_ai.ui_routes import setup_ui_routes
    setup_ui_routes(ui_app)
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


def _patch_page(**overrides):
    """Patch the api calls GET /ai makes: company settings + cloud entitlement."""
    patches = {
        "get_company": AsyncMock(return_value={"settings": {}}),
        "ai_quota_status": AsyncMock(return_value=_CLOUD_STATUS),
    }
    patches.update(overrides)
    return [patch(f"celerp_ai.ui_routes.api.{name}", p) for name, p in patches.items()]


def _apply(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in patches:
        p.stop()


# ── GET /ai ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_page_empty_state_without_conversation(ui_client):
    patches = _patch_page()
    _apply(patches)
    try:
        r = await ui_client.get("/ai", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 200
    assert 'id="ai-empty-state"' in r.text
    assert 'id="ai-chat-form"' in r.text


@pytest.mark.asyncio
async def test_ai_page_unknown_conversation_renders_empty_state(ui_client):
    """A garbage or foreign conversation id falls back to the empty state,
    never a 500."""
    patches = _patch_page(
        ai_conversation_get=AsyncMock(side_effect=APIError(404, "Conversation not found")),
    )
    _apply(patches)
    try:
        r = await ui_client.get("/ai?conversation=not-a-real-id", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 200
    assert 'id="ai-empty-state"' in r.text


@pytest.mark.asyncio
async def test_ai_page_renders_thread_with_action_card(ui_client):
    """An owned conversation renders its bubbles plus one action card per open
    pending action, and seeds the hidden conversation id."""
    detail = {
        "id": "conv-1",
        "messages": [
            {"id": "m1", "role": "user", "content": "Add a widget"},
            {"id": "m2", "role": "assistant", "content": "I can add that.",
             "pending_actions": [_ACTION]},
        ],
    }
    patches = _patch_page(ai_conversation_get=AsyncMock(return_value=detail))
    _apply(patches)
    try:
        r = await ui_client.get("/ai?conversation=conv-1", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 200
    assert "Add a widget" in r.text
    assert "I can add that." in r.text
    assert "ai-action__card" in r.text
    assert "create_item_items_post" in r.text
    assert 'value="conv-1"' in r.text  # hidden conversation_id seeded


# ── POST /ai/chat ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_returns_user_and_assistant_bubbles(ui_client):
    result = {"answer": "Here is your summary.", "model_used": "m",
              "tools_called": [], "pending_actions": []}
    with patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/chat", cookies=_authed(),
                                 data={"query": "summarize", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "ai-msg--user" in r.text
    assert "Here is your summary." in r.text
    assert r.headers["HX-Push-Url"] == "/ai?conversation=conv-1"


@pytest.mark.asyncio
async def test_chat_creates_conversation_on_first_turn(ui_client):
    result = {"answer": "ok", "model_used": "m", "tools_called": [], "pending_actions": []}
    with patch("celerp_ai.ui_routes.api.ai_conversation_create",
               AsyncMock(return_value={"id": "conv-new"})) as create, \
         patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(return_value=result)) as query:
        r = await ui_client.post("/ai/chat", cookies=_authed(),
                                 data={"query": "hi", "conversation_id": ""})
    create.assert_awaited_once()
    assert query.await_args.args[2] == "conv-new"  # queried inside the new conversation
    assert r.headers["HX-Push-Url"] == "/ai?conversation=conv-new"


@pytest.mark.asyncio
async def test_chat_renders_action_card(ui_client):
    result = {"answer": "I'll add it once you confirm.", "model_used": "m",
              "tools_called": [], "pending_actions": [{**_ACTION, "message_id": "m2"}]}
    with patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/chat", cookies=_authed(),
                                 data={"query": "add widget", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "ai-action__card" in r.text
    assert "AGENT-1" in r.text
    assert "/ai/confirm-action-ui" in r.text
    assert 'value="m2"' in r.text  # message_id threaded into the confirm form


@pytest.mark.asyncio
async def test_chat_quota_exceeded_renders_upgrade_card(ui_client):
    err = APIError(402, {"base_limit": 200, "tier": "ai"})
    with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/chat", cookies=_authed(),
                                 data={"query": "hi", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "ai-upgrade-cta" in r.text


@pytest.mark.asyncio
async def test_chat_rate_limited_renders_busy_message(ui_client):
    err = APIError(429, "rate limited")
    with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/chat", cookies=_authed(),
                                 data={"query": "hi", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "The assistant is busy right now" in r.text


# ── POST /ai/conversations (new) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_conversation_redirects_to_thread(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_conversation_create",
               AsyncMock(return_value={"id": "conv-9"})):
        r = await ui_client.post("/ai/conversations", cookies=_authed())
    assert r.headers["HX-Redirect"] == "/ai?conversation=conv-9"


# ── POST /ai/confirm-action-ui ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_success_names_entity(ui_client):
    result = {"ok": True, "status": 200,
              "data": {"name": "Agent Widget", "id": "item:9"}, "error": None}
    with patch("celerp_ai.ui_routes.api.ai_confirm_action", AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/confirm-action-ui", cookies=_authed(), data={
            "conversation_id": "conv-1", "message_id": "m2", "tool_call_id": "call-1",
        })
    assert r.status_code == 200
    assert "ai-action__done" in r.text
    assert "Agent Widget" in r.text
    assert "item:9" in r.text


@pytest.mark.asyncio
async def test_confirm_expired_shows_neutral_panel(ui_client):
    err = APIError(409, {"code": "action_not_pending"})
    with patch("celerp_ai.ui_routes.api.ai_confirm_action", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/confirm-action-ui", cookies=_authed(), data={
            "conversation_id": "conv-1", "message_id": "m2", "tool_call_id": "call-1",
        })
    assert r.status_code == 200
    assert "already handled or has expired" in r.text


@pytest.mark.asyncio
async def test_confirm_failure_shows_error(ui_client):
    result = {"ok": False, "status": 422,
              "data": None, "error": {"code": "validation_failed", "message": "sku is required"}}
    with patch("celerp_ai.ui_routes.api.ai_confirm_action", AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/confirm-action-ui", cookies=_authed(), data={
            "conversation_id": "conv-1", "message_id": "m2", "tool_call_id": "call-1",
        })
    assert r.status_code == 200
    assert "ai-action__error" in r.text
    assert "sku is required" in r.text


# ── Catalog resolution in a non-English locale ───────────────────────────────

@pytest.mark.asyncio
async def test_action_card_strings_resolve_in_thai(ui_client):
    """Rendered strings come from the catalog: in Thai the card, its buttons, and
    the composer placeholder show the Thai text, not the English source."""
    result = {"answer": "ยืนยันไหม", "model_used": "m",
              "tools_called": [], "pending_actions": [{**_ACTION, "message_id": "m2"}]}
    with patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/chat", cookies=_authed(lang="th"),
                                 data={"query": "เพิ่มสินค้า", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "การดำเนินการที่เสนอ" in r.text   # ai.action_proposal (th)
    assert "ยืนยัน" in r.text                  # btn.confirm (th)
    assert "ยกเลิก" in r.text                  # btn.dismiss (th)
    assert "Proposed action" not in r.text     # English source must not leak


@pytest.mark.asyncio
async def test_busy_message_resolves_in_thai(ui_client):
    err = APIError(429, "rate limited")
    with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/chat", cookies=_authed(lang="th"),
                                 data={"query": "hi", "conversation_id": "conv-1"})
    assert r.status_code == 200
    assert "ผู้ช่วยกำลังไม่ว่างในขณะนี้" in r.text  # ai.busy (th)
    assert "The assistant is busy right now" not in r.text
