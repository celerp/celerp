# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The assistant's writes for a company meet every other company write at the company
lock (``jobs.lock_for_ai``), on PostgreSQL with separate connections, whichever goes
first: a reset, a permission change, a start-up resume and a conversation delete each
either wait for the AI write and then see it, or go first and leave the AI write to see
their outcome. Nothing is left half written, and a question or a file keeps its one
identity at the model.

The lease renewal is the one AI write that never waits on the company lock: a renewal
that waited behind a long holder of it would let a healthy runner's lease lapse, and
its job read as nobody's. The usage reconciler settles each company in a short
transaction of its own, so one company's held lock never holds up another's.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from ai_fake_cloud import answer, cloud  # noqa: F401
from celerp.ai import deadlines, jobs, usage
from celerp.models.ai import AIBatchJob, AIJobStep, AIUsage
from celerp.services.company_lock import locked_company
from celerp.services.company_reset import AI_BATCH_ACTIVE
from company_backup_support import token
from migration_support import count, maker, real_client, real_engine  # noqa: F401
from test_ai_jobs import AUTH, _files, _finished, _op
from test_ai_usage import _claims, _conversation, _messages, _settled
from test_company_reset import RESET, _local_files, _two_companies
from test_helpers import register_admin, until, until_a_lock_waits

pytestmark = [pytest.mark.asyncio, pytest.mark.process]

NAME = "Harbor Goods Ltd"       # company A of ``_two_companies``
QUERY = "How do I add a supplier?"
NONE = dict.fromkeys(("ai_batch_jobs", "ai_job_steps", "ai_conversations", "ai_messages", "ai_usage"), 0)


@pytest.fixture
def pg(real_engine, monkeypatch):
    """Every AI session on its own real connection, as in production."""
    monkeypatch.setattr(jobs, "SessionLocal", maker(real_engine))
    monkeypatch.setattr(deadlines, "SYNC_WAIT_S", 0.05)
    return real_engine


@asynccontextmanager
async def _company_held(engine, company_id):
    """Another transaction holds the company lock until the block ends, as any company
    write does."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT id FROM companies WHERE id = :c FOR NO KEY UPDATE"), {"c": str(company_id)})
        try:
            yield
        finally:
            await conn.rollback()


class _Pause:
    """Wraps ``owner.name`` so its first call runs, then waits (still inside its
    transaction) until ``go``; later calls pass straight through."""

    def __init__(self, monkeypatch, owner, name: str):
        self.entered, self.go, real = asyncio.Event(), asyncio.Event(), getattr(owner, name)

        async def _paused(*args, **kw):
            out = await real(*args, **kw)
            if not self.entered.is_set():
                self.entered.set()
                await self.go.wait()
            return out
        monkeypatch.setattr(owner, name, _paused)


async def _row(engine, job_id) -> AIBatchJob:
    async with maker(engine)() as s:
        return await s.get(AIBatchJob, job_id)


async def _ai_rows(engine, company_id) -> dict[str, int]:
    """The company's AI rows, a step by its job's company and a message by its thread's."""
    c = str(company_id)
    return {
        "ai_batch_jobs": await count(engine, "ai_batch_jobs", "company_id = :c", c=c),
        "ai_job_steps": await count(engine, "ai_job_steps",
                                    "job_id IN (SELECT id FROM ai_batch_jobs WHERE company_id = :c)", c=c),
        "ai_conversations": await count(engine, "ai_conversations", "company_id = :c", c=c),
        "ai_messages": await count(engine, "ai_messages",
                                   "conversation_id IN (SELECT id FROM ai_conversations WHERE company_id = :c)", c=c),
        "ai_usage": await count(engine, "ai_usage", "company_id = :c", c=c),
    }


