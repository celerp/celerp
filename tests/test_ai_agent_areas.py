# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Area loading in the agent loop: the model starts with the core tools and two meta
tools, lists an area's tools, and opens the ones it needs. Every listing and opening
is measured against the request budget before it reaches the history, so a run never
outgrows one model call because of them.

Also the download link (a download tool answers a link the answer shows) and the
outside warning on a send card.

The model is scripted (``celerp.ai.service.complete`` replaced); no relay call is made.
"""
from __future__ import annotations

import json
import os
import uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp import agent_markers
from celerp.ai import jobs, service
from celerp.ai import tools as ai_tools
from celerp.ai.llm import ModelResult
from celerp.ai.service import (
    LIST_TOOLS,
    MAX_META_CALLS_PER_RUN,
    OPEN_TOOLS,
    REQUEST_TEXT_BUDGET,
    _request_text_bytes,
    agent_system_prompt,
    run_agent,
)
from celerp.help.registry import get_index
from celerp.help.search import module_enabled
from test_helpers import real_agent_app
from ui.i18n import t


def _cap(name: str, module: str, *, confirm: bool = False, params: dict | None = None,
         download: str | None = None, path_names: tuple = ()) -> dict:
    return {
        "name": name, "method": "POST" if confirm else "GET", "path": f"/{name}", "module": module,
        "path_names": path_names, "query_names": (), "expects_body": confirm,
        "key_field": None, "keyed": False, "requires_confirmation": confirm,
        "result_permissions": (), "field_visibility": False, "download": download,
        "summary": name.replace("_", " ").capitalize(), "tool": {"type": "function", "function": {
            "name": name, "description": f"Do {name.replace('_', ' ')}.", "parameters": params or {}}},
    }


CAPS = {c["name"]: c for c in (
    _cap("search", "celerp"),
    _cap("help_search", "celerp"),
    _cap("help_read", "celerp"),
    _cap("list_items", "celerp-inventory"),
    _cap("get_item", "celerp-inventory"),
    _cap("create_item", "celerp-inventory", confirm=True),
    _cap("list_contacts", "celerp-contacts"),
)}


def _turn(content: str = "", calls: list[tuple[str, dict, str]] | None = None) -> ModelResult:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for name, args, cid in calls
        ]
    return ModelResult(message=message, model_used="test-model", usage={"credits": 1},
                       reservation_id="r", remaining=10)


async def _owner_authority(company_id, user_id):
    return "owner", {}


class _Model:
    """Scripted turns; records what each model call carried."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def __call__(self, messages, *, tools=None, reservation_id=None, **_kw):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": list(tools or [])})
        return self.turns.pop(0)

    def tool_result(self, call: int, call_id: str) -> dict:
        for m in self.calls[call]["messages"]:
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
                return json.loads(m["content"])
        raise AssertionError(f"no tool message for {call_id}")

    def tool_names(self, call: int) -> set[str]:
        return {spec["function"]["name"] for spec in self.calls[call]["tools"]}

    def sizes(self) -> list[int]:
        return [_request_text_bytes(c["messages"], c["tools"]) for c in self.calls]


