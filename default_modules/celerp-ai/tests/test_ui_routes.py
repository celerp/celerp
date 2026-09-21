# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""HTMX route coverage for the conversation-backed AI chat UI.

The chat page opens a conversation from the URL, the composer posts into that
conversation (creating one on the first turn), pending mutations render as action
cards the user confirms, and every visible string resolves from the catalog.
Malformed or missing conversation ids return to the canonical empty chat URL.
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
async def test_ai_page_unknown_conversation_redirects_to_empty_chat(ui_client):
    """A missing/foreign thread returns to the canonical empty chat URL."""
    patches = _patch_page(
        ai_conversation_get=AsyncMock(side_effect=APIError(404, "Conversation not found")),
    )
    _apply(patches)
    try:
        r = await ui_client.get(
            "/ai?conversation=11111111-1111-4111-8111-111111111111",
            cookies=_authed(),
        )
    finally:
        _stop(patches)
    assert r.status_code == 302
    assert r.headers["location"] == "/ai"


@pytest.mark.asyncio
async def test_ai_page_malformed_conversation_redirects_without_api_call(ui_client):
    get_conversation = AsyncMock()
    patches = _patch_page(ai_conversation_get=get_conversation)
    _apply(patches)
    try:
        r = await ui_client.get("/ai?conversation=not-a-uuid", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 302
    assert r.headers["location"] == "/ai"
    get_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_page_renders_thread_with_action_card(ui_client):
    """An owned conversation renders its bubbles plus one action card per open
    pending action, and seeds the hidden conversation id."""
    detail = {
        "id": "11111111-1111-4111-8111-111111111111",
        "messages": [
            {"id": "m1", "role": "user", "content": "Add a widget"},
            {"id": "m2", "role": "assistant", "content": "I can add that.",
             "pending_actions": [_ACTION]},
        ],
    }
    patches = _patch_page(ai_conversation_get=AsyncMock(return_value=detail))
    _apply(patches)
    try:
        r = await ui_client.get("/ai?conversation=11111111-1111-4111-8111-111111111111", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 200
    assert "Add a widget" in r.text
    assert "I can add that." in r.text
    assert "ai-action__card" in r.text
    assert 'data-capability="create_item_items_post"' in r.text
    assert "create_item_items_post</" not in r.text  # the raw name is never visible text
    assert 'value="11111111-1111-4111-8111-111111111111"' in r.text  # hidden conversation_id seeded


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
    assert create.await_args.kwargs["title"] == "hi"
    assert query.await_args.args[2] == "conv-new"  # queried inside the new conversation
    assert r.headers["HX-Push-Url"] == "/ai?conversation=conv-new"


@pytest.mark.asyncio
async def test_first_turn_query_failure_keeps_created_conversation(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_conversation_create",
               AsyncMock(return_value={"id": "conv-new"})), \
         patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(side_effect=APIError(502, "down"))):
        r = await ui_client.post(
            "/ai/chat", cookies=_authed(),
            data={"query": "keep this question", "conversation_id": ""},
        )
    assert r.status_code == 200
    assert r.headers["HX-Push-Url"] == "/ai?conversation=conv-new"
    assert 'id="ai-conversation-id"' in r.text
    assert 'value="conv-new"' in r.text
    assert "keep this question" in r.text


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
async def test_new_conversation_is_navigation_only(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_conversation_create", AsyncMock()) as create:
        r = await ui_client.post("/ai/conversations", cookies=_authed())
    create.assert_not_awaited()
    assert r.headers["HX-Redirect"] == "/ai"


# ── POST /ai/confirm-action-ui ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_success_names_entity(ui_client):
    result = {"ok": True, "status": 200, "title": "Create item",
              "data": {"name": "Agent Widget", "id": "item:9"}, "error": None}
    with patch("celerp_ai.ui_routes.api.ai_confirm_action", AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/confirm-action-ui", cookies=_authed(), data={
            "conversation_id": "conv-1", "message_id": "m2", "tool_call_id": "call-1",
        })
    assert r.status_code == 200
    assert "ai-action__done" in r.text
    assert "Create item: Done." in r.text
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


# ── Action cards: what a person reads ────────────────────────────────────────

def _card_html(action: dict, lang: str = "en") -> str:
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_card
    return to_xml(_action_card("conv-1", "m2", action, lang))


def test_action_card_shows_title_fields_and_next_step():
    html = _card_html({**_ACTION, "title": "Create item"})
    assert "Create item" in html
    assert "Sku" in html and "AGENT-1" in html
    assert "Name" in html and "Agent Widget" in html
    assert "Check the details, then confirm" in html
    assert "Proposed action" in html


def test_action_card_falls_back_to_readable_name_without_title():
    html = _card_html(_ACTION)
    assert "Create item items post" in html
    assert "create_item_items_post</" not in html


def test_action_card_hides_id_when_name_sibling_present_and_nests_lines():
    action = {
        "id": "prop_1", "name": "create_bill_bills_post", "title": "Create bill from receipt.jpg",
        "arguments": {"body": {
            "contact_id": "c-1", "contact_name": "Supplier Co", "total": 842.0,
            "lines": [{"item_name": "Widget", "quantity": 2, "amount": 10.5}],
            "notes": None,
        }},
    }
    html = _card_html(action)
    assert "c-1" not in html
    assert "Supplier Co" in html
    assert "ai-action__sublist" in html
    assert "Item name: Widget" in html
    assert "Quantity: 2" in html
    assert "Notes" in html and "--" in html


def test_action_card_lists_warnings_before_the_buttons():
    action = {**_ACTION, "title": "Create bill",
              "warnings": ["The tax does not match the line totals."]}
    html = _card_html(action)
    assert "Check before confirming" in html
    assert "The tax does not match the line totals." in html
    assert html.index("ai-action__warnings") < html.index("/ai/confirm-action-ui")


def test_failed_record_card_has_no_buttons_and_says_why():
    action = {**_ACTION, "title": "Create item", "status": "failed",
              "error": "The action did not finish."}
    html = _card_html(action)
    assert "ai-action--failed" in html
    assert "The action did not finish." in html
    assert "This change was not applied." not in html
    assert "/ai/confirm-action-ui" not in html
    assert "Dismiss" not in html
    bare = _card_html({**_ACTION, "title": "Create item", "status": "failed"})
    assert "This change was not applied." in bare


def test_action_card_formats_amounts_and_quantities():
    action = {
        "id": "prop_1", "name": "create_doc_docs_post", "title": "Create bill from Quick Parts Ltd",
        "arguments": {"body": {
            "currency": "THB", "subtotal": 15.0, "tax": 1.5, "total": 20.0,
            "line_items": [{"name": "Bolt M6", "quantity": 1.0, "unit_price": 10.0, "line_total": 10.0}],
        }},
    }
    html = _card_html(action)
    assert "฿15.00" in html and "฿1.50" in html and "฿20.00" in html
    assert "Quantity: 1," in html
    assert "Unit price: ฿10.00" in html and "Line total: ฿10.00" in html
    assert ">15.0<" not in html and "1.0," not in html


def test_action_group_offers_confirm_all_only_for_several_open_cards():
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_group
    one = to_xml(_action_group("conv-1", "m2", [_ACTION], "en"))
    assert "/ai/confirm-all-ui" not in one
    two = to_xml(_action_group("conv-1", "m2", [_ACTION, {**_ACTION, "id": "call-2"}], "en"))
    assert "/ai/confirm-all-ui" in two
    assert "Confirm all (2)" in two
    failed = {**_ACTION, "id": "call-3", "status": "failed", "error": "x"}
    mixed = to_xml(_action_group("conv-1", "m2", [_ACTION, failed], "en"))
    assert "/ai/confirm-all-ui" not in mixed


# ── The review table for a large proposal set ────────────────────────────────

def _bills(n: int, *, flagged: set[int] = frozenset()) -> list[dict]:
    return [{
        "id": f"p{i}", "name": "create_bill_docs_post", "title": f"Create bill from r{i}.jpg",
        "arguments": {"body": {"contact_name": f"Vendor {i}", "issue_date": f"2026-09-0{i}",
                               "currency": "THB", "total": 10 * i}},
        "warnings": ["No date was found."] if i in flagged else [],
    } for i in range(1, n + 1)]


def _group_html(actions: list[dict]) -> str:
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _action_group
    return to_xml(_action_group("conv-1", "m2", actions, "en"))


def test_action_group_renders_review_table_past_five_proposals():
    html = _group_html(_bills(6, flagged={2}))
    assert 'id="ai-actions-m2"' in html and "ai-action-table" in html
    assert html.count('class="bulk-select"') == 6
    # Flagged rows start unticked with the reason in the Checks column.
    row2 = html[html.index('id="ai-act-m2-p2"'):html.index('id="ai-act-m2-p3"')]
    assert "checked" not in row2 and "No date was found." in row2
    row1 = html[html.index('id="ai-act-m2-p1"'):html.index('id="ai-act-m2-p2"')]
    assert "checked" in row1
    assert "Vendor 3" in html and "฿30.00" in html and "2026-09-03" in html
    # The shared bulk toolbar posts the ticked ids to the chunked confirm route.
    assert "/ai/confirm-all-ui/conv-1/m2?view=table" in html
    assert "Confirm selected" in html and "bulk-select-all" in html
    assert 'id="ai-tail-m2"' in html
    assert "ai-action__card" not in html


def test_action_group_keeps_cards_up_to_five_proposals():
    html = _group_html(_bills(5))
    assert "ai-action-table" not in html
    assert html.count('class="ai-action ai-action__card"') == 5
    assert "Confirm all (5)" in html
    assert "/ai/confirm-all-ui/conv-1/m2?view=cards" in html
    assert html.count('name="selected"') == 5


def test_action_table_failed_record_has_no_checkbox():
    actions = _bills(6)
    actions[0] = {**actions[0], "status": "failed", "error": "The model stopped."}
    html = _group_html(actions)
    assert html.count('class="bulk-select"') == 5
    row = html[html.index('id="ai-act-m2-p1"'):html.index('id="ai-act-m2-p2"')]
    assert "Failed" in row and "The model stopped." in row


def test_action_table_retryable_is_actionable_but_not_preselected():
    actions = _bills(6)
    actions[0] = {**actions[0], "status": "retryable", "error": "Result was uncertain."}
    html = _group_html(actions)
    row = html[html.index('id="ai-act-m2-p1"'):html.index('id="ai-act-m2-p2"')]
    assert 'class="bulk-select"' in row
    assert "checked" not in row
    assert "Retry" in row and "Result was uncertain." in row
    assert "Dismiss selected" in html
    assert "/ai/dismiss-all-ui/conv-1/m2" in html


# ── POST /ai/confirm-all-ui/{conversation}/{message}: chunked bulk confirm ───

def _ok(i: int) -> dict:
    return {"tool_call_id": f"p{i}", "name": "create_bill_docs_post", "title": f"Create bill from r{i}.jpg",
            "ok": True, "status": 201, "data": {"id": f"doc:{i}", "doc_number": f"BILL-{i}"}, "error": None}


@pytest.mark.asyncio
async def test_confirm_all_runs_the_first_chunk_and_chains_the_rest(ui_client):
    ids = [f"p{i}" for i in range(1, 13)]
    result = {"results": [_ok(i) for i in range(1, 11)], "completed": 10, "failed": 0}
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(return_value=result)) as call:
        r = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=table", cookies=_authed(),
                                 data={"selected": ids})
    assert r.status_code == 200, r.text
    assert call.await_args.args[2:] == ("conv-1", "m2")
    assert call.await_args.kwargs["tool_call_ids"] == ids[:10]
    # Progress line plus a form that fires on load with the remaining ids and the tally.
    assert "Applying 10 of 12." in r.text
    assert 'hx-trigger="load"' in r.text
    assert 'hx-post="/ai/confirm-all-ui/conv-1/m2?view=table"' in r.text
    assert 'name="selected" value="p11"' in r.text and 'name="selected" value="p12"' in r.text
    assert 'name="selected" value="p1"' not in r.text
    assert 'name="completed" value="10"' in r.text
    assert 'name="doc_ids" value="doc:1,doc:2,doc:3,doc:4,doc:5,doc:6,doc:7,doc:8,doc:9,doc:10"' in r.text
    # Each finished row loses its checkbox and gains a status badge with a record link.
    assert 'hx-swap-oob="true" id="ai-act-m2-p1-pick"' in r.text
    assert 'id="ai-act-m2-p1-status"' in r.text and "Applied" in r.text
    assert 'href="/docs/doc:1"' in r.text and 'href="/docs/doc:10"' in r.text
    assert "ai-act-m2-p11" not in r.text


