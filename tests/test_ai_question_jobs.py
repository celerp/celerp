# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A thread question runs as a job: the send returns 202 at once and the browser polls
the question's state, so no request waits on the model and a remote-access proxy's
timeout never cuts a long question. The state read never reports a finished question
as interrupted, whatever order the job's end and the read take.

Asking again under the same id is answered by the question alone: memory, language,
tools and history assembled afresh may differ, the last user message does not, so the
service replays its stored reply; a replayed proposal still waits for the user.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import uuid

import pytest

from ai_fake_cloud import FakeCloud, answer, install, tool_turn, until
from celerp.ai import jobs, service
from test_ai_usage import _conversation, owner  # noqa: F401

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731


def _cap(name: str, method: str, *, confirm: bool) -> dict:
    return {"name": name, "method": method, "path": f"/{name}", "path_names": (), "query_names": (),
            "expects_body": method != "GET", "inject_idempotency": False, "requires_confirmation": confirm,
            "tool": {"type": "function", "function": {"name": name, "description": name, "parameters": {}}}}


@pytest.fixture
def cloud(monkeypatch):
    c = install(monkeypatch, FakeCloud())
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    yield c
    for event in list(c.hold.values()):
        event.set()


@pytest.fixture(autouse=True)
async def _no_job_left_running():
    yield
    await jobs.shutdown()


async def _ask(client, token, conv, qid, query="How do I add a supplier?"):
    return await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                             json={"query": query, "operation_id": str(qid)})


