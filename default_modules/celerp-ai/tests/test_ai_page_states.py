# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""What the AI page shows for each answer and account situation.

An answer built from help pages ends with a Sources line linking each page; a help
search that found nothing says the answer is not from the Celerp help.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from celerp.help import registry
from celerp.help.schema import HelpPage
from ui.i18n import t
from test_helpers import make_test_token

_GOOD = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "help" / "good"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    from celerp_ai.ui_routes import setup_ui_routes
    setup_ui_routes(ui_app)
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token()})
        yield c


@pytest.fixture
def one_help_page(monkeypatch):
    page = HelpPage(
        id="contacts.add-supplier", title="Add a supplier", summary="Add a vendor.", keywords=[],
        routes=["/contacts"], permission=None, order=1,
        steps=[{"text": "Open the list.", "labels": {}, "image": None,
                "capture": {"route": "/contacts", "selector": "main", "actions": []}}],
        related=[], help_dir=_GOOD, source="first_party", module="celerp-contacts",
        module_display="Contacts",
    )
    monkeypatch.setattr(registry, "_index", registry.Index(pages={page.id: page}))
    return page


@pytest.mark.asyncio
async def test_sources_line_and_no_coverage_line(ui_client, one_help_page):
    from celerp_ai.ui_routes import _thread

    async def _reply(help_pages):
        result = {"answer": "Open Contacts.", "model_used": "m", "tools_called": [],
                  "pending_actions": [], "help_pages": help_pages}
        with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(return_value=result)):
            r = await ui_client.post("/ai/chat", data={"query": "supplier?", "conversation_id": "conv-1"})
        assert r.status_code == 200
        return r.text

    cited = await _reply(["contacts.add-supplier"])
    assert t("ai.help_sources") in cited
    assert 'href="/help/contacts.add-supplier"' in cited and "Add a supplier" in cited
    assert t("ai.help_no_coverage") not in cited

    uncovered = await _reply([])
    assert t("ai.help_no_coverage") in uncovered
    assert t("ai.help_sources") not in uncovered

    plain = await _reply(None)
    assert t("ai.help_sources") not in plain and t("ai.help_no_coverage") not in plain

    # A reloaded thread shows the same line from the stored message.
    stored = {"id": "m1", "role": "assistant", "content": "Open Contacts.",
              "created_at": "2026-10-23T00:00:00", "help_pages": ["contacts.add-supplier"]}
    html = "".join(to_xml(ft) for ft in _thread([stored], [], "conv-1", "en"))
    assert 'href="/help/contacts.add-supplier"' in html


# ── Account states of GET /ai ────────────────────────────────────────────────

_OFFER = {"paid_ai": False, "free_ai_questions": 30, "plan_ai_questions": {"cloud": 100, "ai": 200}}


async def _page(ui_client, status, *, offer=None, owner=True, role="owner"):
    patches = {
        "get_company": AsyncMock(return_value={"settings": {}}),
        "ai_quota_status": AsyncMock(return_value=status),
        "ai_offer": AsyncMock(return_value=_OFFER if offer is None else offer),
        "installation_owner": AsyncMock(return_value=owner),
    }
    ui_client.cookies.update({"celerp_token": make_test_token(role=role)})
    started = [patch(f"celerp_ai.ui_routes.api.{n}", m) for n, m in patches.items()]
    for p in started:
        p.start()
    try:
        r = await ui_client.get("/ai")
    finally:
        for p in reversed(started):
            p.stop()
    assert r.status_code == 200
    return r.text


@pytest.mark.asyncio
async def test_no_account_owner_sees_create_account_button(ui_client):
    html = await _page(ui_client, {"local": True})
    assert t("ai.free_no_account_title") in html
    assert t("ai.free_questions_included", n=30) in html and t("ai.free_terms") in html
    assert "next=ai" in html and 'hx-target="#account-gate-host"' in html
    assert t("ai.free_create_account") in html
    assert t("ai.free_ask_owner") not in html
    assert 'id="ai-chat-form"' not in html


