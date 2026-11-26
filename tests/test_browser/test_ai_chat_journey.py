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
from celerp_ai.ui_routes import QUESTION_POLL_S

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("tools_open_when_called")]


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


# A reply lands in the thread at the question bubble's next poll. The first question a
# server answers also builds its API schema, so on a slow runner the answer can land just
# after the first poll and show at the second: every wait here covers two polls more
# than Playwright's default.
_PLAYWRIGHT_WAIT_MS = 5000  # Playwright's default for expect()
_REPLY_WAIT_MS = _PLAYWRIGHT_WAIT_MS + 2 * QUESTION_POLL_S * 1000


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
        stack.enter_context(patch("celerp.ai.quota.get_quota_status",
                                  new=AsyncMock(return_value=_FAKE_STATUS)))
        stack.enter_context(patch.object(ai_service, "complete", _fake_complete))
        stack.callback(expect.set_options, timeout=_PLAYWRIGHT_WAIT_MS)
        expect.set_options(timeout=_REPLY_WAIT_MS)
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

def test_new_conversation_button_returns_to_empty_chat_without_creating_row(page: Page, ui_server):
    _SCRIPT.append(_answer("Existing thread."))
    _open_chat(page, ui_server)
    _send(page, "existing question")
    page.wait_for_url("**/ai?conversation=*")
    conversation = page.url.split("conversation=", 1)[1]
    history = page.locator("#ai-history .ai-sidebar__item")
    # The sidebar list loads after the reply: count it once it lists this thread.
    expect(page.locator(f"#ai-history .ai-sidebar__item[href='/ai?conversation={conversation}']")).to_have_count(1)
    history_before = history.count()

    page.locator(".ai-sidebar__new").click()
    page.wait_for_url("**/ai")
    assert "conversation=" not in page.url
    expect(page.locator("#ai-empty-state")).to_be_visible()
    expect(history).to_have_count(history_before)


def test_send_creates_conversation_and_pushes_url(page: Page, ui_server):
    _SCRIPT.append(_answer("Here is your summary."))
    _open_chat(page, ui_server)
    _send(page, "summarize my month")

    expect(page.locator(".ai-msg--user").last).to_have_text("summarize my month")
    expect(page.locator(".ai-msg--ai").last).to_contain_text("Here is your summary.")
    page.wait_for_url("**/ai?conversation=*")
    assert uuid.UUID(page.locator("#ai-conversation-id").get_attribute("value"))
    expect(page.locator("#ai-history .ai-sidebar__item").first).to_contain_text("summarize my month")


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


@pytest.mark.parametrize("width,height", _VIEWPORTS)
def test_reply_table_renders_inside_its_bubble(page: Page, ui_server, width, height):
    """A reply's Markdown table is a table that stays inside its bubble (a wide one
    scrolls inside it), with its bold text bold, at desktop and phone widths."""
    page.set_viewport_size({"width": width, "height": height})
    rows = "\n".join(f"| Item {i} with a long descriptive name | Warehouse {i} | {i * 1000}.00 |"
                     for i in range(1, 5))
    _SCRIPT.append(_answer("**Stock** by location:\n\n| Item | Location | Value |\n|---|---|---:|\n"
                           + rows + "\n\nThat is all of it."))
    _open_chat(page, ui_server)
    _send(page, "stock by location")

    reply = page.locator(".ai-msg--ai").last
    expect(reply.locator("table tbody tr")).to_have_count(4)
    expect(reply.locator("strong")).to_have_text("Stock")
    expect(reply).not_to_contain_text("|---")
    table_box, reply_box = reply.locator("table").bounding_box(), reply.bounding_box()
    assert table_box["x"] + table_box["width"] <= reply_box["x"] + reply_box["width"] + 1
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


def test_a_proposal_card_that_arrives_is_scrolled_into_view_to_its_foot(page: Page, ui_server, item_create_cap):
    """A card lands in the thread at the question's poll; the thread follows it to its foot,
    so Confirm and Dismiss are on screen, not under the composer. A reply that only answers
    keeps the thread at its foot the same way."""
    page.set_viewport_size({"width": 1280, "height": 720})
    _SCRIPT.append(_answer("\n\n".join(f"Line {n} of a long answer." for n in range(40))))
    _SCRIPT.append(_mutation(item_create_cap, {"name": "Scrolled Widget", "sell_by": "piece"},
                             "I'll add that once you confirm."))
    _open_chat(page, ui_server)
    at_foot = """() => { const m = document.getElementById('ai-messages');
                         return m.scrollHeight > m.clientHeight && m.scrollTop + m.clientHeight >= m.scrollHeight - 2; }"""

    _send(page, "tell me something long")
    expect(page.locator(".ai-msg--ai", has_text="Line 39 of a long answer.")).to_be_attached()
    page.wait_for_function(at_foot)

    _send(page, "add a scrolled widget")
    card = page.locator(".ai-action__card", has_text="Scrolled Widget")
    expect(card).to_be_attached()
    page.wait_for_function(at_foot)
    thread = page.locator("#ai-messages").bounding_box()
    for name in ("Confirm", "Dismiss"):
        button = card.get_by_role("button", name=name).bounding_box()
        assert thread["y"] <= button["y"] and button["y"] + button["height"] <= thread["y"] + thread["height"], (
            f"{name} is outside the thread's view: {button} in {thread}")


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


