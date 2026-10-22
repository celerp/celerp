# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Celerp AI and the help pages: the prompt names the user's language, tool calls carry
that language, and help pages the agent read become the answer's citations.

The model is scripted (``celerp.ai.service.complete`` replaced), so no relay call is made.
"""
from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
from fastapi import FastAPI, Request

from celerp.ai import service
from celerp.ai.conversations import (
    _has_protected_action,
    help_pages,
    message_error,
    pending_actions,
    tool_names,
)
from celerp.ai.service import AgentResult, agent_system_prompt, run_agent
from celerp.ai.tools import execute_agent_capability
from celerp.ai.llm import ModelResult
from test_helpers import register_admin
from ui import i18n
from ui.i18n import lang_label


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {}}}


_CAPS = {
    "help_search": {
        "name": "help_search", "method": "GET", "path": "/help/search",
        "path_names": (), "query_names": ("q", "module"), "expects_body": False,
        "inject_idempotency": False, "tool": _tool("help_search"),
    },
    "help_read": {
        "name": "help_read", "method": "GET", "path": "/help/page/{page_id}",
        "path_names": ("page_id",), "query_names": (), "expects_body": False,
        "inject_idempotency": False, "tool": _tool("help_read"),
    },
}


def _turn(content: str = "", calls: list[tuple[str, dict, str]] | None = None) -> ModelResult:
    import json
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for name, args, cid in calls
        ]
    return ModelResult(message=message, model_used="test-model", usage={"credits": 1},
                       reservation_id="r", remaining=10)


def _script(monkeypatch, turns: list[ModelResult]) -> list[list[dict]]:
    seen: list[list[dict]] = []

    async def _complete(messages, *, tools=None, reservation_id=None, **_kw):
        seen.append([dict(m) for m in messages])
        return turns.pop(0)

    monkeypatch.setattr(service, "complete", _complete)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: _CAPS)
    return seen


async def _run(**over) -> AgentResult:
    kwargs = dict(
        app=object(), authorization="Bearer tok", query="how do I add a supplier?",
        company_id=uuid.uuid4(), company_settings={}, user_id=uuid.uuid4(),
        role="owner", memory={"notes": [], "kv": {}}, file_ids=None, history=[],
    )
    kwargs.update(over)
    return await run_agent(**kwargs)


@pytest.fixture
def thai():
    i18n.set_lang("th")
    yield
    i18n.set_lang("en")


@pytest.mark.asyncio
async def test_agent_prompt_names_user_language(monkeypatch, thai):
    prompt = agent_system_prompt("th")
    assert lang_label("th") in prompt
    assert "help_search" in prompt and "help_read" in prompt
    assert "English" in prompt
    assert agent_system_prompt("en") != prompt

    seen = _script(monkeypatch, [_turn("สวัสดี")])
    await _run()
    assert seen[0][0] == {"role": "system", "content": prompt}


@pytest.mark.asyncio
async def test_tool_requests_forward_accept_language(thai):
    app = FastAPI()

    @app.get("/echo")
    async def _echo(request: Request):
        return {"lang": request.headers.get("accept-language")}

    capability = {"name": "echo", "method": "GET", "path": "/echo", "path_names": (),
                  "query_names": (), "expects_body": False, "inject_idempotency": False}
    result = await execute_agent_capability(app, "Bearer tok", capability, {}, "c1")
    assert result["ok"] is True
    assert result["data"] == {"lang": "th"}


@pytest.mark.asyncio
async def test_help_read_results_become_citations(monkeypatch, client):
    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        if capability["name"] == "help_read" and arguments["path"]["page_id"] == "contacts.nothing":
            return {"ok": False, "status": 404, "error": {"code": "not_found", "message": "Help page not found"}}
        return {"ok": True, "status": 200, "data": {}}

    monkeypatch.setattr(service, "execute_agent_capability", _exec)
    _script(monkeypatch, [
        _turn(calls=[("help_search", {"query": {"q": "add supplier"}}, "c1")]),
        _turn(calls=[("help_read", {"path": {"page_id": "contacts.add-supplier"}}, "c2"),
                     ("help_read", {"path": {"page_id": "contacts.nothing"}}, "c3")]),
        _turn("Open Contacts."),
    ])
    result = await _run()
    assert result.help_pages == ["contacts.add-supplier"]

    # A search with no successful read is an empty citation list; no help search at all is None.
    _script(monkeypatch, [_turn(calls=[("help_search", {"query": {"q": "x"}}, "c1")]), _turn("No page.")])
    assert (await _run()).help_pages == []
    _script(monkeypatch, [_turn("Hello.")])
    assert (await _run()).help_pages is None

    # Stored with the assistant message and returned by both the reply and the history.
    from celerp.gateway.state import set_session_token
    set_session_token(secrets.token_hex(32))
    headers = {"Authorization": f"Bearer {await register_admin(client)}"}

    async def _agent(**_kw):
        return AgentResult(answer="Open Contacts.", model_used="m", tools_called=["help_search", "help_read"],
                           pending_actions=[], credits=1, help_pages=["contacts.add-supplier"])

    monkeypatch.setattr(service, "run_agent", _agent)
    conv = (await client.post("/ai/conversations", headers=headers, json={"title": None})).json()["id"]
    r = await client.post(f"/ai/conversations/{conv}/query", headers=headers, json={"query": "supplier?"})
    assert r.status_code == 200, r.text
    assert r.json()["help_pages"] == ["contacts.add-supplier"]
    assert r.json()["tools_called"] == ["help_search", "help_read"]
    detail = (await client.get(f"/ai/conversations/{conv}", headers=headers)).json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert assistant["help_pages"] == ["contacts.add-supplier"]


def test_citation_record_ignored_by_action_helpers():
    records = ["help_search", "help_read", {"help_pages": ["contacts.add-supplier"]}]
    assert tool_names(records) == ["help_search", "help_read"]
    assert pending_actions(records) == []
    assert _has_protected_action(records) is False
    assert message_error(records) is False
    assert help_pages(records) == ["contacts.add-supplier"]
    assert help_pages(["help_search", {"help_pages": []}]) == []
    assert help_pages(["list_items"]) is None
    assert help_pages(None) is None


def test_message_out_carries_help_pages():
    import celerp.main  # noqa: F401  (puts bundled module packages on the import path)
    from celerp_ai.routes import _message_out

    def _msg(tools_called):
        return SimpleNamespace(id=uuid.uuid4(), role="assistant", content="x", model_used="m",
                               tools_called=tools_called, file_ids=None, credits_used=0,
                               created_at=datetime.now(timezone.utc))

    assert _message_out(_msg(["help_read", {"help_pages": ["core.settings"]}])).help_pages == ["core.settings"]
    assert _message_out(_msg(["list_items"])).help_pages is None