def _script(monkeypatch, turns, caps=CAPS, executed=None) -> _Model:
    model = _Model(turns)
    monkeypatch.setattr(service, "complete", model)
    monkeypatch.setattr(service, "_authority", _owner_authority)
    if caps is not None:
        monkeypatch.setattr(service, "compile_agent_capabilities",
                            lambda app, settings, role=None, **_gates: caps)

    async def _execute(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        if executed is not None:
            executed.append(capability["name"])
        if capability.get("download"):
            return {"ok": True, "status": 200, "data": {"download": capability["download"]}}
        return {"ok": True, "status": 200, "data": {"rows": []}}
    monkeypatch.setattr(service, "execute_agent_capability", _execute)
    return model


async def _run(**over):
    kwargs = dict(
        app=object(), bearer=lambda: "Bearer tok", query="a question",
        company_id=uuid.uuid4(), company_settings={}, user_id=uuid.uuid4(),
        role="owner", memory={"notes": [], "kv": {}}, file_ids=None, history=[],
        operation_id=uuid.uuid4(),
    )
    kwargs.update(over)
    kwargs.setdefault("journal", jobs.Journal(kwargs["operation_id"]))
    return await run_agent(**kwargs)


def _filler(nbytes: int) -> dict:
    """Company memory of about ``nbytes``: it rides in the system message of every model
    call, as a history at its trimmed maximum does."""
    return {"notes": [{"content": "x" * nbytes}], "kv": {}}


def _error_code(result: dict) -> str | None:
    return None if result.get("ok", True) else (result.get("error") or {}).get("code")


@pytest.fixture
def real_app():
    """Built per test: the autouse loader reset unloads the modules after each one."""
    return real_agent_app()


# -- The core set and the meta tools ----------------------------------------

@pytest.mark.asyncio
async def test_first_request_carries_only_the_core_set(monkeypatch):
    model = _script(monkeypatch, [_turn("done")])
    await _run()
    assert model.tool_names(0) == {"search", "help_search", "help_read", "propose_for_each",
                                   LIST_TOOLS, OPEN_TOOLS}


@pytest.mark.asyncio
async def test_list_tools_refuses_an_unknown_area(monkeypatch):
    model = _script(monkeypatch, [_turn(calls=[(LIST_TOOLS, {"area": "no-such-area"}, "c1")]), _turn("done")])
    await _run()
    result = model.tool_result(1, "c1")
    assert _error_code(result) == "unknown_area"
    assert "celerp-inventory" in result["error"]["message"]


@pytest.mark.asyncio
async def test_list_tools_empty_area(monkeypatch):
    # celerp-backup always runs (folded into core); this caller has no tool there.
    model = _script(monkeypatch, [_turn(calls=[(LIST_TOOLS, {"area": "celerp-backup"}, "c1")]), _turn("done")])
    await _run()
    result = model.tool_result(1, "c1")
    assert result["ok"] is True and result["tools"] == []
    assert result["note"] == service.EMPTY_AREA


@pytest.mark.asyncio
async def test_list_tools_names_each_tool_and_whether_it_changes_data(monkeypatch):
    model = _script(monkeypatch, [_turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]), _turn("done")])
    await _run()
    assert model.tool_result(1, "c1")["tools"] == [
        {"name": "create_item", "description": "Do create item.", "changes": True},
        {"name": "get_item", "description": "Do get item.", "changes": False},
        {"name": "list_items", "description": "Do list items.", "changes": False},
    ]


@pytest.mark.asyncio
async def test_list_tools_leaves_out_changes_in_read_only_mode(monkeypatch):
    model = _script(monkeypatch, [_turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]), _turn("done")])
    await _run(read_only=True)
    assert {row["name"] for row in model.tool_result(1, "c1")["tools"]} == {"get_item", "list_items"}


@pytest.mark.asyncio
async def test_list_tools_hides_tools_the_role_cannot_use(monkeypatch, real_app):
    viewer = ai_tools.compile_agent_capabilities(real_app, {}, "viewer", caller_gates=frozenset())
    owner = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    area = "celerp-inventory"
    model = _script(monkeypatch, [_turn(calls=[(LIST_TOOLS, {"area": area}, "c1")]), _turn("done")], caps=None)
    await _run(app=real_app, role="viewer")
    listed = {row["name"] for row in model.tool_result(1, "c1")["tools"]}
    assert listed == {n for n, c in viewer.items() if c["module"] == area}
    assert {n for n, c in owner.items() if c["module"] == area} - listed


@pytest.mark.asyncio
async def test_open_tools_refuses_unlisted_names(monkeypatch):
    model = _script(monkeypatch, [_turn(calls=[(OPEN_TOOLS, {"names": ["get_item"]}, "c1")]), _turn("done")])
    await _run()
    assert _error_code(model.tool_result(1, "c1")) == "unlisted_tool"
    assert "get_item" not in model.tool_names(1)