async def _lapse(engine, job_id) -> None:
    """Time passes past the lease of a runner whose process is gone."""
    async with maker(engine)() as s:
        await s.execute(update(AIBatchJob).where(AIBatchJob.id == job_id).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()


def _identities(cloud) -> set[str]:
    return {b.get("step_id") or b.get("operation_id") for b in cloud.bodies}


def _finish_cut_off(cloud, identity: str) -> None:
    """The model ends the send a stopped process cut off; a later send of the same
    identity gets its stored reply."""
    cloud.replies[identity], cloud.state[identity] = answer("Open Contacts and choose New."), "done"


async def _reading_job(client, token_, company_id, user_id, cloud, *, end: str = "store"):
    """A one-file reading job whose model call is held at the service."""
    conv = await _conversation(client, token_)
    (file_id,) = _files(company_id, user_id, 1)
    job_id = uuid.uuid4()
    identity = _op(job_id, file_id)
    cloud.held(identity, end=end)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token_), json={
        "query": "Read these receipts", "file_ids": [file_id], "document_mode": "receipts",
        "operation_id": str(job_id)})
    assert r.status_code == 202, r.text
    await until(lambda: identity in cloud.arrived)
    return conv, job_id, identity


async def _single(client, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    tok = await register_admin(client)
    return (tok, *_claims(tok))


async def _company_a(engine, monkeypatch, tmp_path):
    _local_files(monkeypatch, tmp_path)
    shared, _solo, a, b = await _two_companies(engine, tmp_path)
    return shared, a, b, await token(engine, shared, a)


# ── The lease ─────────────────────────────────────────────────────────────────

async def test_a_company_lock_held_past_the_lease_never_lapses_a_healthy_runners_job(
        real_client, pg, cloud, monkeypatch, tmp_path):
    """The measurement behind the lease exception: another transaction holds the company
    lock for two lease periods while the runner waits on the model. Every read meanwhile
    finds the job running under its first runner, and it ends with one model run."""
    monkeypatch.setattr(jobs, "LEASE_S", 1.0)
    monkeypatch.setattr(jobs, "LEASE_RENEW_S", 0.2)
    tok, company_id, user_id = await _single(real_client, monkeypatch, tmp_path)
    _conv, job_id, identity = await _reading_job(real_client, tok, company_id, user_id, cloud)
    first = (await _row(pg, job_id)).runner_id
    seen = set()
    loop = asyncio.get_running_loop()
    async with _company_held(pg, company_id):
        end = loop.time() + 2.0
        while loop.time() < end:
            row = await _row(pg, job_id)
            seen.add((jobs.liveness(row), row.runner_id))
            await asyncio.sleep(0.1)
    assert seen == {("running", first)}
    cloud.release(identity)
    assert (await _finished(real_client, tok, job_id))["status"] == "completed"
    assert cloud.executions[identity] == 1


async def test_a_stalled_heartbeat_never_sends_the_question_again_under_a_new_identity(
        real_client, pg, cloud, monkeypatch, tmp_path):
    """A runner that stops renewing while the model answers loses its lease, and the user
    asks again under the question's id. The new runner sends the same identity, which the
    service runs once; the stale runner's answer is refused and the thread holds one."""
    monkeypatch.setattr(jobs, "LEASE_S", 0.5)
    monkeypatch.setattr(jobs, "LEASE_RENEW_S", 3600.0)
    tok, company_id, _user_id = await _single(real_client, monkeypatch, tmp_path)
    conv, qid = await _conversation(real_client, tok), uuid.uuid4()
    cloud.held(str(qid))
    ask = {"query": QUERY, "operation_id": str(qid)}
    r = await real_client.post(f"/ai/conversations/{conv}/query", headers=AUTH(tok), json=ask)
    assert r.status_code == 202, r.text
    await until(lambda: str(qid) in cloud.arrived)

    async def _lapsed():
        return jobs.liveness(await _row(pg, qid)) == "unheld"
    await until(_lapsed)
    stale = (await _row(pg, qid)).runner_id
    again = await real_client.post(f"/ai/conversations/{conv}/query", headers=AUTH(tok), json=ask)
    assert again.status_code == 202, again.text

    async def _claimed_again():
        row = await _row(pg, qid)
        return jobs.liveness(row) == "running" and row.runner_id != stale
    await until(_claimed_again)
    cloud.release(str(qid))

    assert (await _settled(real_client, tok, conv, qid))["state"] == "answered"
    assert _identities(cloud) == {str(qid)}
    assert cloud.executions[str(qid)] == 1
    assert [m["role"] for m in await _messages(real_client, tok, conv)] == ["user", "assistant"]
    assert await count(pg, "ai_usage", "company_id = :c", c=str(company_id)) == 1


# ── A reset ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("first", ["reset", "question"])
async def test_a_reset_and_a_new_question_never_overlap(real_client, pg, cloud, monkeypatch, tmp_path, first):
    """A question asked while the reset holds the company waits and finds the company
    gone, with nothing sent or left. A reset that waits for a question being created
    finds it running and refuses, deleting nothing."""
    shared, a, _b, tok = await _company_a(pg, monkeypatch, tmp_path)
    qid = uuid.uuid4()
    cloud.held(str(qid))
    pause = _Pause(monkeypatch, jobs, "company_runs_now" if first == "reset" else "reused_job")
    reset = lambda: real_client.post(RESET, json={"company_name": NAME}, headers=AUTH(tok))  # noqa: E731
    question = lambda: real_client.post("/ai/query", headers=AUTH(tok),  # noqa: E731
                                        json={"query": QUERY, "operation_id": str(qid)})
    started = asyncio.create_task(reset() if first == "reset" else question())
    await pause.entered.wait()
    waiting = asyncio.create_task(question() if first == "reset" else reset())
    await until_a_lock_waits(pg)
    pause.go.set()
    one, other = await started, await waiting
    r_reset, r_question = (one, other) if first == "reset" else (other, one)

    if first == "reset":
        assert r_reset.status_code == 200, r_reset.text
        assert r_question.status_code == 404, r_question.text
        assert not cloud.sends
        assert await _ai_rows(pg, a) == NONE
    else:
        assert r_question.status_code == 202, r_question.text
        assert (r_reset.status_code, r_reset.json()["detail"]) == (409, AI_BATCH_ACTIVE)
        assert await count(pg, "companies", "id = :c", c=str(a)) == 1
        cloud.release(str(qid))

        async def _ended():
            return jobs.liveness(await _row(pg, qid)) == "ended"
        await until(_ended)
        assert (await _row(pg, qid)).status == "completed"
        assert cloud.executions[str(qid)] == 1


@pytest.mark.parametrize("first", ["reset", "resume"])
async def test_a_reset_and_the_start_up_resume_never_overlap(real_client, pg, cloud, monkeypatch, tmp_path, first):
    """A job whose process died is resumed at start-up under its company lock. A reset
    holding the company first leaves the resume nothing to start; a resume holding it
    first starts the job, and the waiting reset refuses. The resumed job sends the same
    file identity, which the service ran once."""
    shared, a, _b, tok = await _company_a(pg, monkeypatch, tmp_path)
    _conv, job_id, identity = await _reading_job(real_client, tok, a, shared, cloud, end="running")
    await jobs.shutdown()
    await _lapse(pg, job_id)
    sent = cloud.sends[identity]
    pause = _Pause(monkeypatch, jobs, "company_runs_now" if first == "reset" else "_recover_company")
    reset = lambda: real_client.post(RESET, json={"company_name": NAME}, headers=AUTH(tok))  # noqa: E731
    started = asyncio.create_task(reset() if first == "reset" else jobs.recover_on_start())
    await pause.entered.wait()
    waiting = asyncio.create_task(jobs.recover_on_start() if first == "reset" else reset())
    await until_a_lock_waits(pg)
    pause.go.set()
    one, other = await started, await waiting
    r_reset, resumed = (one, other) if first == "reset" else (other, one)

    if first == "reset":
        assert r_reset.status_code == 200, r_reset.text
        assert resumed == 0
        assert await _ai_rows(pg, a) == NONE
        assert cloud.sends[identity] == sent
    else:
        assert resumed == 1
        assert (r_reset.status_code, r_reset.json()["detail"]) == (409, AI_BATCH_ACTIVE)
        _finish_cut_off(cloud, identity)
        assert (await _finished(real_client, tok, job_id))["status"] == "completed"
        assert _identities(cloud) == {identity}
        assert cloud.executions[identity] == 1


# ── A permission change ───────────────────────────────────────────────────────

async def _revoke(session, company_id) -> None:
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "role_grants": {"use_ai_assistant": []}}