@pytest.mark.asyncio
async def test_confirm_all_last_chunk_shows_tally_and_drafts_link(ui_client):
    result = {"results": [_ok(11), _ok(12)], "completed": 2, "failed": 0}
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(return_value=result)):
        r = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=table", cookies=_authed(), data={
            "selected": ["p11", "p12"], "completed": "10", "failed": "0",
            "doc_ids": ",".join(f"doc:{i}" for i in range(1, 11)),
        })
    assert r.status_code == 200, r.text
    assert "12 applied, 0 failed." in r.text
    assert "hx-trigger" not in r.text
    assert "Open these 12 drafts" in r.text
    assert 'href="/docs?view=drafts&amp;ids=' + ",".join(f"doc:{i}" for i in range(1, 13)) + '"' in r.text


@pytest.mark.asyncio
async def test_confirm_all_cards_view_replaces_each_card(ui_client):
    result = {
        "results": [
            {"tool_call_id": "p1", "name": "create_contact_crm_contacts_post", "title": "Add vendor Supplier Co",
             "ok": True, "status": 201, "data": {"name": "Supplier Co", "id": "contact:1"}, "error": None},
            {"tool_call_id": "p2", "name": "create_bill_docs_post", "title": "Create bill from receipt.jpg",
             "ok": False, "status": 422, "data": None,
             "error": {"code": "validation_failed", "message": "total is required"}},
        ],
        "completed": 1, "failed": 1,
    }
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(return_value=result)) as call:
        r = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=cards", cookies=_authed(),
                                 data={"selected": ["p1", "p2"]})
    assert call.await_args.kwargs["tool_call_ids"] == ["p1", "p2"]
    assert r.status_code == 200
    assert "1 applied, 1 failed." in r.text
    assert 'hx-swap-oob="true" id="ai-act-m2-p1"' in r.text
    assert "Add vendor Supplier Co" in r.text and "ai-action__done" in r.text
    assert 'href="/contacts/contact:1"' in r.text
    assert "Create bill from receipt.jpg" in r.text and "total is required" in r.text
    assert "Open these" not in r.text