@pytest.mark.asyncio
async def test_no_account_non_owner_sees_button_and_owner_note(ui_client):
    html = await _page(ui_client, {"local": True}, owner=False, role="manager")
    assert "next=ai" in html and t("ai.free_ask_owner") in html

    # The owner check failed: no note on a guess, the button stays.
    html = await _page(ui_client, {"local": True}, owner=None, role="manager")
    assert "next=ai" in html and t("ai.free_ask_owner") not in html


@pytest.mark.asyncio
async def test_owner_check_failure_reads_as_unknown(ui_client, monkeypatch):
    """The client itself turns a failed owner check into None, so the page leaves the note out."""
    import contextlib
    import ui.api_client as api

    @contextlib.asynccontextmanager
    async def _down(token, **_kw):
        raise api.APIError(503, "down")
        yield

    monkeypatch.setattr(api, "_api_client", _down)
    owner = await api.installation_owner("tok")
    assert owner is None
    html = await _page(ui_client, {"local": True}, owner=owner, role="manager")
    assert "next=ai" in html and t("ai.free_ask_owner") not in html

    # The free count is unknown: the count line is left out, the card stays.
    html = await _page(ui_client, {"local": True}, offer={"paid_ai": False, "free_ai_questions": None,
                                                         "plan_ai_questions": None})
    assert "next=ai" in html and t("ai.free_questions_included", n=30) not in html
    assert t("ai.free_terms") not in html


@pytest.mark.asyncio
async def test_free_tier_shows_questions_left(ui_client):
    html = await _page(ui_client, {"tier": "free", "base_limit": 30, "remaining": 12, "used": 18})
    assert 'id="ai-chat-form"' in html
    assert t("ai.free_questions_left", n=12, limit=30) in html
    assert "ai-showcase__terminal" not in html


@pytest.mark.asyncio
async def test_free_not_eligible_state(ui_client):
    html = await _page(ui_client, {"tier": "free", "base_limit": 0, "remaining": 0, "used": 0})
    assert t("ai.free_not_eligible") in html
    assert 'href="/settings/cloud"' in html
    assert 'id="ai-chat-form"' not in html and "ai-showcase__terminal" not in html


@pytest.mark.asyncio
async def test_disconnected_shows_signed_out(ui_client):
    html = await _page(ui_client, {"disconnected": True})
    assert t("error.ai_signed_out") in html
    assert 'href="/settings/cloud"' in html
    assert "ai-showcase__terminal" not in html and 'id="ai-chat-form"' not in html


@pytest.mark.asyncio
async def test_buy_more_credits_hidden_for_free(ui_client):
    from celerp_ai.ui_routes import _quota_section

    free = {"tier": "free", "base_limit": 30, "remaining": 12, "used": 18}
    assert 'id="ai-topup-link"' not in await _page(ui_client, free)
    assert 'id="ai-topup-link"' in await _page(ui_client, {"tier": "ai", "base_limit": 200,
                                                           "remaining": 50, "used": 150})
    # Free questions never reset; the quota card says so and offers no top-up.
    card = to_xml(_quota_section({**free, "resets_at": "never"}))
    assert t("ai.credits_do_not_reset") in card
    assert t("msg.buy_more_credits") not in card
    paid = to_xml(_quota_section({"tier": "ai", "base_limit": 200, "remaining": 50, "used": 150}))
    assert t("msg.buy_more_credits") in paid


