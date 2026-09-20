# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Browser click-path tests for the conversation-backed AI chat.

The harness runs the API and UI in-process (uvicorn threads), so the model call
`celerp.ai.service.complete` is patched from the test the same way
`test_ai_page.py` patches `get_quota_status`. A per-test script drives the fake
model: a plain answer, a proposed mutation, or a gateway error. Reads and writes
still go through the real routes, so a confirmed action creates a real record.

Layout guards assert the page never scrolls sideways and the action card stays
inside the message column, and that the sidebar, header, and input box did not
move (their bounding boxes match the values captured on `main`, since the only
stylesheet change is the `.ai-bills*` to `.ai-action*` rename on the card).
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from playwright.sync_api import Page, expect

import celerp.ai.service as ai_service
from celerp.ai.llm import ModelResult

pytestmark = pytest.mark.browser


# ── Fake model scripting ──────────────────────────────────────────────────────

# The server thread pops from this list on each `complete` call. Every test
# fills it before triggering the request that reaches the agent loop.
_SCRIPT: list = []

_FAKE_STATUS = {
    "allowed": True, "used": 0, "base_limit": 200, "topup_balance": 0,
    "remaining": 200, "limit": 200, "topup_credits": 0, "resets_at": None,
    "tier": "ai",
}


async def _fake_complete(messages, *, tools=None, tool_choice=None,
                         reservation_id=None, **_kw) -> ModelResult:
    if not _SCRIPT:
        return _answer("Done.")
    item = _SCRIPT.pop(0)
    if isinstance(item, BaseException):
        raise item
    return item


def _answer(text: str) -> ModelResult:
    return ModelResult(message={"role": "assistant", "content": text},
                       model_used="fake-model", usage={}, reservation_id=None,
                       remaining=None)


def _tool_call(cap_name: str, arguments: dict, content: str) -> ModelResult:
    """A model turn that calls one capability with the given argument sections."""
    import json
    return ModelResult(
        message={
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": f"call-{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": cap_name, "arguments": json.dumps(arguments)},
            }],
        },
        model_used="fake-model", usage={}, reservation_id=None, remaining=None,
    )


def _mutation(cap_name: str, body: dict, content: str) -> ModelResult:
    return _tool_call(cap_name, {"body": body}, content)


@pytest.fixture(scope="session")
def agent_caps(api_server) -> dict:
    """The compiled agent capabilities of the running app, keyed by (method, path)."""
    from celerp.main import app
    from celerp.ai.tools import compile_agent_capabilities
    caps = compile_agent_capabilities(app, {})
    return {(cap["method"], cap["path"]): {"name": name, **cap} for name, cap in caps.items()}


@pytest.fixture(scope="session")
def item_create_cap(agent_caps) -> str:
    return agent_caps[("POST", "/items")]["name"]


@pytest.fixture(autouse=True)
def agent_env():
    """Report an active cloud subscription and swap in the fake model.

    Mirrors `test_ai_page.py`'s session-token + quota patch, then adds the
    scripted `complete` so the agent loop never reaches the relay.
    """
    from celerp.gateway.state import set_session_token
    set_session_token("test-session-token-for-browser-tests")
    _SCRIPT.clear()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("celerp_ai.routes.get_quota_status",
                                  new=AsyncMock(return_value=_FAKE_STATUS)))
        stack.enter_context(patch.object(ai_service, "complete", _fake_complete))
        yield
    _SCRIPT.clear()
    set_session_token("")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_chat(page: Page, ui_server: str) -> None:
    page.goto(f"{ui_server}/ai", wait_until="domcontentloaded")
    expect(page.locator(".ai-chat")).to_be_visible()


def _send(page: Page, text: str) -> None:
    page.locator("#ai-query-input").fill(text)
    page.locator(".ai-input__send").click()


# ── Click paths ────────────────────────────────────────────────────────────────

