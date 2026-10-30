# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A question's journal keeps only what the request itself decides, and replays it only
under the authority of whoever asks now.

A read the app refused because of who was asking (signed out, a permission taken away,
a module turned off) is never kept as the read's result. Signed out, the question ends
and asking it again, signed in, reads again. Refused the read, the question ends
refused, and only a new question follows: the model is never asked to go on without it.

A read the journal kept carries a replay contract beside its result, never sent to the
model: its asker's role, whether they held each permission its result depends on, and
the item field visibility config when that shapes the result. It is replayed, when the
question is asked again or resumed after a restart, and again just before each model
step leaves (after any wait for a slot), only while authority read fresh still supports
that contract. Otherwise the question ends refused before anything is sent: the kept
result is not shown to the model again and no step is sent under a new identity. The
tests assert what the AI service received, not only how the question ended.

Which refusals are never kept is one rule (``jobs.caller_refusal``) over the status
codes the app's own authorization raises, read here from the app's auth modules.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import uuid

import httpx

from ai_fake_cloud import answer, cloud, tool_turn, until  # noqa: F401
from celerp.ai import jobs, service, tools
from celerp.services.company_lock import locked_company
from celerp.services.session_tracker import clear as clear_tracker
from test_ai_job_run_context import SEARCH, _restarted_between_the_tool_call_and_its_read
from test_ai_usage import _conversation, _settled, owner  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parent.parent
AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
QUERY = "How do I add a supplier?"
# Where the app decides who may make a request: the session and token checks, the
# permission checks, and the AI session gate.
AUTH_MODULES = ("celerp/services/auth.py", "celerp/services/permissions.py", "celerp/session_gate.py")


def _auth_refusal_codes() -> set[int]:
    codes = set()
    for name in AUTH_MODULES:
        for node in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "status"
                    and node.attr.startswith("HTTP_4")):
                codes.add(int(node.attr.split("_")[1]))
    return codes


def test_the_refusals_never_journaled_are_every_refusal_the_apps_authorization_raises():
    assert _auth_refusal_codes() == set(jobs.CALLER_REFUSALS) == {401, 403}
    for code in _auth_refusal_codes():
        assert jobs.caller_refusal({"ok": False, "status": code, "data": {"detail": "x"}}) is not None
    assert jobs.caller_refusal({"ok": False, "status": 0, "error": {"code": "missing_authorization"}}) is not None
    for request_answer in (200, 400, 404, 409, 422):
        assert jobs.caller_refusal({"ok": request_answer == 200, "status": request_answer, "data": {}}) is None


async def _sign_in(client, session, email="admin@perm.example", password="adminpw12") -> str:
    await clear_tracker(session)
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _ask_again(client, token, conv, qid) -> dict:
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(qid)})
    assert r.status_code == 202, r.text
    return await _settled(client, token, conv, qid)


async def test_signed_out_while_a_read_runs_then_signed_in_the_question_asked_again_reads_and_answers(
        client, owner, cloud, session, monkeypatch):
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.script = lambda identity, body: tool_turn(SEARCH) if identity == str(qid) else answer("Done.")
    monkeypatch.setattr(service, "compile_agent_capabilities", tools.compile_agent_capabilities)
    real = service.execute_agent_capability
    signed_out = asyncio.Event()
    results: list[dict] = []

    async def _read(*args, **kwargs):
        await signed_out.wait()
        results.append(await real(*args, **kwargs))
        return results[-1]
    monkeypatch.setattr(service, "execute_agent_capability", _read)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(qid)})
    assert r.status_code == 202, r.text
    await until(lambda: str(qid) in cloud.arrived)
    assert (await client.post("/auth/logout", headers=AUTH(token))).status_code in (200, 204)
    signed_out.set()
    await until(lambda: results)    # requests share the test's one connection: one at a time
    fresh = await _sign_in(client, session)
    state = await _settled(client, fresh, conv, qid)
    assert results[0]["status"] == 401
    assert (state["state"], state["message"]["error_code"], state["message"]["retryable"]) == (
        "failed", "session_expired", True)

    state = await _ask_again(client, fresh, conv, qid)
    assert results[-1]["status"] == 200 and len(results) == 2
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")
    assert cloud.executions[str(qid)] == 1