async def _step_and_usage(engine, job_id, company_id) -> tuple[list[str], int]:
    async with maker(engine)() as s:
        states = list((await s.execute(select(AIJobStep.state).where(AIJobStep.job_id == job_id))).scalars())
    return states, await count(engine, "ai_usage", "company_id = :c", c=str(company_id))


@pytest.mark.parametrize("first", ["revocation", "send"])
async def test_a_permission_revoked_and_a_file_sent_never_overlap(real_client, pg, cloud, monkeypatch, tmp_path, first):
    """The step's send is recorded (step running, usage pending) in one transaction under
    the company lock, after admitting its asker again. A revocation that commits first
    leaves the file unsent and uncharged; one that commits after finds it sent, and the
    file ends with its answer and its charge. Never one without the other."""
    tok, company_id, user_id = await _single(real_client, monkeypatch, tmp_path)
    conv = await _conversation(real_client, tok)
    (file_id,) = _files(company_id, user_id, 1)
    job_id = uuid.uuid4()
    identity = _op(job_id, file_id)
    gate = asyncio.Event()
    admitted = jobs._admitted
    if first == "revocation":
        async def _late(session, claim):
            await gate.wait()
            await admitted(session, claim)
        monkeypatch.setattr(jobs, "_admitted", _late)
    else:
        cloud.held(identity)
    r = await real_client.post(f"/ai/conversations/{conv}/query", headers=AUTH(tok), json={
        "query": "Read these receipts", "file_ids": [file_id], "document_mode": "receipts",
        "operation_id": str(job_id)})
    assert r.status_code == 202, r.text

    if first == "revocation":
        async with maker(pg)() as s:
            await _revoke(s, company_id)
            await s.flush()
            gate.set()
            await until_a_lock_waits(pg)
            await s.commit()

        async def _ended():
            return jobs.liveness(await _row(pg, job_id)) == "ended"
        await until(_ended)
        assert not cloud.sends
        assert await _step_and_usage(pg, job_id, company_id) == (["pending"], 0)
    else:
        await until(lambda: identity in cloud.arrived)
        assert await _step_and_usage(pg, job_id, company_id) == (["running"], 1)
        async with maker(pg)() as s:
            await _revoke(s, company_id)
            await s.commit()
        cloud.release(identity)

        async def _ended():
            return jobs.liveness(await _row(pg, job_id)) == "ended"
        await until(_ended)
        assert await _step_and_usage(pg, job_id, company_id) == (["done"], 1)
        assert cloud.executions[identity] == 1


