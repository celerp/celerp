# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Deterministic state-machine tests for the agent loop (service.run_agent).

`complete` is replaced by a fake that returns scripted ModelResults built from
the recorded relay wire shapes in _fixtures (tool_calls carry the real id/type
and arguments as a JSON string). The FastAPI app is never built and the relay is
never called: compile_agent_capabilities and execute_agent_capability are stubbed
so the loop's control flow - reads, mutations, error results, caps, threading -
is exercised in isolation.
"""

from __future__ import annotations

import json
import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.ai import service
from celerp.ai.service import (
    MAX_ARGUMENT_BYTES,
    MAX_MODEL_TURNS,
    MAX_TOOL_CALLS_PER_TURN,
    MAX_TOTAL_RESULT_BYTES,
    AgentResult,
    _EMPTY_ANSWER,
    run_agent,
)

from _fixtures import raw_tool_call, tool_call, tool_result


# -- Fake capabilities (shape mirrors compile_agent_capabilities output) ----

def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {}}}


CAPS = {
    "list_items": {
        "name": "list_items", "method": "GET", "path": "/api/inventory/items",
        "path_names": (), "query_names": ("q",), "expects_body": False,
        "inject_idempotency": False, "tool": _tool("list_items"),
    },
    "get_item": {
        "name": "get_item", "method": "GET", "path": "/api/inventory/items/{item_id}",
        "path_names": ("item_id",), "query_names": (), "expects_body": False,
        "inject_idempotency": False, "tool": _tool("get_item"),
    },
    "create_contact": {
        "name": "create_contact", "method": "POST", "path": "/api/crm/contacts",
        "path_names": (), "query_names": (), "expects_body": True,
        "inject_idempotency": True, "tool": _tool("create_contact"),
    },
}


class FakeComplete:
    """Return scripted ModelResults in order, recording every call's inputs."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, messages, *, tools=None, tool_choice=None, reservation_id=None, **kw):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
            "reservation_id": reservation_id,
        })
        return self._results.pop(0)


def _install(monkeypatch, results, execute=None):
    """Wire the fake complete/compile/execute into the service module."""
    fake = FakeComplete(results)
    monkeypatch.setattr(service, "complete", fake)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings: CAPS)
    if execute is not None:
        monkeypatch.setattr(service, "execute_agent_capability", execute)
    return fake


def _execute_returning(result):
    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        return result
    return _exec


async def _run(**over):
    kwargs = dict(
        app=object(), authorization="Bearer tok", query="a question",
        company_id=uuid.uuid4(), company_settings={}, user_id=uuid.uuid4(),
        memory={"notes": [], "kv": {}}, file_ids=None, history=[],
    )
    kwargs.update(over)
    return await run_agent(**kwargs)


def _tool_message(fake: FakeComplete, call_index: int, call_id: str) -> dict:
    """Find the role=tool message for call_id in the messages of the given complete() call."""
    for msg in fake.calls[call_index]["messages"]:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            return json.loads(msg["content"])
    raise AssertionError(f"no tool message for {call_id} in call {call_index}")


# -- Core control flow ------------------------------------------------------

@pytest.mark.asyncio
async def test_no_tools_returns_answer_first_turn(monkeypatch):
    _install(monkeypatch, [tool_result(content="You currently have 42 items.")])
    result = await _run()
    assert isinstance(result, AgentResult)
    assert result.answer == "You currently have 42 items."
    assert result.tools_called == []
    assert result.pending_actions == []
    assert result.error is None


@pytest.mark.asyncio
async def test_read_call_executes_and_feeds_result_back(monkeypatch):
    executed = []

    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        executed.append((capability["name"], arguments, call_id))
        return {"ok": True, "status": 200, "data": {"items": [{"sku": "WDG-1"}]}}

    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("list_items", {"query": {"q": "widget"}}, "call_read_1")]),
            tool_result(content="Found 1 widget."),
        ],
        execute=_exec,
    )
    result = await _run()
    assert result.answer == "Found 1 widget."
    assert result.tools_called == ["list_items"]
    assert executed == [("list_items", {"query": {"q": "widget"}}, "call_read_1")]
    fed = _tool_message(fake, 1, "call_read_1")
    assert fed == {"ok": True, "status": 200, "data": {"items": [{"sku": "WDG-1"}]}}


