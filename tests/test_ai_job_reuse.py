# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A send names its question by an id, and ``jobs.reused_job`` is the one comparison
that decides what the id names: no job (a new question), the asker's job for this
same request (asked again), or not this send's. Another user's id is not found, as
everywhere a job is named by its id; the asker's own id for another request is
``operation_conflict``. Two first sends under one new id racing each other both get
the one question, never a server error.
"""
from __future__ import annotations

import asyncio
import uuid

from ai_fake_cloud import answer, cloud, until  # noqa: F401
from celerp.ai import jobs
from celerp.models.ai import AIMessage
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_ai_job_owner_scope import _one_off
from test_ai_usage import _claims, _conversation, owner  # noqa: F401
from test_helpers import invite_user, register_admin

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
QUERY = "What are this month's figures?"


async def _colleague(client, session, token) -> str:
    return await invite_user(client, session, AUTH(token), "colleague@perm.example", "manager")


async def test_a_one_off_send_under_a_colleagues_question_id_is_not_found(client, owner, cloud, session):
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "completed")
    other = await _colleague(client, session, token)
    r = await client.post("/ai/query", headers=AUTH(other), json={"query": QUERY, "operation_id": str(job_id)})
    assert r.status_code == 404, r.text
    assert not cloud.sends


async def test_a_thread_send_under_a_colleagues_question_id_is_not_found(client, owner, cloud, session):
    token, company_id, user_id = owner
    job_id = await _one_off(session, company_id, user_id, "completed")
    other = await _colleague(client, session, token)
    conv = await _conversation(client, other)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(other),
                          json={"query": QUERY, "operation_id": str(job_id)})
    assert r.status_code == 404, r.text
    assert (await client.get(f"/ai/conversations/{conv}", headers=AUTH(other))).json()["messages"] == []
    assert not cloud.sends


async def test_a_message_id_that_names_no_question_job_is_not_found(client, owner, cloud, session):
    """A thread message with no job under its id was never a question this app asked:
    a send under that id is answered not found, never treated as the question asked
    again."""
    token, *_ = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    session.add(AIMessage(id=qid, conversation_id=uuid.UUID(conv), role="user", content=QUERY))
    await session.commit()
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(qid)})
    assert r.status_code == 404, r.text
    assert not cloud.sends


async def test_two_first_sends_under_one_new_id_never_answer_a_server_error(
        real_client, real_engine, cloud, monkeypatch):
    """On Postgres with real connections: both sends race to create the question; the
    one that loses reads the winner's job and gets the same answer."""
    monkeypatch.setattr(jobs, "SessionLocal", maker(real_engine))
    token = await register_admin(real_client)
    job_id = uuid.uuid4()
    cloud.script = lambda identity, body: answer("This month's figures.")
    cloud.held(str(job_id))
    sends = [asyncio.create_task(real_client.post("/ai/query", headers=AUTH(token),
                                                  json={"query": QUERY, "operation_id": str(job_id)}))
             for _ in range(2)]
    await until(lambda: str(job_id) in cloud.arrived)
    await asyncio.sleep(0.2)
    cloud.release(str(job_id))
    replies = await asyncio.gather(*sends)
    assert [r.status_code for r in replies] == [200, 200], [r.text for r in replies]
    assert {r.json()["answer"] for r in replies} == {"This month's figures."}
    assert cloud.executions[str(job_id)] == 1


async def test_a_send_that_reads_the_question_before_its_creator_starts_it_waits_for_its_answer(
        real_client, real_engine, cloud, monkeypatch):
    """The send that creates the question has committed it, held by this process, but not yet
    started its task when the other send reads it: the other send waits for the question's
    answer, never answers that it is running. The creator is held between its commit and its
    start until the other send has begun to wait."""
    real_maker = maker(real_engine)
    created, other_waits = asyncio.Event(), asyncio.Event()
    first = []

    class _CreatorPauses:
        """The first job session: once it has committed and is closing, it waits for the
        other send to begin waiting."""
        def __init__(self):
            self.session = real_maker()
            self.first = not first
            first.append(self)

        async def __aenter__(self):
            return await self.session.__aenter__()

        async def __aexit__(self, *exc):
            await self.session.__aexit__(*exc)
            if self.first:
                created.set()
                await other_waits.wait()

    real_wait = jobs.wait_for_job

    async def _wait_for_job(job_id, *, started):
        if created.is_set() and not other_waits.is_set():
            other_waits.set()
        return await real_wait(job_id, started=started)

    token = await register_admin(real_client)
    monkeypatch.setattr(jobs, "SessionLocal", _CreatorPauses)
    monkeypatch.setattr(jobs, "wait_for_job", _wait_for_job)
    job_id = uuid.uuid4()
    cloud.script = lambda identity, body: answer("This month's figures.")
    send = lambda: real_client.post("/ai/query", headers=AUTH(token), json={"query": QUERY, "operation_id": str(job_id)})  # noqa: E731
    creator = asyncio.create_task(send())
    await created.wait()
    other = await send()
    assert other.status_code == 200, other.text
    assert other.json().get("answer") == "This month's figures.", other.json()
    mine = await creator
    assert mine.json().get("answer") == "This month's figures.", mine.json()
    assert cloud.executions[str(job_id)] == 1