async def test_signed_out_across_a_restart_then_signed_in_the_question_asked_again_reads_and_answers(
        client, owner, cloud, session, monkeypatch):
    token, *_ = owner
    conv, qid, results = await _restarted_between_the_tool_call_and_its_read(client, token, cloud, monkeypatch)
    assert (await client.post("/auth/logout", headers=AUTH(token))).status_code in (200, 204)
    await jobs.recover_on_start()
    await until(lambda: len(results) == 2)  # requests share the test's one connection: one at a time
    fresh = await _sign_in(client, session)
    state = await _settled(client, fresh, conv, qid)
    assert results[-1]["status"] == 401, results
    assert (state["state"], state["message"]["error_code"]) == ("failed", "session_expired")

    state = await _ask_again(client, fresh, conv, qid)
    assert results[-1]["status"] == 200, results
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")


def _gated_read(app) -> dict:
    """An agent capability over a real read the app refuses without manage_company_settings."""
    path = next(path for path, _, route in tools._api_routes(app) if route.name == "start_report")
    return {"name": "start_report", "method": "GET", "path": path, "module": "celerp",
            "path_names": (), "query_names": (), "expects_body": False, "inject_idempotency": False,
            "requires_confirmation": False, "required_permissions": ("manage_company_settings",),
            "result_permissions": ("manage_company_settings",), "field_visibility": False,
            "tool": {"type": "function", "function": {"name": "start_report", "description": "last start",
                                                      "parameters": {}}}}


async def _grant(session, company_id, granted: bool) -> None:
    company = await locked_company(session, company_id)
    grants = dict((company.settings or {}).get("role_grants") or {})
    if granted:
        grants.pop("manage_company_settings", None)
    else:
        grants["manage_company_settings"] = []
    company.settings = {**(company.settings or {}), "role_grants": grants}
    await session.commit()


async def _gated(app, monkeypatch) -> list[dict]:
    """The question's one tool is a read gated on a permission; returns every result it read."""
    caps = {"start_report": _gated_read(app)}
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app_, settings, role=None: caps)
    real = service.execute_agent_capability
    results: list[dict] = []

    async def _read(*args, **kwargs):
        results.append(await real(*args, **kwargs))
        return results[-1]
    monkeypatch.setattr(service, "execute_agent_capability", _read)
    return results


async def _asked_new(client, token, conv) -> dict:
    """A new question, under a new id: it reads under the asker's authority now."""
    return await _ask_again(client, token, conv, uuid.uuid4())


async def test_a_read_refused_ends_the_question_refused_and_only_a_new_question_reads_again(
        client, owner, cloud, session, monkeypatch):
    from celerp.main import app
    token, company_id, _ = owner
    results = await _gated(app, monkeypatch)
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    # Every question's first turn asks for the read; the turn after it answers.
    cloud.script = lambda identity, body: (answer("Done.") if body.get("step_id")
                                           else tool_turn(("start_report", {}, "call-1")))
    await _grant(session, company_id, False)
    state = await _ask_again(client, token, conv, qid)
    assert results[0]["status"] == 403
    assert (state["state"], state["message"]["error_code"], state["message"]["retryable"],
            state["message"]["new_question"]) == ("failed", "read_refused", False, True)
    assert not [b for b in cloud.bodies if b.get("step_id")]      # the model never went on without the read

    await _grant(session, company_id, True)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(qid)})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "operation_conflict", r.text
    assert len(results) == 1

    state = await _asked_new(client, token, conv)
    assert results[-1]["status"] == 200 and len(results) == 2
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")