@pytest.mark.asyncio
async def test_plan_counts_from_account_methods_and_omitted_when_unknown(ui_client):
    html = await _page(ui_client, {"local": True})
    assert t("ai.plan_lifetime_credits", n=100) in html
    assert t("ai.plan_monthly_credits", n=200) in html

    html = await _page(ui_client, {"local": True},
                       offer={"paid_ai": False, "free_ai_questions": 30, "plan_ai_questions": {"ai": 250}})
    assert t("ai.plan_monthly_credits", n=250) in html
    assert t("ai.plan_lifetime_credits", n=100) not in html

    from ui.api_client import APIError
    ui_client.cookies.update({"celerp_token": make_test_token()})
    with patch("celerp_ai.ui_routes.api.get_company", AsyncMock(return_value={"settings": {}})), \
            patch("celerp_ai.ui_routes.api.ai_quota_status", AsyncMock(return_value={"local": True})), \
            patch("celerp_ai.ui_routes.api.ai_offer", AsyncMock(side_effect=APIError(502, "down"))), \
            patch("celerp_ai.ui_routes.api.installation_owner", AsyncMock(return_value=True)):
        r = await ui_client.get("/ai")
    assert r.status_code == 200 and "next=ai" in r.text
    assert t("ai.plan_monthly_credits", n=200) not in r.text
    assert t("msg.get_the_ai_plan") in r.text


# ── Relay refusals of a conversation reply ───────────────────────────────────
# Each error is built the way the UI client builds it from the API's response,
# carrying the relay's own body (relay/routers/ai.py).

def _refusal(status: int, detail):
    from ui.api_client import _api_error
    return _api_error(status, {"detail": detail}, "")


_FREE_USED_UP = {"code": "quota_exceeded", "message": "AI credit limit reached.", "tier": "free",
                 "used": 30, "base_limit": 30, "topup_balance": 0, "remaining": 0, "resets_at": "never"}
_HITS = {"hits": [{"id": "contacts.add-supplier", "title": "Add a supplier", "summary": "Add a vendor.",
                   "module": "celerp-contacts", "module_enabled": True, "permitted": True,
                   "permission_text": None, "source": "first_party"}]}


async def _chat(ui_client, error, *, hits=None, offer=None):
    ui_client.cookies.update({"celerp_token": make_test_token()})
    search = AsyncMock(return_value=hits) if not isinstance(hits, Exception) else AsyncMock(side_effect=hits)
    with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(side_effect=error)), \
            patch("celerp_ai.ui_routes.api.help_search", search), \
            patch("celerp_ai.ui_routes.api.ai_offer", AsyncMock(return_value=_OFFER if offer is None else offer)):
        r = await ui_client.post("/ai/chat", data={"query": "How do I add a supplier?",
                                                   "conversation_id": "conv-1"})
    assert r.status_code == 200
    return r, search


@pytest.mark.asyncio
async def test_free_zero_left_shows_help_hits_no_topup(ui_client):
    r, search = await _chat(ui_client, _refusal(402, _FREE_USED_UP), hits=_HITS)
    assert t("ai.free_used_up", limit=30) in r.text
    assert 'href="/help/contacts.add-supplier"' in r.text and "Add a supplier" in r.text
    assert t("msg.get_the_ai_plan") in r.text and t("ai.plan_monthly_credits", n=200) in r.text
    assert t("msg.buy_more_credits") not in r.text
    assert search.await_args.args[1] == "How do I add a supplier?"
    # The bubble is the help line itself: no wrapper whose markup whitespace would show
    # as blank lines in a pre-wrap bubble.
    assert f'<p class="ai-msg ai-msg--ai ai-msg__sources">\n{t("ai.help_hits")} <a' in r.text


@pytest.mark.asyncio
async def test_free_zero_left_help_search_failure_omits_hits(ui_client):
    from ui.api_client import APIError
    r, _ = await _chat(ui_client, _refusal(402, _FREE_USED_UP), hits=APIError(502, "down"))
    assert t("ai.free_used_up", limit=30) in r.text
    assert t("msg.get_the_ai_plan") in r.text
    assert "/help/" not in r.text


@pytest.mark.asyncio
async def test_free_paused_shows_date(ui_client):
    body = {"code": "free_paused", "resets_at": "2026-11-01T00:00:00+00:00",
            "message": "Free AI queries are paused for the rest of this month and resume on 2026-11-01. "
                       "Paid plans keep working."}
    r, _ = await _chat(ui_client, _refusal(503, body), hits=_HITS)
    assert t("ai.free_paused", date="2026-11-01") in r.text
    assert 'href="/help/contacts.add-supplier"' in r.text
    assert t("ai.busy") not in r.text