@pytest.mark.asyncio
async def test_mutation_call_executes_nothing_and_returns_pending(monkeypatch):
    calls = []

    async def _exec(*a, **k):
        calls.append(a)
        return {"ok": True, "status": 200, "data": {}}

    _install(
        monkeypatch,
        [tool_result(content="I'll create Acme.", calls=[tool_call("create_contact", {"body": {"name": "Acme"}}, "call_mut_1")])],
        execute=_exec,
    )
    result = await _run()
    assert calls == []
    assert result.answer == "I'll create Acme."
    assert len(result.pending_actions) == 1
    pending = result.pending_actions[0]
    assert pending.name == "create_contact"
    assert pending.id == "call_mut_1"
    assert pending.arguments == {"body": {"name": "Acme"}}
    assert pending.created_at and pending.expires_at


@pytest.mark.asyncio
async def test_mixed_read_and_mutation_drops_reads_stores_mutations(monkeypatch):
    calls = []

    async def _exec(*a, **k):
        calls.append(a)
        return {"ok": True, "status": 200, "data": {}}

    _install(
        monkeypatch,
        [tool_result(calls=[
            tool_call("list_items", {"query": {"q": "x"}}, "r1"),
            tool_call("create_contact", {"body": {"name": "A"}}, "m1"),
        ])],
        execute=_exec,
    )
    result = await _run()
    assert calls == []
    assert result.tools_called == []
    assert [p.name for p in result.pending_actions] == ["create_contact"]
    assert result.pending_actions[0].id == "m1"


@pytest.mark.asyncio
async def test_content_and_tool_calls_together_keeps_content(monkeypatch):
    _install(
        monkeypatch,
        [tool_result(content="Proposing a new contact.", calls=[tool_call("create_contact", {"body": {"name": "A"}}, "m1")])],
        execute=_execute_returning({"ok": True, "status": 200, "data": {}}),
    )
    result = await _run()
    assert result.answer == "Proposing a new contact."
    assert len(result.pending_actions) == 1


@pytest.mark.asyncio
async def test_empty_content_no_tool_calls_yields_placeholder_answer(monkeypatch):
    _install(monkeypatch, [tool_result(content="")])
    result = await _run()
    assert result.answer == _EMPTY_ANSWER
    assert result.answer != ""


# -- Error tool results (surfaced to the model, not the user) ---------------

@pytest.mark.asyncio
async def test_unknown_capability_returns_error_result_and_continues(monkeypatch):
    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("does_not_exist", {}, "u1")]),
            tool_result(content="Recovered."),
        ],
        execute=_execute_returning({"ok": True, "status": 200, "data": {}}),
    )
    result = await _run()
    assert result.answer == "Recovered."
    fed = _tool_message(fake, 1, "u1")
    assert fed["ok"] is False
    assert fed["error"]["code"] == "unknown_capability"


@pytest.mark.asyncio
async def test_invalid_json_arguments_returns_error_result(monkeypatch):
    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[raw_tool_call("list_items", "{not valid json", "b1")]),
            tool_result(content="Recovered."),
        ],
        execute=_execute_returning({"ok": True, "status": 200, "data": {}}),
    )
    result = await _run()
    assert result.answer == "Recovered."
    fed = _tool_message(fake, 1, "b1")
    assert fed["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_flat_arguments_rejected_with_shape_hint(monkeypatch):
    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        if set(arguments) - {"path", "query", "body"}:
            return {"ok": False, "status": 0,
                    "error": {"code": "invalid_arguments", "message": "Tool arguments contain unsupported sections."}}
        return {"ok": True, "status": 200, "data": {"items": [{"sku": "WDG"}]}}

    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("list_items", {"q": "x"}, "f1")]),
            tool_result(calls=[tool_call("list_items", {"query": {"q": "x"}}, "f2")]),
            tool_result(content="Found it."),
        ],
        execute=_exec,
    )
    result = await _run()
    assert result.answer == "Found it."
    fed = _tool_message(fake, 1, "f1")
    assert fed["error"]["code"] == "invalid_arguments"
    assert fed["expected_sections"] == ["query"]


@pytest.mark.asyncio
async def test_missing_path_param_returns_error_result(monkeypatch):
    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        if capability["name"] == "get_item" and "path" not in arguments:
            return {"ok": False, "status": 0,
                    "error": {"code": "invalid_arguments", "message": "A required path parameter is missing."}}
        return {"ok": True, "status": 200, "data": {"item": {"sku": "WDG"}}}

    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("get_item", {}, "p1")]),
            tool_result(content="Which item did you mean?"),
        ],
        execute=_exec,
    )
    result = await _run()
    assert result.answer == "Which item did you mean?"
    fed = _tool_message(fake, 1, "p1")
    assert fed["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_tool_404_surfaces_to_model_not_as_run_error(monkeypatch):
    _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("get_item", {"path": {"item_id": "missing"}}, "n1")]),
            tool_result(content="I could not find that item."),
        ],
        execute=_execute_returning({"ok": False, "status": 404, "data": {"detail": "Not found"}}),
    )
    result = await _run()
    assert result.error is None
    assert result.answer == "I could not find that item."


