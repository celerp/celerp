# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A question's run is built from its job's rows and the app's own authorization,
never from what one process held: started live or resumed after a restart, the
agent gets the same context, its tools read the app as the user who asked, and a
resumed question whose user may no longer use the assistant ends failed before it
reads anything.

The AI service is played over HTTP (tests/ai_fake_cloud.py); a restart is
``jobs.shutdown()`` then ``jobs.recover_on_start()``.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import select

from ai_fake_cloud import answer, cloud, tool_turn, until  # noqa: F401
from celerp.ai import jobs, service, tools
from celerp.models.ai import AIBatchJob, AIMessage
from celerp.services.auth import validate_access_token
from celerp.services.company_lock import locked_company
from test_ai_usage import _conversation, _settled, owner  # noqa: F401

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
SEARCH = ("help_search", {"query": {"q": "supplier"}}, "call-1")


async def _ask(client, token, conv, qid):
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": "How do I add a supplier?", "operation_id": str(qid)})
    assert r.status_code == 202, r.text


async def _restarted_between_the_tool_call_and_its_read(client, token, cloud, monkeypatch):
    """A thread question whose first model turn asks for a read; the restart comes
    after that turn ended and before the read's result was kept, so the resumed run
    reads the app again. Returns the conversation, the question id and every result
    the read returned, pre-restart first."""
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.script = lambda identity, body: tool_turn(SEARCH) if identity == str(qid) else answer("Done.")
    results: list[dict] = []
    # The app's real tools: the read goes through the app as the user who asked.
    monkeypatch.setattr(service, "compile_agent_capabilities", tools.compile_agent_capabilities)
    real = service.execute_agent_capability
    held = asyncio.Event()

    async def _read(*args, **kwargs):
        results.append(await real(*args, **kwargs))
        if len(results) == 1:
            await held.wait()       # the process stops here
        return results[-1]
    monkeypatch.setattr(service, "execute_agent_capability", _read)
    await _ask(client, token, conv, qid)
    await until(lambda: results)
    await jobs.promote(qid)         # moved to the background, so it resumes after a restart
    await jobs.shutdown()
    return conv, qid, results


async def test_a_question_resumed_between_a_tool_request_and_its_next_turn_reads_the_same_result(
        client, owner, cloud, monkeypatch):
    token, *_ = owner
    conv, qid, results = await _restarted_between_the_tool_call_and_its_read(client, token, cloud, monkeypatch)

    await jobs.recover_on_start()
    state = await _settled(client, token, conv, qid)
    assert state["state"] == "answered", state
    assert len(results) == 2, results               # read again after the restart
    before, after = results
    assert before["ok"] is True, before
    assert after == before
    continuation = [b for b in cloud.bodies if b.get("step_id")][-1]
    (tool_msg,) = [m for m in continuation["messages"] if m.get("role") == "tool"]
    assert tool_msg["tool_call_id"] == "call-1"
    assert json.loads(tool_msg["content"]) == before
    assert cloud.executions[str(qid)] == 1


async def test_a_question_resumed_after_its_user_lost_the_permission_ends_failed_and_reads_nothing(
        client, owner, cloud, session, monkeypatch):
    token, company_id, _ = owner
    conv, qid, results = await _restarted_between_the_tool_call_and_its_read(client, token, cloud, monkeypatch)
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "role_grants": {"use_ai_assistant": []}}
    await session.commit()

    await jobs.recover_on_start()

    # The question routes need the permission too, so the question is read from its rows.
    async def _ended():
        row = await session.get(AIBatchJob, qid, populate_existing=True)
        return row.status in jobs.TERMINAL_JOBS
    await until(_ended)
    row = await session.get(AIBatchJob, qid)
    assert row.status == "failed"
    reply = await session.get(AIMessage, uuid.UUID(row.results["message_id"]))
    assert reply.content == "This job stopped because you no longer have permission to use the AI assistant."
    assert [c["code"] for c in reply.tools_called if c.get("error")] == ["job_permission_lost"]
    assert len(results) == 1                        # nothing read after the restart
    assert not [b for b in cloud.bodies if b.get("step_id")]


async def test_a_resumed_question_gets_every_field_of_the_context_its_live_start_got(
        client, owner, cloud, session, monkeypatch):
    """Whatever the live start passes the agent, the resume passes too: compared field
    by field over the whole call, so a field added later cannot be left out of resume."""
    token, *_ = owner
    calls: list[dict] = []
    held = asyncio.Event()

    async def _agent(**kwargs):
        calls.append(kwargs)
        await held.wait()
    monkeypatch.setattr(service, "run_agent", _agent)
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    await _ask(client, token, conv, qid)
    await until(lambda: calls)
    await jobs.promote(qid)
    await jobs.shutdown()
    await jobs.recover_on_start()
    await until(lambda: len(calls) == 2)

    live, resumed = calls
    assert set(resumed) == set(live)
    for name in live:
        if name == "authorization":
            continue
        if name == "journal":
            assert type(resumed[name]) is type(live[name])
            assert resumed[name].question_id == live[name].question_id
            continue
        assert resumed[name] == live[name], name
    assert live["authorization"] and resumed["authorization"]
    contexts = [await validate_access_token(session, call["authorization"].removeprefix("Bearer "))
                for call in calls]
    assert {(c.user.id, c.company_id, c.role, c.snonce) for c in contexts} == {
        (contexts[0].user.id, contexts[0].company_id, contexts[0].role, contexts[0].snonce)}
    held.set()
    await jobs.shutdown()


async def test_a_question_resumed_after_its_user_signed_out_reads_nothing(client, owner, cloud, session, monkeypatch):
    """Signing out ends what the user's questions may read, as it ends the user's own
    requests: the resumed read is refused and the question ends failed, never answered."""
    token, *_ = owner
    conv, qid, results = await _restarted_between_the_tool_call_and_its_read(client, token, cloud, monkeypatch)
    r = await client.post("/auth/logout", headers=AUTH(token))
    assert r.status_code in (200, 204), r.text

    await jobs.recover_on_start()

    async def _ended():
        row = await session.get(AIBatchJob, qid, populate_existing=True)
        return row.status in jobs.TERMINAL_JOBS
    await until(_ended)
    row = await session.get(AIBatchJob, qid)
    assert row.status == "failed"
    assert results[1:] and results[1]["status"] == 401
    assert not [b for b in cloud.bodies if b.get("step_id")]
    messages = (await session.execute(select(AIMessage).where(AIMessage.id == uuid.UUID(row.results["message_id"])))
                ).scalars().all()
    assert messages and messages[0].content
