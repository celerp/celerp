# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A question's journal keeps only what the request itself decides. A read the app
refused because of who was asking at that moment (signed out, a permission taken away,
a module turned off) is never kept as the read's result: the question ends failed,
and asking it again, once the asker may read again, runs the read under the session
that asks again and answers.

Which refusals those are is one rule (``jobs.caller_refusal``) over the status codes
the app's own authorization raises, read here from the app's auth modules.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid

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
    fresh = await _sign_in(client, session)
    state = await _settled(client, fresh, conv, qid)
    assert results[-1]["status"] == 401
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


async def test_a_permission_taken_away_then_given_back_the_question_asked_again_reads_and_answers(
        client, owner, cloud, session, monkeypatch):
    from celerp.main import app
    token, company_id, _ = owner
    caps = {"start_report": _gated_read(app)}
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app_, settings, role=None: caps)
    real = service.execute_agent_capability
    results: list[dict] = []

    async def _read(*args, **kwargs):
        results.append(await real(*args, **kwargs))
        return results[-1]
    monkeypatch.setattr(service, "execute_agent_capability", _read)
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.script = lambda identity, body: (tool_turn(("start_report", {}, "call-1")) if identity == str(qid)
                                           else answer("Done."))
    await _grant(session, company_id, False)
    state = await _ask_again(client, token, conv, qid)
    assert results[0]["status"] == 403
    assert (state["state"], state["message"]["error_code"], state["message"]["retryable"]) == (
        "failed", "read_refused", True)

    await _grant(session, company_id, True)
    state = await _ask_again(client, token, conv, qid)
    assert results[-1]["status"] == 200 and len(results) == 2
    assert (state["state"], state["message"]["content"]) == ("answered", "Done.")
    assert cloud.executions[str(qid)] == 1