@pytest.mark.asyncio
async def test_open_tools_adds_the_tools_for_later_turns(monkeypatch):
    executed: list[str] = []
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": ["get_item", "list_items"]}, "c2")]),
        _turn(calls=[("list_items", {}, "c3")]),
        _turn("done"),
    ], executed=executed)
    result = await _run()
    assert model.tool_result(2, "c2") == {"ok": True, "opened": ["get_item", "list_items"]}
    assert {"get_item", "list_items"} <= model.tool_names(2)
    assert "create_item" not in model.tool_names(3)
    assert executed == ["list_items"]
    assert result.tools_called == ["list_items"]


@pytest.mark.asyncio
async def test_open_tools_takes_at_most_ten_names(monkeypatch):
    names = [f"tool_{i}" for i in range(11)]
    caps = {**CAPS, **{n: _cap(n, "celerp-inventory") for n in names}}
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": names}, "c2")]),
        _turn("done"),
    ], caps=caps)
    await _run()
    assert _error_code(model.tool_result(2, "c2")) == "invalid_arguments"
    assert not set(names) & model.tool_names(2)


@pytest.mark.asyncio
async def test_unopened_tool_is_refused(monkeypatch):
    executed: list[str] = []
    model = _script(monkeypatch, [_turn(calls=[("list_items", {}, "c1")]), _turn("done")], executed=executed)
    await _run()
    result = model.tool_result(1, "c1")
    assert _error_code(result) == "unknown_capability"
    assert result["error"]["message_key"] == "ai.action_unavailable"
    assert OPEN_TOOLS in result["error"]["message"]
    assert executed == []


@pytest.mark.asyncio
async def test_unopened_change_is_never_proposed(monkeypatch):
    model = _script(monkeypatch, [_turn(calls=[("create_item", {"body": {}}, "c1")]), _turn("done")])
    result = await _run()
    assert result.pending_actions == []
    assert _error_code(model.tool_result(1, "c1")) == "unknown_capability"


@pytest.mark.asyncio
async def test_meta_calls_do_not_count_toward_the_tool_call_cap_and_are_capped(monkeypatch):
    lists = [(LIST_TOOLS, {"area": "celerp-inventory"}, f"m{i}") for i in range(MAX_META_CALLS_PER_RUN + 1)]
    model = _script(monkeypatch, [
        _turn(calls=lists[:4]),
        _turn(calls=lists[4:]),
        _turn(calls=[("search", {}, f"s{i}") for i in range(service.MAX_TOOL_CALLS_PER_TURN)]),
        _turn("done"),
    ])
    result = await _run()
    assert all(model.tool_result(2, f"m{i}")["ok"] for i in range(MAX_META_CALLS_PER_RUN))
    assert _error_code(model.tool_result(2, f"m{MAX_META_CALLS_PER_RUN}")) == "meta_limit"
    assert result.tools_called == ["search"] * service.MAX_TOOL_CALLS_PER_TURN


@pytest.mark.asyncio
async def test_meta_calls_are_journal_steps_and_a_resumed_run_opens_the_same_tools(monkeypatch):
    turns = [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": ["get_item"]}, "c2")]),
        _turn("done"),
    ]
    model = _script(monkeypatch, list(turns))
    journal = jobs.Journal(uuid.uuid4())
    await _run(journal=journal)
    assert journal.stored("meta:c1")["result"]["tools"]
    assert journal.stored("meta:c2")["result"] == {"ok": True, "opened": ["get_item"]}
    assert journal.stored_reads() == {}

    # Asked again under the same journal: every step replays and no meta call reruns.
    model = _script(monkeypatch, [])
    again = await _run(journal=journal, operation_id=journal.question_id)
    assert again.answer == "done" and model.calls == []


@pytest.mark.asyncio
async def test_a_meta_call_shares_a_turn_with_a_change_as_a_read_does(monkeypatch):
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": ["create_item"]}, "c2")]),
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-contacts"}, "c3"), ("create_item", {"body": {}}, "c4")]),
        _turn("done"),
    ])
    result = await _run()
    assert result.pending_actions == []
    assert model.tool_result(3, "c3")["ok"] is True
    assert _error_code(model.tool_result(3, "c4")) == "mixed_turn"


# -- The budget ---------------------------------------------------------------