async def _state(client, token, conv, qid) -> dict:
    r = await client.get(f"/ai/conversations/{conv}/questions/{qid}", headers=AUTH(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _settled(client, token, conv, qid) -> dict:
    holder: dict = {}

    async def _terminal():
        holder["s"] = await _state(client, token, conv, qid)
        return holder["s"]["state"] != "running"
    await until(_terminal)
    return holder["s"]


def _fingerprint(body: dict) -> str:
    last_user = [m for m in body["messages"] if m["role"] == "user"][-1]
    return hashlib.sha256(json.dumps(last_user, sort_keys=True).encode()).hexdigest()


# ── Red 20: start and poll, nothing waits on the model ────────────────────────

async def test_a_question_longer_than_the_proxy_timeout_is_started_and_polled_within_it(client, owner, cloud):
    """The question takes longer than the proxy allows a request; the send and every
    poll return well inside it, and the thread shows the answer once the job ends."""
    proxy_timeout = 0.5
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.held(str(qid))
    cloud.script = lambda identity, body: answer("Open Contacts and choose New.")

    r = await asyncio.wait_for(_ask(client, token, conv, qid), proxy_timeout)
    assert r.status_code == 202, r.text
    assert r.json()["message_id"] == str(qid)
    for _ in range(3):
        state = await asyncio.wait_for(_state(client, token, conv, qid), proxy_timeout)
        assert state["state"] == "running"
        await asyncio.sleep(proxy_timeout / 2)
    cloud.release(str(qid))
    state = await _settled(client, token, conv, qid)
    assert state["state"] == "answered"
    assert state["message"]["content"] == "Open Contacts and choose New."


async def _held_question(client, token, cloud):
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.held(str(qid))
    assert (await _ask(client, token, conv, qid)).status_code == 202
    await until(lambda: str(qid) in cloud.arrived)
    return conv, qid


async def test_a_job_ending_between_the_registry_read_and_the_stored_read_reads_running(
        client, owner, cloud, monkeypatch):
    """V2, forced: the read finds the job held, then the job commits its answer and
    leaves the registry before the read looks at the stored state."""
    token, *_ = owner
    conv, qid = await _held_question(client, token, cloud)
    real_is_held, real_stored = jobs.is_held, jobs.stored_question_state
    reads: list[bool] = []

    def _is_held(job_id):
        reads.append(real_is_held(job_id))
        return reads[-1]

    async def _stored_after_the_end(*args, **kwargs):
        cloud.release(str(qid))
        await until(lambda: not real_is_held(qid))
        return await real_stored(*args, **kwargs)

    monkeypatch.setattr(jobs, "is_held", _is_held)
    monkeypatch.setattr(jobs, "stored_question_state", _stored_after_the_end)
    state = await _state(client, token, conv, qid)
    assert reads == [True]
    assert state["state"] == "running"


async def test_a_question_out_of_the_registry_always_has_its_answer_stored(client, owner, cloud, monkeypatch):
    """V2, the other side: whenever the registry no longer holds the job, the stored
    state is already terminal (the answer is committed before the job leaves)."""
    token, *_ = owner
    conv, qid = await _held_question(client, token, cloud)
    seen: list[str] = []

    async def _read_until_done():
        while True:
            state = (await _state(client, token, conv, qid))["state"]
            seen.append(state)
            if state != "running":
                return
    reader = asyncio.create_task(_read_until_done())
    await asyncio.sleep(0.05)
    cloud.release(str(qid))
    await asyncio.wait_for(reader, 10)
    assert "interrupted" not in seen and seen[-1] == "answered"


async def test_the_job_owns_its_sessions_and_carries_plain_ids():
    """V3: a job task never holds the request's session or ORM objects."""
    for fn in (jobs.start_job, jobs.recover_on_start):
        for p in inspect.signature(fn).parameters.values():
            assert "Session" not in str(p.annotation) and p.name not in ("session", "db_session")
    src = inspect.getsource(jobs)
    assert "SessionLocal" in src


async def test_a_second_send_while_the_question_runs_is_refused_in_progress(client, owner, cloud):
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.held(str(qid))
    assert (await _ask(client, token, conv, qid)).status_code == 202
    await until(lambda: str(qid) in cloud.arrived)
    again = await _ask(client, token, conv, qid)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "question_in_progress"
    assert cloud.sends[str(qid)] == 1


async def test_the_old_answering_hold_and_whole_run_kill_are_gone():
    from celerp_ai import routes
    assert not hasattr(routes, "_ANSWERING") and not hasattr(routes, "_answering")
    assert not hasattr(routes, "_refuse_if_answering")
    assert not hasattr(service, "AGENT_RUN_TIMEOUT_S") and not hasattr(service, "ask_unattended")


# ── Red 10 (app half): replay by the question ─────────────────────────────────

async def _interrupted(client, token, conv, qid, cloud) -> None:
    """Ask, let the service answer and store it, but lose the reply: the restart ends
    the interactive question interrupted, so the next send is answered by replay."""
    cloud.held(str(qid), end="store")
    assert (await _ask(client, token, conv, qid)).status_code == 202
    await until(lambda: str(qid) in cloud.arrived)
    await jobs.shutdown()
    await jobs.recover_on_start()
    assert (await _state(client, token, conv, qid))["state"] == "interrupted"


async def test_ask_again_after_memory_language_and_tools_changed_sends_the_same_question(
        client, owner, cloud, monkeypatch):
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    await _interrupted(client, token, conv, qid, cloud)

    r = await client.post("/ai/memory/notes", headers=AUTH(token), json={"note": "We buy from Bangkok suppliers."})
    assert r.status_code == 201, r.text
    monkeypatch.setattr(service, "compile_agent_capabilities",
                        lambda app, settings, role=None: {"contacts_search": _cap("contacts_search", "GET",
                                                                                   confirm=False)})
    r = await client.post(f"/ai/conversations/{conv}/query", headers={**AUTH(token), "Accept-Language": "th"},
                          json={"query": "How do I add a supplier?", "operation_id": str(qid)})
    assert r.status_code == 202, r.text
    state = await _settled(client, token, conv, qid)
    assert state["state"] == "answered"
    first, again = [b for b in cloud.bodies if b.get("operation_id") == str(qid)]
    assert first["messages"][0] != again["messages"][0]      # the system message changed
    assert _fingerprint(first) == _fingerprint(again)        # the question did not
    assert cloud.executions[str(qid)] == 1


async def test_a_replayed_proposal_still_waits_for_the_user(client, owner, cloud, monkeypatch):
    """A first-turn reply that proposed a change is replayed with its original tool call
    id; the change is proposed again, never run."""
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    caps = {"contacts_create": _cap("contacts_create", "POST", confirm=True)}
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: caps)
    ran: list = []

    async def _exec(*a, **k):
        ran.append(a)
        return {"ok": True, "status": 201, "data": {}}
    monkeypatch.setattr(service, "execute_agent_capability", _exec)
    cloud.script = lambda identity, body: tool_turn(("contacts_create", {"body": {"name": "Acme"}}, "call-7"))
    await _interrupted(client, token, conv, qid, cloud)

    assert (await _ask(client, token, conv, qid)).status_code == 202
    state = await _settled(client, token, conv, qid)
    assert state["state"] == "answered"
    assert [a["id"] for a in state["message"]["pending_actions"]] == ["call-7"]
    assert ran == []
    assert cloud.executions[str(qid)] == 1


async def test_a_replayed_call_to_a_tool_no_longer_offered_gets_the_tool_error(client, owner, cloud, monkeypatch):
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    caps = {"contacts_search": _cap("contacts_search", "GET", confirm=False)}
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: caps)
    ran: list = []

    async def _exec(*a, **k):
        ran.append(a)
        return {"ok": True, "status": 200, "data": {}}
    monkeypatch.setattr(service, "execute_agent_capability", _exec)
    cloud.script = lambda identity, body: (tool_turn(("contacts_search", {}, "call-9"))
                                           if identity == str(qid) else answer("Done."))
    await _interrupted(client, token, conv, qid, cloud)

    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: {})
    assert (await _ask(client, token, conv, qid)).status_code == 202
    await _settled(client, token, conv, qid)
    assert ran == []
    continuation = [b for b in cloud.bodies if b.get("step_id")][-1]
    (tool_msg,) = [m for m in continuation["messages"] if m.get("role") == "tool"]
    assert tool_msg["tool_call_id"] == "call-9"
    assert json.loads(tool_msg["content"])["error"]["code"] == "unknown_capability"