@pytest.mark.asyncio
async def test_tool_403_surfaces_to_model_not_as_run_error(monkeypatch):
    _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("list_items", {"query": {"q": "x"}}, "d1")]),
            tool_result(content="You do not have permission to view inventory."),
        ],
        execute=_execute_returning({"ok": False, "status": 403, "data": {"detail": "Forbidden"}}),
    )
    result = await _run()
    assert result.error is None
    assert "permission" in result.answer


# -- Caps -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_cap_returns_error(monkeypatch):
    reads = [
        tool_result(calls=[tool_call("list_items", {"query": {"q": str(i)}}, f"c{i}")])
        for i in range(MAX_MODEL_TURNS)
    ]
    _install(monkeypatch, reads, execute=_execute_returning({"ok": True, "status": 200, "data": {"items": []}}))
    result = await _run()
    assert result.error is not None
    assert "allowed steps" in result.error


@pytest.mark.asyncio
async def test_tool_calls_per_turn_cap(monkeypatch):
    over = [tool_call("list_items", {"query": {"q": str(i)}}, f"t{i}") for i in range(MAX_TOOL_CALLS_PER_TURN + 1)]
    calls = []

    async def _exec(*a, **k):
        calls.append(a)
        return {"ok": True, "status": 200, "data": {}}

    _install(monkeypatch, [tool_result(calls=over)], execute=_exec)
    result = await _run()
    assert calls == []
    assert result.error is not None
    assert "too many operations" in result.error


@pytest.mark.asyncio
async def test_total_result_bytes_cap(monkeypatch):
    big = {"ok": True, "status": 200, "data": {"blob": "x" * 130000}}
    _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("list_items", {"query": {"q": "a"}}, "b1")]),
            tool_result(calls=[tool_call("list_items", {"query": {"q": "b"}}, "b2")]),
        ],
        execute=_execute_returning(big),
    )
    assert 130000 < MAX_TOTAL_RESULT_BYTES < 260000
    result = await _run()
    assert result.error is not None
    assert "Too much data" in result.error


@pytest.mark.asyncio
async def test_argument_bytes_cap(monkeypatch):
    huge = json.dumps({"query": {"q": "x" * (MAX_ARGUMENT_BYTES + 100)}})
    assert len(huge.encode("utf-8")) > MAX_ARGUMENT_BYTES
    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[raw_tool_call("list_items", huge, "a1")]),
            tool_result(content="Recovered."),
        ],
        execute=_execute_returning({"ok": True, "status": 200, "data": {}}),
    )
    result = await _run()
    assert result.answer == "Recovered."
    fed = _tool_message(fake, 1, "a1")
    assert fed["error"]["code"] == "invalid_arguments"


# -- Relay threading and failure mapping ------------------------------------

@pytest.mark.asyncio
async def test_model_used_comes_from_relay(monkeypatch):
    _install(monkeypatch, [tool_result(content="ok", model_used="z-ai/glm-5.3-flash")])
    result = await _run()
    assert result.model_used == "z-ai/glm-5.3-flash"


@pytest.mark.asyncio
async def test_reservation_id_threaded_between_turns(monkeypatch):
    fake = _install(
        monkeypatch,
        [
            tool_result(calls=[tool_call("list_items", {"query": {"q": "x"}}, "c1")], reservation_id="RESV-XYZ"),
            tool_result(content="done", reservation_id="RESV-XYZ"),
        ],
        execute=_execute_returning({"ok": True, "status": 200, "data": {}}),
    )
    await _run()
    assert fake.calls[0]["reservation_id"] is None
    assert fake.calls[1]["reservation_id"] == "RESV-XYZ"


@pytest.mark.asyncio
async def test_timeout_maps_to_error(monkeypatch):
    import asyncio

    async def _slow(messages, *, tools=None, tool_choice=None, reservation_id=None, **kw):
        await asyncio.sleep(5)
        return tool_result(content="never")

    monkeypatch.setattr(service, "AGENT_RUN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(service, "complete", _slow)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings: CAPS)
    result = await _run()
    assert result.error is not None
    assert "too long" in result.error


@pytest.mark.asyncio
async def test_relay_409_continuation_expired_maps_to_plain_error(monkeypatch):
    async def _raise(messages, *, tools=None, tool_choice=None, reservation_id=None, **kw):
        raise RuntimeError("continuation_expired")

    monkeypatch.setattr(service, "complete", _raise)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings: CAPS)
    result = await _run()
    assert result.pending_actions == []
    assert result.error == "The conversation step expired, ask the question again."