@pytest.mark.asyncio
async def test_confirm_all_nothing_pending_shows_expired_tail(ui_client):
    err = APIError(409, "Nothing is pending.", {"code": "action_not_pending"})
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=cards", cookies=_authed(),
                                 data={"selected": "p1"})
    assert r.status_code == 200
    assert "already handled or has expired" in r.text
    assert "hx-trigger" not in r.text
    empty = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=table", cookies=_authed(), data={})
    assert "already handled or has expired" in empty.text


@pytest.mark.asyncio
async def test_confirm_all_pending_dependency_stays_actionable(ui_client):
    result = {
        "results": [{
            "tool_call_id": "p1", "name": "create_doc_docs_post",
            "title": "Create bill", "ok": False, "status": 409,
            "action_status": "pending", "data": None,
            "error": {"code": "action_dependency_not_ready",
                      "message": "Confirm the required earlier action first."},
        }],
        "completed": 0, "failed": 0, "attention": 1,
    }
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(return_value=result)):
        r = await ui_client.post(
            "/ai/confirm-all-ui/conv-1/m2?view=table", cookies=_authed(),
            data={"selected": ["p1"]},
        )
    assert "0 applied, 0 failed, 1 need attention." in r.text
    assert "Confirm the required earlier action first." in r.text
    assert 'class="bulk-select"' in r.text