# ── A conversation delete ─────────────────────────────────────────────────────

@pytest.mark.parametrize("first", ["delete", "ask_again"])
async def test_a_conversation_delete_and_asking_again_never_overlap(
        real_client, pg, cloud, monkeypatch, tmp_path, first):
    """An interrupted question is asked again while its conversation is deleted. A delete
    holding the company first removes the thread, and the question asked again is not
    found, with nothing sent and the question ended; asking again first starts the question, and the waiting
    delete is refused as busy, keeping the thread."""
    tok, company_id, _user_id = await _single(real_client, monkeypatch, tmp_path)
    conv, qid = await _conversation(real_client, tok), uuid.uuid4()
    cloud.held(str(qid), end="running")
    ask = {"query": QUERY, "operation_id": str(qid)}
    assert (await real_client.post(f"/ai/conversations/{conv}/query", headers=AUTH(tok), json=ask)).status_code == 202
    await until(lambda: str(qid) in cloud.arrived)
    await jobs.shutdown()
    await _lapse(pg, qid)
    sent = cloud.sends[str(qid)]
    pause = _Pause(monkeypatch, jobs, "end_conversation_jobs" if first == "delete" else "reused_job")
    delete = lambda: real_client.delete(f"/ai/conversations/{conv}", headers=AUTH(tok))  # noqa: E731
    again = lambda: real_client.post(f"/ai/conversations/{conv}/query", headers=AUTH(tok), json=ask)  # noqa: E731
    started = asyncio.create_task(delete() if first == "delete" else again())
    await pause.entered.wait()
    waiting = asyncio.create_task(again() if first == "delete" else delete())
    await until_a_lock_waits(pg)
    pause.go.set()
    one, other = await started, await waiting
    r_delete, r_again = (one, other) if first == "delete" else (other, one)

    if first == "delete":
        assert r_delete.status_code in (200, 204), r_delete.text
        assert r_again.status_code == 404, r_again.text
        assert cloud.sends[str(qid)] == sent
        # The question's job outlives its thread, ended; its send stays counted.
        assert await _ai_rows(pg, company_id) == {**NONE, "ai_batch_jobs": 1, "ai_job_steps": 1, "ai_usage": 1}
        assert jobs.liveness(await _row(pg, qid)) == "ended"
    else:
        assert r_again.status_code == 202, r_again.text
        assert r_delete.status_code == 409, r_delete.text
        assert r_delete.json()["detail"]["code"] == "conversation_busy"
        _finish_cut_off(cloud, str(qid))
        assert (await _settled(real_client, tok, conv, qid))["state"] == "answered"
        assert _identities(cloud) == {str(qid)}
        assert cloud.executions[str(qid)] == 1