def _relay_down_after_the_read(cloud, qid) -> dict:
    """The question's first turn asks for the read; every later turn finds the AI
    service unreachable until ``down["on"]`` is cleared. Returns the switch."""
    down = {"on": True, "continued": []}

    def _script(identity, body):
        if identity == str(qid):
            return tool_turn(("start_report", {}, "call-1"))
        down["continued"].append(identity)
        if down["on"]:
            cloud.state.pop(identity, None)
            return httpx.ConnectError("relay down")
        return answer("Done.")
    cloud.script = _script
    return down


async def _read_kept_then_failed(client, owner, cloud, session, monkeypatch):
    """A question whose read succeeded and was kept, and which then failed because the
    AI service was unreachable: asking it again replays the kept read."""
    from celerp.main import app
    token, company_id, _ = owner
    results = await _gated(app, monkeypatch)
    await _grant(session, company_id, True)
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    down = _relay_down_after_the_read(cloud, qid)
    first = await _ask_again(client, token, conv, qid)
    assert results[0]["status"] == 200 and first["state"] == "failed", first
    return conv, qid, results, down


async def test_a_read_kept_while_its_asker_may_make_it_is_replayed_not_read_again(
        client, owner, cloud, session, monkeypatch):
    token, *_ = owner
    conv, qid, results, down = await _read_kept_then_failed(client, owner, cloud, session, monkeypatch)
    down["on"] = False
    state = await _ask_again(client, token, conv, qid)
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")
    assert len(results) == 1                                # the kept read, not a second one
    sent = [b for b in cloud.bodies if b.get("step_id")][-1]
    assert [m for m in sent["messages"] if m.get("role") == "tool"][0]["tool_call_id"] == "call-1"
    assert cloud.executions[str(qid)] == 1


async def test_a_read_kept_before_its_permission_was_taken_away_ends_the_question_refused_unsent(
        client, owner, cloud, session, monkeypatch):
    token, company_id, _ = owner
    conv, qid, results, down = await _read_kept_then_failed(client, owner, cloud, session, monkeypatch)
    down["on"] = False
    sends = len(cloud.bodies)
    await _grant(session, company_id, False)
    state = await _ask_again(client, token, conv, qid)
    assert (state["state"], state["message"]["error_code"], state["message"]["new_question"]) == (
        "failed", "read_refused", True)
    assert len(results) == 1 and len(cloud.bodies) == sends   # nothing read, nothing sent

    await _grant(session, company_id, True)
    state = await _asked_new(client, token, conv)
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")


async def test_a_question_resumed_after_a_restart_replays_a_kept_read_only_while_its_asker_may_make_it(
        client, owner, cloud, session, monkeypatch):
    """The restart comes while the turn after the kept read runs at the service. The
    permission is taken away before the process starts again: the resumed question
    ends refused, reads nothing and sends no step, under its own identity or a new one."""
    from celerp.main import app
    token, company_id, _ = owner
    results = await _gated(app, monkeypatch)
    await _grant(session, company_id, True)
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    continued: list[str] = []

    def _script(identity, body):
        if identity == str(qid):
            return tool_turn(("start_report", {}, "call-1"))
        continued.append(identity)
        cloud.held(identity)            # the process stops while this turn runs
        return answer("Done.")
    cloud.script = _script
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(qid)})
    assert r.status_code == 202, r.text
    await until(lambda: continued)
    await jobs.promote(qid)
    await jobs.shutdown()
    sends = len(cloud.bodies)
    await _grant(session, company_id, False)

    await jobs.recover_on_start()
    state = await _settled(client, token, conv, qid)
    assert (state["state"], state["message"]["error_code"], state["message"]["new_question"]) == (
        "failed", "read_refused", True)
    assert len(results) == 1 and len(cloud.bodies) == sends


# ── A kept read's result depends on more than its route's permission ─────────────

COST = 4321.5


async def _settings(session, company_id, change) -> None:
    company = await locked_company(session, company_id)
    company.settings = change(dict(company.settings or {}))
    await session.commit()