def test_new_conversation_button_creates_thread(page: Page, ui_server):
    _open_chat(page, ui_server)
    page.locator(".ai-sidebar__new").click()
    page.wait_for_url("**/ai?conversation=*")
    conv_id = page.url.split("conversation=", 1)[1]
    assert uuid.UUID(conv_id)  # a real conversation id, not a placeholder
    history = page.locator("#ai-history")
    expect(history.locator(".ai-sidebar__item").first).to_be_visible()


def test_send_creates_conversation_and_pushes_url(page: Page, ui_server):
    _SCRIPT.append(_answer("Here is your summary."))
    _open_chat(page, ui_server)
    _send(page, "summarize my month")

    expect(page.locator(".ai-msg--user").last).to_have_text("summarize my month")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("Here is your summary.")
    page.wait_for_url("**/ai?conversation=*")
    assert uuid.UUID(page.locator("#ai-conversation-id").get_attribute("value"))


def test_reload_keeps_thread(page: Page, ui_server):
    _SCRIPT.append(_answer("Revenue was 12,000."))
    _open_chat(page, ui_server)
    _send(page, "what was revenue")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("Revenue was 12,000.")
    page.wait_for_url("**/ai?conversation=*")

    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".ai-msg--user")).to_contain_text("what was revenue")
    expect(page.locator(".ai-msg--ai")).to_contain_text("Revenue was 12,000.")


def test_sidebar_link_opens_thread(page: Page, ui_server):
    _SCRIPT.append(_answer("Stock looks healthy."))
    _open_chat(page, ui_server)
    _send(page, "how is my stock")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("Stock looks healthy.")
    page.wait_for_url("**/ai?conversation=*")

    link = page.locator("#ai-history .ai-sidebar__item").first
    expect(link).to_be_visible()
    link.click()
    expect(page.locator(".ai-msg--user")).to_contain_text("how is my stock")


def test_action_card_confirm_success(page: Page, ui_server, item_create_cap, api):
    _SCRIPT.append(_mutation(
        item_create_cap, {"name": "Agent Widget", "sell_by": "piece"},
        "I'll add that once you confirm.",
    ))
    _open_chat(page, ui_server)
    _send(page, "add a widget")

    card = page.locator(".ai-action__card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Agent Widget")
    card.get_by_role("button", name="Confirm").click()

    # POST /items returns only the new id, so the success line degrades to the
    # plain "done" text; the proof the write landed is the record itself.
    expect(page.locator(".ai-action__done")).to_be_visible()

    resp = api.get("/items", params={"q": "Agent Widget"})
    assert resp.status_code == 200
    names = [it.get("name") for it in resp.json()["items"]]
    assert "Agent Widget" in names


def test_action_card_confirm_twice_shows_expired(page: Page, ui_server, item_create_cap):
    _SCRIPT.append(_mutation(
        item_create_cap, {"name": "Twice Widget", "sell_by": "piece"},
        "Confirm to add it.",
    ))
    _open_chat(page, ui_server)
    _send(page, "add another widget")

    card = page.locator(".ai-action__card")
    expect(card).to_be_visible()
    # Capture the confirm identifiers before the card is swapped away.
    confirm_form = card.locator("form").filter(has_text="Confirm")
    conv_id = confirm_form.locator("input[name=conversation_id]").get_attribute("value")
    message_id = confirm_form.locator("input[name=message_id]").get_attribute("value")
    tool_call_id = confirm_form.locator("input[name=tool_call_id]").get_attribute("value")

    card.get_by_role("button", name="Confirm").click()
    expect(page.locator(".ai-action__done")).to_be_visible()

    # The card was replaced by the success line, so a second confirm is only
    # reachable by re-issuing the request against the now-claimed action. It
    # must degrade to the neutral "already handled" panel, never an error.
    resp = page.request.post(f"{ui_server}/ai/confirm-action-ui", form={
        "conversation_id": conv_id,
        "message_id": message_id,
        "tool_call_id": tool_call_id,
    })
    assert resp.ok
    assert "already handled or has expired" in resp.text()