# ── Usage reconciliation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("held", [0, 1])
async def test_one_companys_held_lock_never_holds_up_anothers_usage_reconcile(
        real_client, pg, cloud, monkeypatch, tmp_path, held):
    """Two companies' charges wait to be settled, and one company's lock is held through
    a whole pass (the first or the second the pass reaches). The other company is
    settled from the pass's one status read; the held one stays pending, and the next
    pass settles it."""
    shared, a, b = (await _company_a(pg, monkeypatch, tmp_path))[:3]
    ops = {a: uuid.uuid4(), b: uuid.uuid4()}
    for company_id, op in ops.items():
        async with maker(pg)() as s:
            await jobs.lock_for_ai(s, company_id)
            await usage.record_usage(s, company_id=company_id, user_id=shared, operation_id=op)
            await s.commit()
        cloud.state[str(op)] = "done"
    locked, free = sorted(ops, key=str)[held], sorted(ops, key=str)[1 - held]
    await asyncio.sleep(0.01)       # past their reconcile time

    async def _pending() -> dict:
        async with maker(pg)() as s:
            return {r.company_id: (r.pending, r.credits) for r in (await s.execute(select(AIUsage))).scalars()}

    async with _company_held(pg, locked):
        async with maker(pg)() as s:
            await usage.reconcile_usage(s)
    assert await _pending() == {free: (False, 1), locked: (True, 0)}
    async with maker(pg)() as s:
        await usage.reconcile_usage(s)
    assert await _pending() == {free: (False, 1), locked: (False, 1)}
    assert [sorted(ask) for ask in cloud.status_asks] == [sorted(map(str, ops.values())), [str(ops[locked])]]


# ── Two companies ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("held", ["a", "b"])
async def test_two_companies_jobs_run_without_waiting_on_each_other(
        real_client, pg, cloud, monkeypatch, tmp_path, held):
    """Questions of two companies run at once. While one company's lock is held, the
    other company's question is answered and recorded; the held one is answered once its
    lock is free. Each ran once at the model."""
    shared, a, b, tok_a = await _company_a(pg, monkeypatch, tmp_path)
    toks = {a: tok_a, b: await token(pg, shared, b)}
    qids = {a: uuid.uuid4(), b: uuid.uuid4()}
    for company_id, qid in qids.items():
        cloud.held(str(qid))
        r = await real_client.post("/ai/query", headers=AUTH(toks[company_id]),
                                   json={"query": QUERY, "operation_id": str(qid)})
        assert r.status_code == 202, r.text
    await until(lambda: all(str(q) in cloud.arrived for q in qids.values()))
    locked, free = (a, b) if held == "a" else (b, a)

    async def _status(company_id) -> str:
        return (await _row(pg, qids[company_id])).status

    async with _company_held(pg, locked):
        cloud.release(str(qids[free]))

        async def _answered():
            return await _status(free) == "completed"
        await until(_answered)
        assert jobs.liveness(await _row(pg, qids[locked])) == "running"
    cloud.release(str(qids[locked]))

    async def _both():
        return {await _status(c) for c in qids} == {"completed"}
    await until(_both)
    assert {q: cloud.executions[str(q)] for q in qids.values()} == {q: 1 for q in qids.values()}