def _costs(granted: bool):
    def change(settings):
        grants = dict(settings.get("role_grants") or {})
        if granted:
            grants.pop("view_inventory_costs", None)
        else:
            grants["view_inventory_costs"] = []
        return {**settings, "role_grants": grants}
    return change


def _name_shown_to_admins_only(settings):
    from celerp.services.field_schema import _BASE_FIELDS
    return {**settings, "item_schema": [{**f, "visible_to_roles": ["admin"]} if f["key"] == "name" else f
                                        for f in settings.get("item_schema") or _BASE_FIELDS]}


async def _item_read_kept_then_failed(client, owner_token, asker_token, cloud, monkeypatch, *,
                                      status="available", also=None) -> tuple:
    """A question of ``asker_token`` reads an item (of ``status``) through the real item
    capability, and through ``also`` (another capability) when given, and keeps the
    reads; the turn after them finds the AI service unreachable, so it fails. Returns
    the conversation, the question id, every read result and the relay switch."""
    from celerp.main import app
    from test_helpers import real_agent_app
    r = await client.post("/items", headers=AUTH(owner_token), json={
        "status": status, "sku": f"COST-{uuid.uuid4().hex[:6]}", "sell_by": "piece", "name": "Costly",
        "cost_price": COST, "quantity": 1})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    real_agent_app()
    extra = {also["name"]: also} if also else {}
    monkeypatch.setattr(service, "compile_agent_capabilities",
                        lambda app_, settings, role=None: {**tools.compile_agent_capabilities(app_, settings, role),
                                                           **extra})
    caps = tools.compile_agent_capabilities(app, {}, None)
    name = next(n for n, c in caps.items() if c["method"] == "GET" and c["path"].endswith("/items/{entity_id}"))
    assert caps[name]["required_permissions"] == ("view_inventory",)
    assert {"view_inventory_costs", "edit_inventory"} <= set(caps[name]["result_permissions"])
    assert caps[name]["field_visibility"]
    real = service.execute_agent_capability
    results: list[dict] = []

    async def _read(*args, **kwargs):
        results.append(await real(*args, **kwargs))
        return results[-1]
    monkeypatch.setattr(service, "execute_agent_capability", _read)
    conv = await _conversation(client, asker_token)
    qid = uuid.uuid4()
    down = {"on": True}
    calls = [(name, {"path": {"entity_id": item_id}}, "call-1")] + ([(also["name"], {}, "call-2")] if also else [])

    def _script(identity, body):
        if not body.get("step_id"):                 # a question's first turn
            return tool_turn(*calls)
        if down["on"]:
            cloud.state.pop(identity, None)
            return httpx.ConnectError("relay down")
        return answer("Done.")
    cloud.script = _script
    first = await _ask_again(client, asker_token, conv, qid)
    assert [read["status"] for read in results] == [200] * len(calls) and first["state"] == "failed", first
    return conv, qid, results, down


def _sent_since(cloud, sends: int) -> str:
    """Every model request sent after the first ``sends``, as the AI service received them."""
    return json.dumps(cloud.bodies[sends:])


def _replayed(cloud, call_id: str):
    """What the last model step sent carried as the result of read ``call_id``."""
    sent = [b for b in cloud.bodies if b.get("step_id")][-1]
    return json.loads(next(m for m in sent["messages"] if m.get("tool_call_id") == call_id)["content"])


async def _refused_unsent(client, token, conv, qid, cloud, results, reads: int = 1, *, shown: str) -> None:
    """Asked again, the question ends refused: nothing read again and no model request
    sent, so what the kept read showed (``shown``) never reaches the model again."""
    sends = len(cloud.bodies)
    state = await _ask_again(client, token, conv, qid)
    assert (state["state"], state["message"]["error_code"], state["message"]["new_question"]) == (
        "failed", "read_refused", True)
    assert len(results) == reads and len(cloud.bodies) == sends
    assert shown not in _sent_since(cloud, sends)