@pytest.mark.asyncio
async def test_dismiss_all_refreshes_canonical_thread(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_dismiss_all",
               AsyncMock(return_value={"dismissed": ["p1", "p2"]})) as dismiss:
        r = await ui_client.post(
            "/ai/dismiss-all-ui/conv-1/m2", cookies=_authed(),
            data={"selected": ["p1", "p2"]},
        )
    assert r.headers["HX-Refresh"] == "true"
    assert dismiss.await_args.args[-1] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_confirm_all_api_failure_stops_the_run_with_the_tally(ui_client):
    err = APIError(502, "The API is down.", {"code": "upstream_unavailable", "message": "The API is down."})
    with patch("celerp_ai.ui_routes.api.ai_confirm_all", AsyncMock(side_effect=err)):
        r = await ui_client.post("/ai/confirm-all-ui/conv-1/m2?view=table", cookies=_authed(), data={
            "selected": ["p11", "p12"], "completed": "9", "failed": "1", "doc_ids": "doc:1",
        })
    assert "9 applied, 1 failed." in r.text
    assert "The API is down." in r.text
    assert "hx-trigger" not in r.text
    assert "Open these 1 drafts" in r.text


# ── Sidebar: conversations with open proposals ───────────────────────────────

@pytest.mark.asyncio
async def test_conversations_list_failure_is_not_empty_state(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_conversations_list",
               AsyncMock(side_effect=APIError(502, "down"))):
        r = await ui_client.get("/ai/conversations-list", cookies=_authed())
    assert "Conversations could not be loaded." in r.text
    assert "Retry" in r.text
    assert "No conversations yet." not in r.text