def _big_cap(name: str, nbytes: int, module: str = "celerp-inventory") -> dict:
    return _cap(name, module, params={"type": "object", "description": "y" * nbytes})


@pytest.mark.asyncio
async def test_open_tools_refuses_past_the_budget(monkeypatch):
    caps = {**CAPS, "huge": _big_cap("huge", REQUEST_TEXT_BUDGET // 2)}
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": ["huge", "get_item"]}, "c2")]),
        _turn("done"),
    ], caps=caps)
    result = await _run(memory=_filler(REQUEST_TEXT_BUDGET // 2))
    refused = model.tool_result(2, "c2")
    assert _error_code(refused) == "over_budget"
    assert "bytes" in refused["error"]["message"]
    assert not {"huge", "get_item"} & model.tool_names(2)
    assert max(model.sizes()) <= REQUEST_TEXT_BUDGET
    assert result.error_code is None and result.answer == "done"


@pytest.mark.asyncio
async def test_list_tools_refuses_past_the_budget(monkeypatch):
    many = {f"tool_{i:03d}": _cap(f"tool_{i:03d}", "celerp-inventory") for i in range(400)}
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-inventory"}, "c1")]),
        _turn("done"),
    ], caps={**CAPS, **many})
    result = await _run(memory=_filler(REQUEST_TEXT_BUDGET - 20_000))
    refused = model.tool_result(1, "c1")
    assert _error_code(refused) == "over_budget"
    assert "tools" not in refused
    assert max(model.sizes()) <= REQUEST_TEXT_BUDGET
    assert result.error_code is None


def _areas_by_size(caps: dict) -> list[str]:
    totals: dict[str, int] = {}
    for cap in caps.values():
        area = service.agent_area(cap)
        totals[area] = totals.get(area, 0) + len(json.dumps(cap["tool"]))
    return sorted(totals, key=lambda a: -totals[a])