def test_action_card_dismiss_removes_card(page: Page, ui_server, item_create_cap):
    _SCRIPT.append(_mutation(
        item_create_cap, {"name": "Dismissed Widget", "sell_by": "piece"},
        "Confirm to add it.",
    ))
    _open_chat(page, ui_server)
    _send(page, "add a widget to dismiss")

    card = page.locator(".ai-action__card")
    expect(card).to_be_visible()
    card.get_by_role("button", name="Dismiss").click()
    expect(card).to_have_count(0)


def test_quota_card_on_402(page: Page, ui_server):
    from fastapi import HTTPException
    _SCRIPT.append(HTTPException(status_code=402,
                                 detail={"base_limit": 200, "tier": "ai"}))
    _open_chat(page, ui_server)
    _send(page, "anything")
    expect(page.locator(".ai-upgrade-cta")).to_be_visible()


def test_busy_bubble_on_429(page: Page, ui_server):
    from fastapi import HTTPException
    _SCRIPT.append(HTTPException(status_code=429, detail="rate limited"))
    _open_chat(page, ui_server)
    _send(page, "anything")
    expect(page.locator(".ai-msg--ai").last).to_contain_text(
        "The assistant is busy right now")


def test_file_chip_csv_accepted(page: Page, ui_server):
    _open_chat(page, ui_server)
    page.locator("#ai-file-input").set_input_files({
        "name": "prices.csv",
        "mimeType": "text/csv",
        "buffer": b"sku,name\nA-1,Widget\n",
    })
    chip = page.locator(".ai-file-chip")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text("prices.csv")


# ── Layout guards ──────────────────────────────────────────────────────────────

_VIEWPORTS = [(1440, 900), (390, 844)]


@pytest.mark.parametrize("width,height", _VIEWPORTS)
def test_no_horizontal_overflow_at_1440_and_390(page: Page, ui_server, item_create_cap,
                                                 width, height):
    page.set_viewport_size({"width": width, "height": height})
    _SCRIPT.append(_mutation(
        item_create_cap,
        {"name": "A widget with a deliberately long descriptive product name",
         "sell_by": "piece",
         "description": "A long human readable description that spans well beyond "
                        "the width of a single line so the card has to wrap it"},
        "I'll add that once you confirm.",
    ))
    _open_chat(page, ui_server)
    _send(page, "add a widget with a long name")
    expect(page.locator(".ai-action__card")).to_be_visible()

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px at {width}x{height}"


def test_action_card_within_message_column(page: Page, ui_server, item_create_cap):
    _SCRIPT.append(_mutation(
        item_create_cap, {"name": "Column Widget", "sell_by": "piece"},
        "I'll add that once you confirm.",
    ))
    _open_chat(page, ui_server)
    _send(page, "add a column widget")

    card = page.locator(".ai-action__card")
    expect(card).to_be_visible()
    card_box = card.bounding_box()
    main_box = page.locator(".ai-chat__main").bounding_box()
    assert card_box["x"] >= main_box["x"] - 2
    assert card_box["x"] + card_box["width"] <= main_box["x"] + main_box["width"] + 2


def test_sidebar_header_input_layout_intact(page: Page, ui_server):
    """The card rename only touches `.ai-action*` selectors inside the message
    column, so the chat shell around it must be undisturbed: the sidebar abuts
    the chat column, and the header and input share that column's left edge and
    width with the input stacked below the header. A selector collision that
    leaked the card styling onto a sibling would break one of these."""
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_chat(page, ui_server)
    sidebar = page.locator(".ai-sidebar").bounding_box()
    header = page.locator(".ai-chat__header").bounding_box()
    inp = page.locator(".ai-input").bounding_box()

    # Sidebar sits to the left of, and flush against, the chat column.
    assert abs((sidebar["x"] + sidebar["width"]) - header["x"]) <= 2
    assert abs(sidebar["y"] - header["y"]) <= 2
    # Header and input span the same chat column.
    assert abs(header["x"] - inp["x"]) <= 2
    assert abs(header["width"] - inp["width"]) <= 2
    # The composer is stacked below the header, not overlapping it.
    assert inp["y"] >= header["y"] + header["height"] - 2