@pytest.mark.asyncio
async def test_free_text_too_large_message(ui_client):
    body = {"code": "free_text_too_large",
            "message": "This question is too long for the free plan, which allows 8 KB of text per "
                       "question. Shorten it, or choose Connect or Connect + AI for longer questions."}
    r, _ = await _chat(ui_client, _refusal(413, body))
    assert t("ai.free_text_too_large") in r.text
    assert "X-AI-Clear-Composer" not in r.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("detail, shows", [
    ({"code": "not_available", "message": "AI is not available on this plan"}, "not_eligible"),
    ({"code": "plan_limit", "message": "This plan allows 1 file per question. Remove the extra files, "
                                       "or choose Connect + AI to attach more."}, "relay_text"),
    ({**_FREE_USED_UP, "tier": "ai", "base_limit": 200, "used": 200}, "paid_card"),
])
async def test_conversation_inline_402_codes(ui_client, detail, shows):
    r, _ = await _chat(ui_client, _refusal(402, detail), hits=_HITS)
    paid_card = t("msg.need_more_topup_credits_never_expire")
    subscribe_card = t("msg.upgrade_to_keep_your_ai_operator_working")
    if shows == "not_eligible":
        assert t("ai.free_not_eligible") in r.text
    elif shows == "relay_text":
        assert detail["message"] in r.text
    else:
        assert paid_card in r.text and t("ai.credits_exhausted", limit=200) in r.text
    if shows != "paid_card":
        assert paid_card not in r.text
    assert subscribe_card not in r.text
    # A refused question stays in the composer, whatever the refusal.
    assert "X-AI-Clear-Composer" not in r.headers


# ── The composer's question id ───────────────────────────────────────────────

_QUESTION = "2b0f4c1e-8d0a-4f43-9a55-3c1f6d2e9b10"


def _operation_value(html: str) -> str:
    import re
    found = re.findall(r'<input[^>]*id="ai-operation-id"[^>]*>', html)
    assert len(found) == 1, found
    return re.search(r'value="([^"]*)"', found[0]).group(1)


async def _send(ui_client, *, result=None, error=None, data=None):
    ui_client.cookies.update({"celerp_token": make_test_token()})
    query = AsyncMock(return_value=result) if error is None else AsyncMock(side_effect=error)
    with patch("celerp_ai.ui_routes.api.ai_conversation_query", query), \
            patch("celerp_ai.ui_routes.api.help_search", AsyncMock(return_value=_HITS)), \
            patch("celerp_ai.ui_routes.api.ai_offer", AsyncMock(return_value=_OFFER)):
        r = await ui_client.post("/ai/chat", headers={"HX-Request": "true"}, data={
            "query": "How do I add a supplier?", "conversation_id": "conv-1", "operation_id": _QUESTION,
            **(data or {}),
        })
    return r, query


@pytest.mark.asyncio
async def test_answer_clears_the_composer_and_starts_a_new_question(ui_client):
    r, query = await _send(ui_client, result={
        "answer": "Open Contacts.", "pending_actions": [], "message_id": _QUESTION, "asked_again": False})
    assert r.headers["X-AI-Clear-Composer"] == "1"
    assert query.await_args.kwargs["operation_id"] == _QUESTION
    fresh = _operation_value(r.text)
    assert fresh != _QUESTION and len(fresh) == 36
    assert "ai-msg--user" in r.text and "ai-msg__again" not in r.text


@pytest.mark.asyncio
async def test_lost_answer_keeps_the_question_and_offers_to_ask_again(ui_client):
    import html as _html
    import json as _json
    r, _ = await _send(ui_client, result={
        "answer": "", "error": t("ai.err_outcome_unknown"), "error_code": "outcome_unknown",
        "retryable": True, "message_id": _QUESTION, "asked_again": False})
    assert "X-AI-Clear-Composer" not in r.headers
    assert _operation_value(r.text) == _QUESTION
    assert t("ai.err_outcome_unknown") in _html.unescape(r.text)
    vals = _json.loads(_html.unescape(r.text.split("hx-vals='", 1)[1].split("'", 1)[0]))
    assert vals == {"query": "How do I add a supplier?", "file_ids": "", "conversation_id": "conv-1",
                    "operation_id": _QUESTION}