async def _user_id(client, owner_token, email: str) -> str:
    users = (await client.get("/companies/me/users", headers=AUTH(owner_token))).json()["items"]
    return next(u["id"] for u in users if u["email"] == email)


async def test_a_kept_read_that_showed_costs_ends_the_question_refused_once_costs_are_taken_away(
        client, owner, cloud, session, monkeypatch):
    token, company_id, _ = owner
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch)
    assert str(COST) in json.dumps(results[0])
    down["on"] = False
    await _settings(session, company_id, _costs(False))
    await _refused_unsent(client, token, conv, qid, cloud, results, shown=str(COST))


async def test_a_kept_read_made_without_costs_is_replayed_while_its_asker_still_may_not_see_them(
        client, owner, cloud, session, monkeypatch):
    """A role that never saw costs keeps its cost-free read: the cost permission it
    never held is not part of what the read relied on."""
    token, company_id, _ = owner
    await _settings(session, company_id, _costs(False))
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch)
    assert str(COST) not in json.dumps(results[0])
    down["on"] = False
    state = await _ask_again(client, token, conv, qid)
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")
    assert len(results) == 1                                  # replayed, not read again
    assert _replayed(cloud, "call-1") == results[0]           # the saved result, unchanged


async def test_a_kept_read_that_showed_a_field_ends_the_question_refused_once_the_field_is_hidden(
        client, owner, cloud, session, monkeypatch):
    from test_helpers import invite_user
    token, company_id, _ = owner
    manager = await invite_user(client, session, AUTH(token), "manager@journal.example", "manager")
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, manager, cloud, monkeypatch)
    assert "Costly" in json.dumps(results[0])
    down["on"] = False
    await _settings(session, company_id, _name_shown_to_admins_only)
    assert "Costly" not in (await client.get(f"/items/{results[0]['data']['id']}", headers=AUTH(manager))).text
    await _refused_unsent(client, manager, conv, qid, cloud, results, shown="Costly")


async def test_a_kept_read_ends_the_question_refused_once_its_asker_is_moved_to_a_lower_role(
        client, owner, cloud, session, monkeypatch):
    """Costs are granted to operators too, so the operator role holds every permission
    the read relied on: the lower role alone ends the question refused."""
    from test_helpers import invite_user
    token, company_id, _ = owner

    def _costs_from_operator(settings):
        grants = {**(settings.get("role_grants") or {}),
                  "view_inventory_costs": ["operator", "manager", "admin", "owner"]}
        return {**settings, "role_grants": grants}
    await _settings(session, company_id, _costs_from_operator)
    manager = await invite_user(client, session, AUTH(token), "downgraded@journal.example", "manager")
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, manager, cloud, monkeypatch)
    assert str(COST) in json.dumps(results[0])
    down["on"] = False
    user_id = await _user_id(client, token, "downgraded@journal.example")
    r = await client.patch(f"/companies/me/users/{user_id}", headers=AUTH(token), json={"role": "operator"})
    assert r.status_code == 200, r.text
    operator = await _sign_in(client, session, "downgraded@journal.example", "userpw123")  # a role change signs out
    assert str(COST) in (await client.get(f"/items/{results[0]['data']['id']}", headers=AUTH(operator))).text
    await _refused_unsent(client, operator, conv, qid, cloud, results, shown=str(COST))


async def test_a_kept_read_of_a_draft_cost_ends_the_question_refused_once_its_asker_may_not_edit_items(
        client, owner, cloud, session, monkeypatch):
    """An operator, who may not see costs, sees a draft item's cost because they may
    edit items: the read relied on edit_inventory, so it is not replayed without it."""
    from test_helpers import invite_user
    token, company_id, _ = owner
    operator = await invite_user(client, session, AUTH(token), "drafter@journal.example", "operator")
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, operator, cloud, monkeypatch,
                                                                 status="draft")
    assert str(COST) in json.dumps(results[0])
    down["on"] = False

    def _no_editing(settings):
        return {**settings, "role_grants": {**(settings.get("role_grants") or {}), "edit_inventory": []}}
    await _settings(session, company_id, _no_editing)
    assert str(COST) not in (await client.get(f"/items/{results[0]['data']['id']}", headers=AUTH(operator))).text
    await _refused_unsent(client, operator, conv, qid, cloud, results, shown=str(COST))


