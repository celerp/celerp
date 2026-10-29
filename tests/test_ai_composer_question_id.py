# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The AI composer's question id: the same question sent again keeps its id, a question
the user changed gets a new one, and an id the API will not take again is replaced
only after the API said so, for the user's next send."""

from __future__ import annotations

import html as _html
import json
import re
import uuid

import pytest

import celerp_ai.ui_routes as ai_ui
from ui.api_client import APIError


class _CaptureApp:
    def __init__(self):
        self.handlers: dict = {}

    def __getattr__(self, method):
        return lambda path, **_kw: lambda fn: self.handlers.setdefault((method.upper(), path), fn)


class _FormReq:
    def __init__(self, form: dict):
        self._form = form
        self.method = "POST"
        self.cookies: dict = {}
        self.query_params: dict = {}
        self.headers: dict = {}

    async def form(self):
        return self._form


@pytest.fixture
def chat(monkeypatch):
    sent: list[str] = []
    replies: list = []

    async def _query(token, session_token, conversation_id, query, *, file_ids=None,
                     document_mode="chat", operation_id=""):
        sent.append(operation_id)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ai_ui, "_token", lambda request: "tok")
    monkeypatch.setattr(ai_ui.api, "ai_conversation_query", _query)
    app = _CaptureApp()
    ai_ui.setup_ui_routes(app)
    handler = app.handlers[("POST", "/ai/chat")]

    async def _send(query: str, operation_id: str, question: str = "") -> dict:
        r = await handler(_FormReq({"query": query, "conversation_id": "c1",
                                    "operation_id": operation_id, "operation_question": question}))
        page = r.body.decode()
        return {"id": _value(page, "ai-operation-id"), "question": _value(page, "ai-operation-question")}

    return sent, replies, _send


def _value(page: str, element_id: str) -> str:
    tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', page).group(0)
    return _html.unescape(re.search(r"""value=(["'])(.*?)\1""", tag).group(2))


def _failed(question_id: str) -> dict:
    return {"answer": "", "error": "lost", "error_code": "outcome_unknown", "retryable": True,
            "message_id": question_id, "asked_again": False}


async def test_a_failed_question_sent_again_keeps_its_id(chat):
    sent, replies, send = chat
    first = str(uuid.uuid4())
    replies.extend([_failed(first), _failed(first)])
    kept = await send("supplier?", first)
    assert kept["id"] == first and json.loads(kept["question"]) == ["supplier?", []]
    await send("supplier?", kept["id"], kept["question"])
    assert sent == [first, first]


async def test_a_changed_question_gets_a_new_id(chat):
    sent, replies, send = chat
    first = str(uuid.uuid4())
    replies.extend([_failed(first), _failed("x")])
    kept = await send("supplier?", first)
    await send("customer?", kept["id"], kept["question"])
    assert sent[0] == first and sent[1] not in ("", first)


async def test_an_id_the_api_will_not_take_again_is_replaced_for_the_next_send(chat):
    sent, replies, send = chat
    first = str(uuid.uuid4())
    conflict = {"code": "operation_conflict", "message": "Ask it as a new question."}
    replies.append(APIError(409, conflict, conflict))
    after = await send("supplier?", first)
    assert sent == [first]
    assert after["id"] not in ("", first) and after["question"] == ""