@pytest.mark.asyncio
async def test_failure_that_cannot_be_retried_offers_no_ask_again(ui_client):
    r, _ = await _send(ui_client, result={
        "answer": "", "error": t("ai.err_question_expired"), "error_code": "operation_expired",
        "retryable": False, "message_id": _QUESTION, "asked_again": False})
    assert "X-AI-Clear-Composer" not in r.headers
    assert "ai-msg__again" not in r.text


@pytest.mark.asyncio
async def test_question_asked_again_adds_no_second_question_bubble(ui_client):
    r, _ = await _send(ui_client, result={
        "answer": "Open Contacts.", "pending_actions": [], "message_id": _QUESTION, "asked_again": True})
    assert "ai-msg--user" not in r.text
    assert "Open Contacts." in r.text and r.headers["X-AI-Clear-Composer"] == "1"


@pytest.mark.asyncio
async def test_refused_question_keeps_its_id(ui_client):
    r, _ = await _send(ui_client, error=_refusal(402, {**_FREE_USED_UP, "message_id": _QUESTION,
                                                       "asked_again": False}))
    assert "X-AI-Clear-Composer" not in r.headers
    assert _operation_value(r.text) == _QUESTION


@pytest.mark.asyncio
async def test_ended_session_on_the_ai_page_goes_to_sign_in(ui_client):
    from ui.api_client import APIError
    ended = APIError(401, "Not authenticated")
    r, _ = await _send(ui_client, error=ended)
    assert r.headers["HX-Redirect"].startswith("/login")

    for name, path in (("get_company", "/ai"), ("ai_conversations_list", "/ai/conversations-list")):
        with patch(f"celerp_ai.ui_routes.api.{name}", AsyncMock(side_effect=ended)):
            r = await ui_client.get(path, headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect", "").startswith("/login"), (path, r.status_code)

    with patch("celerp_ai.ui_routes.api.get_company", AsyncMock(return_value={"settings": {}})), \
            patch("celerp_ai.ui_routes.api.ai_quota_status", AsyncMock(return_value={"tier": "ai", "remaining": 5})), \
            patch("celerp_ai.ui_routes.api.ai_conversation_get", AsyncMock(side_effect=ended)):
        r = await ui_client.get(f"/ai?conversation={_QUESTION}")
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_reloaded_thread_explains_refusals_and_offers_ask_again_last_only():
    from celerp_ai.ui_routes import _thread

    def _user(i):
        return {"id": f"u{i}", "role": "user", "content": f"question {i}", "created_at": f"2026-10-23T00:0{i}:00"}

    def _failed(i, **kw):
        return {"id": f"a{i}", "role": "assistant", "error": True, "created_at": f"2026-10-23T00:0{i}:30", **kw}

    paused = {"code": "free_paused", "message": "paused", "resets_at": "2026-11-01T00:00:00+00:00"}
    lost = dict(content=t("ai.err_outcome_unknown"), error_code="outcome_unknown", retryable=True)
    messages = [
        _user(1), _failed(1, content="paused", error_code="free_paused", refusal=paused, refusal_status=503),
        _user(2), _failed(2, **lost),
        _user(3), _failed(3, **lost),
    ]
    html = "".join(to_xml(ft) for ft in _thread(messages, [], "conv-1", "en"))
    assert t("ai.free_paused", date="2026-11-01") in html
    assert html.count("ai-msg__again") == 1
    assert '"operation_id": "u3"' in html and "question 3" in html

    # A last reply that cannot be retried offers nothing.
    expired = _failed(3, content=t("ai.err_question_expired"), error_code="operation_expired", retryable=False)
    html = "".join(to_xml(ft) for ft in _thread([*messages[:-1], expired], [], "conv-1", "en"))
    assert "ai-msg__again" not in html
