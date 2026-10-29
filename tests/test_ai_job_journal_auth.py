# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A question's journal keeps only what the request itself decides, and replays it only
under the authority of whoever asks now.

A read the app refused because of who was asking (signed out, a permission taken away,
a module turned off) is never kept as the read's result. Signed out, the question ends
and asking it again, signed in, reads again. Refused the read, the question ends
refused, and only a new question follows: the model is never asked to go on without it.

A read the journal kept is replayed, when the question is asked again or resumed after
a restart, only while its asker may still make it (AI access and every permission the
read needs). Otherwise the question ends refused before anything is sent: the kept
result is not shown to the model again and no step is sent under a new identity.

Which refusals are never kept is one rule (``jobs.caller_refusal``) over the status
codes the app's own authorization raises, read here from the app's auth modules.
"""
from __future__ import annotations

import ast
import asyncio
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


async def _sign_in(client, session) -> str:
    await clear_tracker(session)
    r = await client.post("/auth/login", json={"email": "admin@perm.example", "password": "adminpw12"})
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