async def test_one_of_several_kept_reads_no_longer_supported_ends_the_question_refused_unsent(
        client, owner, cloud, session, monkeypatch):
    from celerp.main import app
    token, company_id, _ = owner
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch,
                                                                 also=_gated_read(app))
    down["on"] = False
    await _grant(session, company_id, False)        # the second read only; the item read still holds
    await _refused_unsent(client, token, conv, qid, cloud, results, reads=2, shown=str(COST))


async def test_several_kept_reads_all_still_supported_are_replayed_unchanged(
        client, owner, cloud, session, monkeypatch):
    from celerp.main import app
    token, *_ = owner
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch,
                                                                 also=_gated_read(app))
    down["on"] = False
    state = await _ask_again(client, token, conv, qid)
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")
    assert len(results) == 2
    assert [_replayed(cloud, "call-1"), _replayed(cloud, "call-2")] == results


async def test_costs_taken_away_while_the_question_waits_for_a_slot_end_it_refused_unsent(
        client, owner, cloud, session, monkeypatch):
    """The kept read is admitted when the question starts again, then the question
    waits for a model slot; costs are taken away meanwhile. It is checked again with
    fresh authority just before the step leaves, so nothing is sent."""
    token, company_id, _ = owner
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch)
    down["on"] = False
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    sends = len(cloud.bodies)
    async with jobs.gate.slot("interactive"):
        r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                              json={"query": QUERY, "operation_id": str(qid)})
        assert r.status_code == 202, r.text
        await until(lambda: jobs.gate._waiting["interactive"] == 1)
        await _settings(session, company_id, _costs(False))
    state = await _settled(client, token, conv, qid)
    assert (state["state"], state["message"]["error_code"]) == ("failed", "read_refused")
    assert len(results) == 1 and len(cloud.bodies) == sends
    assert str(COST) not in _sent_since(cloud, sends)


async def test_a_kept_read_without_a_replay_contract_ends_the_question_refused(
        client, owner, cloud, session, monkeypatch):
    """A read kept in an earlier format, its result alone, is never replayed."""
    from sqlalchemy import select
    from celerp.models.ai import AIJobStep
    token, *_ = owner
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch)
    down["on"] = False
    step = (await session.execute(select(AIJobStep).where(AIJobStep.key == "tool:call-1"))).scalar_one()
    assert set(step.result) == {"result", "replay"}
    step.result = {"result": step.result["result"]}
    await session.commit()
    await _refused_unsent(client, token, conv, qid, cloud, results, shown=str(COST))


async def test_authority_that_changed_while_a_read_ran_ends_the_question_refused_before_its_next_step(
        client, owner, cloud, session, monkeypatch):
    """Costs are granted while the read runs: what it showed was not decided under one
    authority, so its contract is empty and the step that would carry it is never sent."""
    token, company_id, _ = owner
    await _settings(session, company_id, _costs(False))
    conv, qid, results, down = await _item_read_kept_then_failed(client, token, token, cloud, monkeypatch)
    down["on"] = False
    read = service.execute_agent_capability

    async def _granted_meanwhile(*args, **kwargs):
        result = await read(*args, **kwargs)
        await _settings(session, company_id, _costs(True))
        return result
    monkeypatch.setattr(service, "execute_agent_capability", _granted_meanwhile)
    state = await _asked_new(client, token, conv)
    assert (state["state"], state["message"]["error_code"]) == ("failed", "read_refused")
    assert len(results) == 2 and [b.get("step_id") for b in cloud.bodies[-1:]] == [None]