@pytest.mark.asyncio
async def test_memory_failure_has_retry_and_no_clear(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_memory_get",
               AsyncMock(side_effect=APIError(502, "down"))):
        r = await ui_client.get("/ai/memory-panel", cookies=_authed())
    assert "Memory could not be loaded." in r.text
    assert "Retry" in r.text
    assert "Clear All Memory" not in r.text


@pytest.mark.asyncio
async def test_conversations_list_shows_pending_count(ui_client):
    convs = [{"id": "c1", "title": "Receipts", "pending_count": 7},
             {"id": "c2", "title": "Questions", "pending_count": 0}]
    with patch("celerp_ai.ui_routes.api.ai_conversations_list", AsyncMock(return_value=convs)):
        r = await ui_client.get("/ai/conversations-list", cookies=_authed())
    assert r.status_code == 200
    assert r.text.count("ai-sidebar__badge") == 1
    assert '>7</span>' in r.text and "7 open proposals" in r.text


# ── Reading jobs: /ai/chat 202 and /ai/proposals-ui ──────────────────────────

@pytest.mark.asyncio
async def test_ai_page_status_failure_is_not_showcase(ui_client):
    patches = _patch_page(ai_quota_status=AsyncMock(side_effect=APIError(502, "down")))
    _apply(patches)
    try:
        r = await ui_client.get("/ai", cookies=_authed())
    finally:
        _stop(patches)
    assert "Celerp AI is temporarily unavailable." in r.text
    assert "ai-showcase__terminal" not in r.text


@pytest.mark.asyncio
async def test_quota_status_proxy_failure_is_unknown(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_quota_status",
               AsyncMock(side_effect=APIError(502, "down"))):
        r = await ui_client.get("/ai/quota-status", cookies=_authed())
    assert r.status_code == 200
    assert r.json() == {"unknown": True}


@pytest.mark.asyncio
async def test_chat_with_receipts_renders_progress_bubble(ui_client):
    with patch("celerp_ai.ui_routes.api.ai_conversation_query",
               AsyncMock(return_value={"job_id": "job-1", "message_id": "m1"})):
        r = await ui_client.post("/ai/chat", cookies=_authed(), data={
            "query": "", "conversation_id": "conv-1", "file_ids": "f1,f2,f3",
        })
    assert r.status_code == 200
    assert 'data-job-id="job-1"' in r.text
    assert "Reading files: 0 of 3 finished." in r.text
    assert "/ai/proposals-ui?conversation=conv-1&amp;job=job-1" in r.text
    assert "You can keep working." in r.text


@pytest.mark.asyncio
async def test_proposals_ui_keeps_polling_while_running(ui_client):
    job = {"id": "job-1", "status": "running", "total_files": 4,
           "completed_files": 1, "failed_files": 1, "proposal_message_id": None}
    with patch("celerp_ai.ui_routes.api.ai_batch_status", AsyncMock(return_value=job)), \
         patch("celerp_ai.ui_routes.api.ai_job_proposals", AsyncMock()) as proposals:
        r = await ui_client.get("/ai/proposals-ui?conversation=conv-1&job=job-1", cookies=_authed())
    proposals.assert_not_awaited()
    assert "Reading files: 2 of 4 finished." in r.text
    assert "load delay:5s" in r.text
    assert "width:50%" in r.text