@pytest.mark.asyncio
async def test_largest_open_set_fits_the_budget(monkeypatch, real_app):
    """The largest specs a run can open, beside the core set and a history at its trimmed
    maximum, fit the budget, or open_tools refuses before passing it."""
    caps = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    areas = _areas_by_size(caps)[:2]
    largest = sorted((n for n, c in caps.items() if service.agent_area(c) in areas),
                     key=lambda n: -len(json.dumps(caps[n]["tool"])))[:40]
    opens = [(OPEN_TOOLS, {"names": largest[i:i + 10]}, f"o{i}") for i in range(0, 40, 10)]
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": a}, f"l{a}") for a in areas]),
        _turn(calls=opens),
        _turn("done"),
    ], caps=caps)
    result = await _run(memory=_filler(REQUEST_TEXT_BUDGET // 3))
    assert max(model.sizes()) <= REQUEST_TEXT_BUDGET
    assert result.error_code is None and result.answer == "done"
    for _name, _args, cid in opens:
        assert _error_code(model.tool_result(2, cid)) in (None, "over_budget")


@pytest.mark.asyncio
async def test_largest_list_tools_results_fit_the_budget(monkeypatch, real_app):
    """Every area the meta cap lets a run list, largest first, beside the core set and a
    history at its trimmed maximum, fits the budget, or list_tools refuses first."""
    caps = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    areas = _areas_by_size(caps)[:MAX_META_CALLS_PER_RUN]
    model = _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": a}, f"l{i}") for i, a in enumerate(areas[:3])]),
        _turn(calls=[(LIST_TOOLS, {"area": a}, f"l{i}") for i, a in enumerate(areas[3:], 3)]),
        _turn("done"),
    ], caps=caps)
    result = await _run(memory=_filler(REQUEST_TEXT_BUDGET * 2 // 3))
    assert max(model.sizes()) <= REQUEST_TEXT_BUDGET
    assert result.error_code is None and result.answer == "done"
    codes = [_error_code(model.tool_result(2, f"l{i}")) for i in range(len(areas))]
    assert set(codes) <= {None, "over_budget"} and "over_budget" in codes


@pytest.mark.asyncio
async def test_agent_request_fits_the_budget(monkeypatch, real_app):
    """With every module on, the first model call stays well under the budget."""
    model = _script(monkeypatch, [_turn("done")], caps=None)
    await _run(app=real_app)
    full = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    assert len(full) > 300
    assert model.sizes()[0] <= REQUEST_TEXT_BUDGET // 4


# -- The prompts ------------------------------------------------------------

def test_operator_prompt_names_every_area(real_app):
    caps = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    prompt = agent_system_prompt("en", caps, {})
    areas = service.agent_areas({})
    used = {service.agent_area(c) for c in caps.values()}
    assert len(used) > 5
    for area in used:
        assert f"{area} ({areas[area]})" in prompt, area


def test_every_area_folded_into_core_carries_its_module_label():
    from celerp.modules.loader import CORE_FOLDED, module_label

    areas = service.agent_areas({})
    assert CORE_FOLDED
    for name in CORE_FOLDED:
        assert areas[name] and areas[name] == module_label(name), name


def test_operator_prompt_names_the_ask_groups():
    prompt = agent_system_prompt("en", CAPS, {})
    assert agent_markers.USER_ACTION_EXCLUSIONS
    assert set(agent_markers.USER_ACTION_EXCLUSIONS) <= set(agent_markers.AGENT_EXCLUSIONS)
    for reason in agent_markers.USER_ACTION_EXCLUSIONS.values():
        assert reason in prompt
    for key in set(agent_markers.AGENT_EXCLUSIONS) - set(agent_markers.USER_ACTION_EXCLUSIONS):
        assert agent_markers.AGENT_EXCLUSIONS[key] not in prompt


def test_help_prompt_names_every_help_module():
    prompt = agent_system_prompt("en", CAPS, {})
    shown = {p.module_display for p in get_index().pages.values() if module_enabled(p, {})}
    assert len(shown) > 3
    for display in shown:
        assert display in prompt, display


# -- Downloads and the outside warning ---------------------------------------

@pytest.mark.asyncio
async def test_download_tool_result_reaches_the_answer(monkeypatch):
    caps = {**CAPS, "doc_pdf": _cap("doc_pdf", "celerp-docs", download="/docs/{entity_id}/pdf")}
    _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-docs"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": ["doc_pdf"]}, "c2")]),
        _turn(calls=[("doc_pdf", {}, "c3")]),
        _turn("Here is the PDF."),
    ], caps=caps)
    result = await _run()
    assert result.downloads == ["/docs/{entity_id}/pdf"]


def test_download_link_renders_in_the_outcome_line():
    from celerp.ai.conversations import answer_links, downloads
    from celerp_ai.ui_routes import _msg_bubble

    record = answer_links(None, ["/docs/abc/pdf"])
    assert downloads(record) == ["/docs/abc/pdf"]
    html = str(_msg_bubble("ai", "Here is the PDF.", downloads=downloads(record), lang="en"))
    assert 'href="/docs/abc/pdf"' in html and "table-link" in html
    assert t("ai.download_file", "en") in html


@pytest.mark.asyncio
async def test_outside_message_card_warns(monkeypatch, real_app):
    caps = ai_tools.compile_agent_capabilities(real_app, {}, "owner", caller_gates=frozenset())
    send = next(n for n, c in caps.items() if (c["method"], c["path"]) == ("POST", "/docs/{entity_id}/send"))
    assert caps[send]["outside"] is True
    args = {"path": {"entity_id": "d1"}, "body": {"sent_to": "buyer@example.com"}}
    _script(monkeypatch, [
        _turn(calls=[(LIST_TOOLS, {"area": "celerp-docs"}, "c1")]),
        _turn(calls=[(OPEN_TOOLS, {"names": [send]}, "c2")]),
        _turn(calls=[(send, args, "c3")]),
    ], caps=caps)
    result = await _run()
    [card] = result.pending_actions
    assert t("ai.warn_outside_message") in [w["text"] for w in card.warnings]

    from celerp_ai.ui_routes import _action_body
    html = "".join(str(part) for part in _action_body({"name": send, "shown": card.shown,
                                                       "warnings": card.warnings}, "en"))
    assert t("ai.warn_outside_message", "en") in html
    assert "buyer@example.com" in html
