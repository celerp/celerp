# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""This app against an AI service released before free questions and operation status.
The older service is played over HTTP as it answers: it ignores the question's
operation id, refuses a free account with its plain "not available" 402, answers no
free question count, and has no /ai/operations route (404). The app shows its
existing upgrade path. Its status read failing is a failed read like any other: a
row waiting on it stays awaiting confirmation.
"""
from __future__ import annotations

import json
import uuid

import httpx

from celerp.ai import llm, service, usage
from celerp.gateway.state import set_session_token
from celerp.routers.health import _ai_counts
from test_ai_usage import _age, _asked, _claims, _conversation, _messages, _usage, owner  # noqa: F401
from test_ai_usage_reconcile import _rows


class _OlderRelay:
    """The older service's answers to /ai/complete and to an unknown route."""

    def __init__(self, replies: list):
        self.replies = replies
        self.bodies: list[dict] = []
        self.status_reads = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/ai/complete":
            self.status_reads += request.url.path == "/ai/operations/status"
            return httpx.Response(404, json={"detail": "Not Found"})
        assert request.method == "POST"
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _answered(credits: int = 1) -> httpx.Response:
    return httpx.Response(200, json={
        "message": {"role": "assistant", "content": "Open Contacts and choose New."},
        "model_used": "test-model", "usage": {"credits": credits}, "reservation_id": None, "remaining": 9})


_NOT_AVAILABLE = httpx.Response(402, json={"detail": "AI is not available on this plan"})


def _older_relay(monkeypatch, replies: list) -> _OlderRelay:
    relay = _OlderRelay(replies)
    real = httpx.AsyncClient
    set_session_token("s" * 64)
    monkeypatch.setattr(llm, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(llm.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(relay.handle), **kw))
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    return relay


async def test_operation_status_from_an_older_service_is_a_failed_read(monkeypatch):
    relay = _older_relay(monkeypatch, [])
    try:
        await llm.operations_status([uuid.uuid4()])
    except llm.RelayError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("a 404 status read returned")
    assert relay.status_reads == 1


async def test_a_free_account_is_shown_the_upgrade_path(client, session, owner, monkeypatch):
    """The older service has no free questions: its 402 is read as AI not on this plan,
    the thread keeps the refusal, and nothing is counted. It answers no status, so the
    row stays awaiting confirmation at no charge."""
    token, _, _ = owner
    relay = _older_relay(monkeypatch, [_NOT_AVAILABLE])
    conv = await _conversation(client, token)
    question = uuid.uuid4()
    await _asked(client, token, conv, question)
    assert relay.bodies[0]["operation_id"] == str(question)  # sent, and ignored by the older service

    _, reply = await _messages(client, token, conv)
    assert (reply["error_code"], reply["refusal_status"]) == ("not_available", 402)
    await _age(session)
    await usage.reconcile_usage(session)
    assert all((u["query_count"], u["credits_used"]) == (0, 0) for u in (await _usage(client, token)).json()["users"])
    assert await _rows(session) == {question: (0, True)}


def test_no_free_question_count_from_an_older_service():
    """The older /auth/methods answer has no free AI count: the offer shows none."""
    assert _ai_counts({"google": True, "free_email_quota": 10})["free_ai_questions"] is None


async def test_usage_waits_while_the_older_service_answers_no_status(client, session, owner, monkeypatch):
    """A paid question answered by the older service names no operation charge, so its
    row waits for reconcile. The status read finding no route is a failed read: no row
    settles on a guess, each stays awaiting confirmation and is read again next pass."""
    token, _, _ = owner
    relay = _older_relay(monkeypatch, [_answered(credits=1), httpx.ReadTimeout("slow")])
    conv = await _conversation(client, token)
    answered, lost = uuid.uuid4(), uuid.uuid4()
    assert (await _asked(client, token, conv, answered))[1]["error_code"] is None
    assert (await _asked(client, token, conv, lost))[1]["error_code"] == "timeout"
    assert await _rows(session) == {answered: (1, True), lost: (0, True)}

    await _age(session)
    await usage.reconcile_usage(session)
    assert await _rows(session) == {answered: (1, True), lost: (0, True)}
    assert relay.status_reads == 1  # one batch read for both rows
    await usage.reconcile_usage(session)
    assert relay.status_reads == 2  # still pending, so read again