def test_bulk_card_lists_every_record_in_a_scrolling_box(page: Page, ui_server, agent_caps, company_with_books):
    """A propose_for_each card at its largest set shows every record and the values it
    writes in a box that scrolls on the page, amounts right-aligned under their header."""
    rule = agent_caps[("PATCH", "/accounting/rules/{rule_id}")]["name"]
    records = [{"path": {"rule_id": str(uuid.uuid4())},
                "body": {"match_pattern": f"RENT-{n:04}", "match_amount": 1000 + n}}
               for n in range(ai_service.MAX_FOR_EACH)]
    _SCRIPT.append(_tool_call(ai_service.PROPOSE_FOR_EACH, {"tool": rule, "records": records},
                              "I'll change these rules once you confirm."))
    _open_chat(page, ui_server)
    _send(page, "update the rent rules")

    box = page.locator(".ai-action__card .ai-action__records")
    expect(box).to_be_visible()
    rows = box.locator("tbody tr")
    expect(rows).to_have_count(ai_service.MAX_FOR_EACH)
    assert box.evaluate("e => e.scrollHeight > e.clientHeight")
    amount = box.locator("th.cell--number")
    expect(amount).to_have_count(1)
    assert amount.evaluate("e => getComputedStyle(e).textAlign") == "right"
    rows.last.scroll_into_view_if_needed()
    expect(rows.last).to_be_in_viewport()
    expect(rows.last).to_contain_text("RENT-0199")


def test_bulk_card_wide_records_scroll_inside_their_own_box_not_the_page(page: Page, ui_server, agent_caps, company_with_books):
    """A for_each table with enough columns to need horizontal scrolling scrolls inside
    its own box; it never pushes the page itself wider than the viewport."""
    rule = agent_caps[("PATCH", "/accounting/rules/{rule_id}")]["name"]
    long = "x" * 120
    records = [{"path": {"rule_id": str(uuid.uuid4())},
                "body": {"match_pattern": f"{long}-{n}", "match_amount": 1000 + n,
                         "note_a": long, "note_b": long, "note_c": long}}
               for n in range(5)]
    _SCRIPT.append(_tool_call(ai_service.PROPOSE_FOR_EACH, {"tool": rule, "records": records},
                              "I'll change these rules once you confirm."))
    _open_chat(page, ui_server)
    _send(page, "update the rent rules")

    box = page.locator(".ai-action__card .ai-action__records")
    expect(box).to_be_visible()
    assert box.evaluate("e => e.scrollWidth > e.clientWidth")
    assert box.evaluate("e => getComputedStyle(e).overflowX") == "auto", \
        "the records box must scroll its own overflow, not clip it"
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px"


def test_bulk_confirm_updates_only_the_selected_row_whatever_the_call_ids_hold(page: Page, ui_server, item_create_cap,
                                                                               agent_caps, monkeypatch):
    """A model may name its tool calls with spaces and selector characters; the review
    table still swaps the outcome into the row that was confirmed and no other."""
    import json
    import celerp_ai.ui_routes as ui_routes
    monkeypatch.setattr(ui_routes, "TABLE_FROM", 2)  # a turn proposes at most MAX_TOOL_CALLS_PER_TURN
    ids = ["a b#c.d:e", "a b#c.d:f", "a b#c.d:g"]
    bodies = [(agent_caps[("POST", "/companies/me/locations")]["name"], {"name": "Odd Id Shelf"}),
              (item_create_cap, {"name": "Odd Id Widget", "sell_by": "piece"}),
              (agent_caps[("POST", "/companies/me/categories")]["name"], {"name": "Odd Id Category"})]
    calls = [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps({"body": body})}}
             for cid, (name, body) in zip(ids, bodies)]
    _SCRIPT.append(ModelResult(
        message={"role": "assistant", "content": "I'll add these once you confirm.", "tool_calls": calls},
        model_used="fake-model", usage={}, reservation_id=None, remaining=None))
    _open_chat(page, ui_server)
    _send(page, "add the widgets")

    table = page.locator(".ai-action-table")
    rows = table.locator("tbody tr.data-row")
    expect(rows).to_have_count(len(ids))
    assert page.evaluate("[...document.querySelectorAll('[id]')].filter(e => /[^A-Za-z0-9_-]/.test(e.id)).length") == 0
    for n in range(len(ids)):
        if n != 1:
            rows.nth(n).locator(".bulk-select").uncheck()
    page.locator(".bulkbar .bulk-action-select").select_option("confirm")

    expect(rows.nth(1).locator(".ai-action-table__status")).to_contain_text("Applied")
    for n in range(len(ids)):
        if n != 1:
            expect(rows.nth(n).locator(".bulk-select")).to_have_count(1)
            expect(rows.nth(n).locator(".ai-action-table__status")).not_to_contain_text("Applied")