@pytest.mark.asyncio
async def test_proposals_ui_renders_summary_and_cards_when_done(ui_client):
    job = {"id": "job-1", "status": "completed", "total_files": 2,
           "completed_files": 2, "failed_files": 0, "proposal_message_id": None}
    proposals = {"message_id": "m9", "answer": "Two bills are ready to confirm.",
                 "pending_actions": [
                     {"id": "prop_1", "name": "create_bill_bills_post", "title": "Create bill from a.jpg",
                      "arguments": {"body": {"total": 10}}, "warnings": [], "file_id": "f1"},
                     {"id": "prop_2", "name": "create_bill_bills_post", "title": "Create bill from b.jpg",
                      "arguments": {"body": {"total": 20}}, "warnings": ["No date was found."], "file_id": "f2"},
                 ]}
    with patch("celerp_ai.ui_routes.api.ai_batch_status", AsyncMock(return_value=job)), \
         patch("celerp_ai.ui_routes.api.ai_job_proposals", AsyncMock(return_value=proposals)):
        r = await ui_client.get("/ai/proposals-ui?conversation=conv-1&job=job-1", cookies=_authed())
    assert r.status_code == 200
    assert "2 of 2 files were read." in r.text
    assert "Two bills are ready to confirm." in r.text
    assert "Create bill from a.jpg" in r.text and "Create bill from b.jpg" in r.text
    assert "No date was found." in r.text
    assert "/ai/confirm-all-ui/conv-1/m9?view=cards" in r.text
    assert "Confirm all (2)" in r.text


@pytest.mark.asyncio
async def test_proposals_ui_failed_job_is_an_error_bubble(ui_client):
    job = {"id": "job-1", "status": "failed", "total_files": 2, "completed_files": 0,
           "failed_files": 2, "error": "Every file failed to read."}
    with patch("celerp_ai.ui_routes.api.ai_batch_status", AsyncMock(return_value=job)):
        r = await ui_client.get("/ai/proposals-ui?conversation=conv-1&job=job-1", cookies=_authed())
    assert "ai-msg--error" in r.text
    assert "The files could not be read." in r.text
    assert "Every file failed to read." in r.text
    assert "Attach files and try again" in r.text
    assert "hx-get" not in r.text


@pytest.mark.asyncio
async def test_proposals_ui_api_error_shows_message(ui_client):
    job = {"id": "job-1", "status": "completed", "total_files": 1,
           "completed_files": 1, "failed_files": 0, "proposal_message_id": None}
    err = APIError(409, "The bill capability is not available.", {"code": "capability_unavailable"})
    with patch("celerp_ai.ui_routes.api.ai_batch_status", AsyncMock(return_value=job)), \
         patch("celerp_ai.ui_routes.api.ai_job_proposals", AsyncMock(side_effect=err)):
        r = await ui_client.get("/ai/proposals-ui?conversation=conv-1&job=job-1", cookies=_authed())
    assert "1 of 1 files were read." in r.text
    assert "Error: The bill capability is not available." in r.text


@pytest.mark.asyncio
async def test_thread_orders_jobs_between_messages_and_marks_errors(ui_client):
    detail = {
        "id": "conv-1",
        "messages": [
            {"id": "m1", "role": "user", "content": "", "file_ids": ["f1"],
             "created_at": "2026-09-20T10:00:00"},
            {"id": "m2", "role": "assistant", "content": "The model timed out.", "error": True,
             "created_at": "2026-09-20T10:03:00"},
        ],
        "jobs": [{"id": "job-1", "status": "completed", "total_files": 1, "completed_files": 1,
                  "failed_files": 0, "proposal_message_id": "m2",
                  "created_at": "2026-09-20T10:01:00"}],
    }
    patches = _patch_page(ai_conversation_get=AsyncMock(return_value=detail))
    _apply(patches)
    try:
        r = await ui_client.get("/ai?conversation=11111111-1111-4111-8111-111111111111", cookies=_authed())
    finally:
        _stop(patches)
    assert r.status_code == 200
    assert "Attached file(s)" in r.text
    assert "1 of 1 files were read." in r.text
    assert "ai-msg--error" in r.text
    assert r.text.index("Attached file(s)") < r.text.index("1 of 1 files") < r.text.index("timed out")


def test_chat_view_has_no_gif_and_an_upload_error_slot():
    from fasthtml.common import to_xml
    from celerp_ai.ui_routes import _chat_view
    html = to_xml(_chat_view(lang="en"))
    assert "image/gif" not in html
    assert 'id="ai-upload-error"' in html
    assert "Upload failed: {detail}" in html
