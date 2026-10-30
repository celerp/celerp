# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Whether a job runs now is read from its runner lease, by one rule (``jobs.liveness``):
the process running a job holds a lease on its row, renews it while it works, and lets
it go when the job ends. A job is running while its lease is valid, open and run by
nobody once it lapsed, and ended once its status is final. Every reader asks that rule:
the API, the thread's bubbles, Stop, Continue, asking again, company reset, the
conversations kept for open work, and the start-up resume.

The matrix runs every job shape (a question outside a thread, a thread question, a
reading job) with its runner alive, dead (its process gone, its lease lapsed), failing
on every attempt, and after a restart, and checks what the user is told, what the
thread offers, and that the company can be reset exactly when nothing runs.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fasthtml.common import to_xml
from sqlalchemy import select, update

from ai_fake_cloud import cloud, until  # noqa: F401
from celerp.ai import deadlines, jobs
from celerp.models.ai import AIBatchJob
from celerp.services.company_reset import AI_BATCH_ACTIVE
from test_ai_jobs import AUTH, _files, _job, _op
from test_ai_usage import _conversation, owner  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUERY = "How do I add a supplier?"
SHAPES = ("one_off", "conversation", "documents")
RUNNERS = ("alive", "dead", "failing", "restarted")

# (what the user is told, what the thread offers, whether the company may be reset)
EXPECTED = {
    ("documents", "alive"): ("running", "stop", False),
    ("documents", "dead"): ("interrupted", "continue", True),
    ("documents", "failing"): ("running", "stop", False),
    ("documents", "restarted"): ("running", "stop", False),
    # A thread question waits with its dots; once moved to the background it offers Stop.
    ("conversation", "alive"): ("running", "wait", False),
    ("conversation", "dead"): ("interrupted", "ask_again", True),
    ("conversation", "failing"): ("running", "stop", False),
    ("conversation", "restarted"): ("interrupted", "ask_again", True),
    # Outside a thread a caller polls the module API; past its wait the question runs in the background.
    ("one_off", "alive"): ("running", "wait", False),
    ("one_off", "dead"): ("interrupted", "ask_again", True),
    ("one_off", "failing"): ("running", "wait", False),
    ("one_off", "restarted"): ("running", "wait", False),
}


class Case:
    def __init__(self, shape: str, job_id: uuid.UUID, conv: str | None, identity: str):
        self.shape, self.job_id, self.conv, self.identity = shape, job_id, conv, identity


async def _start(client, token, company_id, user_id, cloud, monkeypatch, shape: str, *, failing: bool) -> Case:
    """A job of ``shape`` whose first model call is held at the service (or whose
    runner raises on every attempt, with ``failing``)."""
    monkeypatch.setattr(deadlines, "SYNC_WAIT_S", 0.05)
    job_id = uuid.uuid4()
    conv = None if shape == "one_off" else await _conversation(client, token)
    if shape == "documents":
        (file_id,) = _files(company_id, user_id, 1)
        identity = _op(job_id, file_id)
        body = {"query": "Read these receipts", "file_ids": [file_id], "document_mode": "receipts",
                "operation_id": str(job_id)}
    else:
        identity = str(job_id)
        body = {"query": QUERY, "operation_id": str(job_id)}
    # A send cut off by the kill stays in the model, so a restart's send waits on it.
    cloud.held(identity, end="running")
    if failing:
        attempts: list[uuid.UUID] = []

        async def _broken(job_id_, runner_id):
            attempts.append(job_id_)
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.001)
        monkeypatch.setattr(jobs, "RUNNER_RESTART_MAX_DELAY_S", 0.01)
        monkeypatch.setattr(jobs, "_run_job" if shape == "documents" else "_run_question", _broken)
    path = "/ai/query" if shape == "one_off" else f"/ai/conversations/{conv}/query"
    r = await client.post(path, headers=AUTH(token), json=body)
    assert r.status_code == 202, r.text
    if failing:
        # Far past the restarts a runner used to be allowed.
        await until(lambda: len(attempts) >= 12)
    else:
        await until(lambda: identity in cloud.arrived)
    return Case(shape, job_id, conv, identity)


async def _lapse(session, job_id: uuid.UUID) -> None:
    """Time passes past the lease of a process that is gone."""
    await session.execute(update(AIBatchJob).where(AIBatchJob.id == job_id).values(
        lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await session.commit()


async def _seen(client, token, company_id, user_id, case: Case) -> tuple[str, str]:
    """What the user is told the job is doing, and what the thread offers for it."""
    from celerp_ai.ui_routes import _job_bubble, _question_view
    if case.shape == "one_off":
        out = await jobs.question_result(case.job_id, company_id, user_id)
        return out["status"], {"running": "wait", "interrupted": "ask_again"}.get(out["status"], out["status"])
    if case.shape == "documents":
        job = await _job(client, token, case.job_id)
        status, html = job["status"], to_xml(_job_bubble(job, case.conv))
    else:
        r = await client.get(f"/ai/conversations/{case.conv}/questions/{case.job_id}", headers=AUTH(token))
        assert r.status_code == 200, r.text
        state = r.json()
        status = state["state"]
        html = "".join(to_xml(part) for part in _flat(await _question_view(token, state, case.conv,
                                                                           str(case.job_id))))
    return status, _offered(html)


def _flat(view) -> tuple:
    return view if isinstance(view, tuple) else (view,)


def _offered(html: str) -> str:
    if "/cancel" in html:
        return "stop"
    if "/resume" in html:
        return "continue"
    if "ai-msg__again" in html:
        return "ask_again"
    if "ai-msg--pending" in html:
        return "wait"
    return "none"


async def _reset(client, session, token):
    # Job sessions write inside the test's one transaction, under whatever savepoint the
    # shared session holds; a refused reset rolls that savepoint back. Released first,
    # the jobs' rows outlive the refusal as they do on separate connections.
    await session.commit()
    return await client.post("/companies/me/reset", headers=AUTH(token), json={"company_name": "Perm Co"})


def _let_go(cloud, case: Case) -> None:
    """The held model call ends with an answer."""
    cloud.replies[case.identity] = cloud.script(case.identity, {})
    cloud.state[case.identity] = "done"
    if case.identity in cloud.hold:
        cloud.release(case.identity)


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("shape", SHAPES)
async def test_every_job_shape_reads_the_same_with_its_runner_alive_dead_failing_or_restarted(
        client, owner, cloud, session, monkeypatch, shape, runner):
    token, company_id, user_id = owner
    case = await _start(client, token, company_id, user_id, cloud, monkeypatch, shape, failing=runner == "failing")
    if runner == "dead":
        await jobs.shutdown()
        await _lapse(session, case.job_id)
    elif runner == "restarted":
        await jobs.shutdown()
        # A resumed job's send polls the one the kill cut off, and records each poll on
        # the test's one connection, where a refused reset's savepoint would take it
        # back (on separate connections it never could). The polls are spaced out and
        # the job is read and reset just after one.
        monkeypatch.setattr(deadlines, "RESEND_DELAY_S", 5.0)
        sent = cloud.sends[case.identity]
        await jobs.recover_on_start()
        if jobs.task_alive(case.job_id):
            await until(lambda: cloud.sends[case.identity] > sent)

    status, offered = await _seen(client, token, company_id, user_id, case)
    expected_status, expected_offer, reset_allowed = EXPECTED[shape, runner]
    assert (status, offered) == (expected_status, expected_offer)
    if not reset_allowed:
        r = await _reset(client, session, token)
        assert r.status_code == 409 and AI_BATCH_ACTIVE in r.text, r.text
        # Stop ends it whatever its runner is doing; then nothing runs.
        r = await client.post(f"/ai/jobs/{case.job_id}/cancel", headers=AUTH(token))
        assert r.status_code == 202, r.text
        _let_go(cloud, case)
        await until(lambda: not jobs.running_tasks(), 20.0)
    r = await _reset(client, session, token)
    assert AI_BATCH_ACTIVE not in r.text and r.status_code == 200, r.text


async def _stopped_unheld(client, token, session, cloud, case: Case) -> None:
    await jobs.shutdown()
    await _lapse(session, case.job_id)
    r = await client.post(f"/ai/jobs/{case.job_id}/cancel", headers=AUTH(token))
    assert r.status_code == 202, r.text
    _let_go(cloud, case)


@pytest.mark.parametrize("shape", SHAPES)
async def test_stop_on_a_job_nobody_runs_ends_it_stopped_and_it_can_be_continued(
        client, owner, cloud, session, monkeypatch, shape):
    """The process running the job is gone. Stop ends it at once as stopped, which the
    user continues as for any stopped job: Continue for a reading job, Ask again for a
    question. The continued job finishes and nothing is sent twice."""
    token, company_id, user_id = owner
    case = await _start(client, token, company_id, user_id, cloud, monkeypatch, shape, failing=False)
    await _stopped_unheld(client, token, session, cloud, case)

    if shape == "documents":
        assert await _seen(client, token, company_id, user_id, case) == ("cancelled", "continue")
        r = await client.post(f"/ai/jobs/{case.job_id}/resume", headers=AUTH(token))
        assert r.status_code == 202, r.text
    elif shape == "conversation":
        status, offered = await _seen(client, token, company_id, user_id, case)
        assert (status, offered) == ("failed", "ask_again")
        r = await client.post(f"/ai/conversations/{case.conv}/query", headers=AUTH(token),
                              json={"query": QUERY, "operation_id": str(case.job_id)})
        assert r.status_code == 202, r.text
    else:
        out = await jobs.question_result(case.job_id, company_id, user_id)
        assert (out["status"], out["error_code"]) == ("failed", "stopped")
        r = await client.post("/ai/query", headers=AUTH(token), json={"query": QUERY, "operation_id": str(case.job_id)})
        assert r.status_code in (200, 202), r.text
    await until(lambda: not jobs.running_tasks(), 20.0)
    row = (await session.execute(select(AIBatchJob.status).where(AIBatchJob.id == case.job_id))).scalar_one()
    assert row == "completed"
    assert cloud.executions[case.identity] == 1


async def test_a_runner_failing_for_longer_than_its_lease_still_reads_as_running_and_then_finishes(
        client, owner, cloud, session, monkeypatch):
    """The database is unavailable to the runner for many lease periods: its process
    lives, so the lease is renewed through the back-off and the job reads as running,
    never as run by nobody. When the outage ends the job finishes without a restart."""
    monkeypatch.setattr(jobs, "LEASE_S", 0.2)
    monkeypatch.setattr(jobs, "LEASE_RENEW_S", 0.05)
    monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(jobs, "RUNNER_RESTART_MAX_DELAY_S", 0.05)
    token, company_id, user_id = owner
    real = jobs._run_job
    outage = {"on": True}
    attempts: list[uuid.UUID] = []

    async def _run_job(job_id, runner_id):
        attempts.append(job_id)
        if outage["on"]:
            raise RuntimeError("database unavailable")
        return await real(job_id, runner_id)
    monkeypatch.setattr(jobs, "_run_job", _run_job)
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 2)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read these receipts", "file_ids": file_ids, "document_mode": "receipts",
        "operation_id": str(uuid.uuid4())})
    job_id = uuid.UUID(r.json()["job_id"])
    seen = set()
    for _ in range(20):                                         # 1 s: five lease periods
        await asyncio.sleep(0.05)
        seen.add((await _job(client, token, job_id))["status"])
    assert seen == {"running"} and len(attempts) > 5
    outage["on"] = False
    await until(lambda: not jobs.running_tasks(), 20.0)
    assert (await _job(client, token, job_id))["status"] == "completed"


async def test_the_liveness_rule_and_its_sql_form_agree_on_every_state(owner, session):
    """``jobs.liveness`` reads one row; ``jobs.runs_now()`` and ``jobs.is_open()`` are the
    same rule for a query. Every status with no lease, a lapsed lease and a valid lease
    is classified alike by both."""
    _, company_id, user_id = owner
    now = datetime.now(timezone.utc)
    rows = {}
    for status in ("pending", "running", "completed", "failed", "cancelled"):
        for lease in (None, now - timedelta(seconds=5), now + timedelta(seconds=30)):
            job = jobs.question_job(job_id=uuid.uuid4(), request=jobs.JobRequest.question(
                company_id=company_id, user_id=user_id, query=f"{status} {lease}"))
            job.status, job.lease_until = status, lease
            job.runner_id = None if lease is None else "elsewhere"
            session.add(job)
            rows[job.id] = job
    await session.commit()
    ids = list(rows)
    running = set((await session.execute(select(AIBatchJob.id).where(AIBatchJob.id.in_(ids), jobs.runs_now()))).scalars())
    open_ = set((await session.execute(select(AIBatchJob.id).where(AIBatchJob.id.in_(ids), jobs.is_open()))).scalars())
    for job_id, job in rows.items():
        state = jobs.liveness(job)
        assert (job_id in running) == (state == "running"), (job.status, job.lease_until)
        assert (job_id in open_) == (state != "ended"), (job.status, job.lease_until)
    assert {jobs.liveness(job) for job in rows.values()} == {"running", "unheld", "ended"}


async def _row(session, job_id: uuid.UUID) -> AIBatchJob:
    return (await session.execute(select(AIBatchJob).where(AIBatchJob.id == job_id)
                                  .execution_options(populate_existing=True))).scalar_one()


async def _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, shape: str, state: str) -> Case:
    """A job of ``shape`` its runner holds (running), whose process is gone (unheld),
    or that finished (ended)."""
    case = await _start(client, token, company_id, user_id, cloud, monkeypatch, shape, failing=False)
    if state == "unheld":
        await jobs.shutdown()
        await _lapse(session, case.job_id)
    elif state == "ended":
        _let_go(cloud, case)
        await until(lambda: not jobs.running_tasks(), 20.0)
    assert jobs.liveness(await _row(session, case.job_id)) == state
    await session.commit()
    return case


@pytest.mark.parametrize("state", ("running", "unheld", "ended"))
@pytest.mark.parametrize("shape", ("conversation", "documents"))
async def test_a_conversation_is_kept_and_undeletable_only_while_a_job_runs_in_it_and_no_job_outlives_it_open(
        client, owner, cloud, session, monkeypatch, shape, state):
    """The history keeps a conversation while a job of it is open. Deleting it is
    refused while a runner works in it; otherwise the delete ends every open job of it
    as stopped, so nothing of it resumes at the next start or reaches the model again."""
    from celerp.ai.conversations import _protected_conversation_ids
    token, company_id, user_id = owner
    case = await _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, shape, state)
    conv = uuid.UUID(case.conv)
    assert await _protected_conversation_ids(session, [conv]) == (set() if state == "ended" else {conv})
    await session.commit()
    r = await client.delete(f"/ai/conversations/{conv}", headers=AUTH(token))
    if state == "running":
        assert r.status_code == 409 and r.json()["detail"]["code"] == "conversation_busy", r.text
        _let_go(cloud, case)
        await until(lambda: not jobs.running_tasks(), 20.0)
        return
    assert r.status_code == 204, r.text
    assert jobs.liveness(await _row(session, case.job_id)) == "ended"
    sent = cloud.executions.get(case.identity, 0)
    assert await jobs.recover_on_start() == 0
    _let_go(cloud, case)
    await until(lambda: not jobs.running_tasks(), 20.0)
    assert cloud.executions.get(case.identity, 0) == sent


@pytest.mark.parametrize("shape", ("conversation", "documents"))
async def test_a_reopen_racing_a_conversation_delete_never_reopens_its_orphaned_job(
        client, owner, cloud, session, monkeypatch, shape):
    """A send (or Continue) reads the job, then the conversation is deleted, then its
    reopen runs: the one reopenable rule refuses a job whose conversation is gone, in
    the reopen's own conditional update, so nothing of it runs or reaches the model."""
    token, company_id, user_id = owner
    case = await _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, shape, "unheld")
    r = await client.delete(f"/ai/conversations/{case.conv}", headers=AUTH(token))
    assert r.status_code == 204, r.text
    row = await _row(session, case.job_id)
    assert row.conversation_id is None and jobs.liveness(row) == "ended"
    sent = len(cloud.bodies)
    if shape == "documents":
        # Its refusal rolls back the test's one shared transaction, rows and all (on
        # separate connections it rolls back only its own), so the row is read before.
        await session.commit()
        assert await jobs.resume(case.job_id, company_id, user_id) is None
    else:
        async with jobs.SessionLocal() as s:
            await jobs.lock_for_ai(s, row.company_id)
            assert not await jobs.reopen_question(s, case.job_id, row.auth_nonce)
            await s.commit()
        assert jobs.liveness(await _row(session, case.job_id)) == "ended"
    assert not jobs.running_tasks() and len(cloud.bodies) == sent


async def test_asking_again_while_the_conversation_is_deleted_answers_not_found(
        client, owner, cloud, session, monkeypatch):
    """The send read the interrupted question, then its conversation was deleted before
    the reopen: the send answers 404, never in progress, and nothing reaches the model."""
    from celerp.ai import conversations
    token, company_id, user_id = owner
    case = await _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, "conversation", "unheld")
    reopen = jobs.reopen_question

    async def deleted_first(s, job_id, auth_nonce):
        assert await conversations.delete_conversation(s, uuid.UUID(case.conv), company_id, user_id)
        return await reopen(s, job_id, auth_nonce)

    monkeypatch.setattr(jobs, "reopen_question", deleted_first)
    sent = len(cloud.bodies)
    r = await client.post(f"/ai/conversations/{case.conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(case.job_id)})
    assert r.status_code == 404, r.text
    assert jobs.liveness(await _row(session, case.job_id)) == "ended"
    assert not jobs.running_tasks() and len(cloud.bodies) == sent


@pytest.mark.parametrize("state", ("running", "unheld"))
async def test_an_open_questions_message_is_never_pruned(client, owner, cloud, session, monkeypatch, state):
    """A thread keeps its newest messages and prunes the oldest, never the message of
    a question whose job is open."""
    from celerp.ai import conversations
    from celerp.models.ai import AIMessage
    token, company_id, user_id = owner
    case = await _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, "conversation", state)
    monkeypatch.setattr(conversations, "MAX_MESSAGES_PER_CONVERSATION", 2)
    for role, text in (("user", "later question"), ("assistant", "later answer")):
        await conversations.add_message(session, uuid.UUID(case.conv), role, text)
    await session.commit()
    assert await session.get(AIMessage, case.job_id) is not None
    _let_go(cloud, case)
    await until(lambda: not jobs.running_tasks(), 20.0)
    # Its runner answers it; an interrupted one still reads as interrupted, with its message.
    expected = {"running": "answered", "unheld": "interrupted"}[state]
    assert (await _seen(client, token, company_id, user_id, case))[0] == expected


@pytest.mark.parametrize("state", ("running", "unheld"))
async def test_a_question_whose_message_is_gone_ends_and_is_answered_not_found(
        client, owner, cloud, session, monkeypatch, state):
    """The question's own message was removed while its job was open. Its job ends
    (nothing of it resumes), and reading or asking it again answers 404 or 409."""
    from sqlalchemy import delete
    from celerp.models.ai import AIMessage
    token, company_id, user_id = owner
    case = await _in_state(client, token, company_id, user_id, cloud, session, monkeypatch, "conversation", state)
    await session.execute(delete(AIMessage).where(AIMessage.id == case.job_id))
    await session.commit()
    _let_go(cloud, case)
    if state == "unheld":
        await jobs.recover_on_start()
    await until(lambda: not jobs.running_tasks(), 20.0)
    await session.commit()
    r = await client.get(f"/ai/conversations/{case.conv}/questions/{case.job_id}", headers=AUTH(token))
    assert r.status_code == 404, r.text
    r = await client.post(f"/ai/conversations/{case.conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(case.job_id)})
    assert r.status_code in (404, 409), r.text
    assert jobs.liveness(await _row(session, case.job_id)) == "ended"
    assert await jobs.recover_on_start() == 0


async def test_a_message_that_is_no_question_is_answered_not_found(client, owner, cloud, session):
    """An id naming a message no question job belongs to is not a question: reading
    it, or asking under it, answers 404."""
    from celerp.ai import conversations
    token, *_ = owner
    conv = await _conversation(client, token)
    message = await conversations.add_message(session, uuid.UUID(conv), "user", "a note")
    await session.commit()
    r = await client.get(f"/ai/conversations/{conv}/questions/{message.id}", headers=AUTH(token))
    assert r.status_code == 404, r.text
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                          json={"query": QUERY, "operation_id": str(message.id)})
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("shape", SHAPES)
async def test_a_runner_whose_lease_lapsed_never_writes_over_the_stop_that_ended_its_job(
        client, owner, cloud, session, monkeypatch, shape):
    """The runner stalls past its lease while its process lives. Stop finds the job
    run by nobody and ends it as stopped. When the stalled runner wakes, its final
    write is refused: the job stays as Stop left it."""
    token, company_id, user_id = owner
    case = await _start(client, token, company_id, user_id, cloud, monkeypatch, shape, failing=False)
    await _lapse(session, case.job_id)
    r = await client.post(f"/ai/jobs/{case.job_id}/cancel", headers=AUTH(token))
    assert r.status_code == 202, r.text
    stopped = await _row(session, case.job_id)
    assert jobs.liveness(stopped) == "ended"
    stopped = (stopped.status, stopped.error, stopped.results, stopped.completed_at)
    _let_go(cloud, case)
    await until(lambda: not jobs.running_tasks(), 20.0)
    row = await _row(session, case.job_id)
    assert (row.status, row.error, row.results, row.completed_at) == stopped
    if shape != "documents":
        assert (await _seen(client, token, company_id, user_id, case))[0] == "failed"


async def _ended_by_a_competing_claim(session, case: Case) -> str:
    """The job's lease lapses while its runner waits on the service, and a second claim
    of this same process takes it and ends it: a reading job with its file read, a
    question answered. Returns the competitor's runner id."""
    from celerp.ai import service
    from celerp.models.ai import AIJobStep
    await _lapse(session, case.job_id)
    async with jobs.SessionLocal() as s:
        await jobs._lock_job(s, case.job_id)
        competitor = await jobs._claim(s, case.job_id)
        await s.commit()
    assert competitor is not None and competitor[:len(jobs.RUNNER_ID)] == jobs.RUNNER_ID
    row = await _row(session, case.job_id)
    assert row.runner_id == competitor
    if case.shape == "documents":
        run = jobs._file_run(row, competitor)
        (step,) = (await session.execute(select(AIJobStep.id).where(AIJobStep.job_id == case.job_id))).scalars()
        async with jobs.SessionLocal() as s:
            await jobs.lock_for_ai(s, run.company_id)
            await jobs._set_step(s, run, step, state="done", result={"filename": "competitor", "answer": "Theirs."})
            await s.commit()
        await jobs._end_job(run)
    else:
        answered = service.RunTally().result(answer="Theirs.", model_used="m", tools_called=[], pending_actions=[],
                                             error=None, error_code=None)
        await jobs._finish_question(case.job_id, answered, None, competitor)
    return competitor


async def _written(session, case: Case) -> tuple:
    """Everything a runner's writes can change: the job's end, its steps, its messages."""
    from celerp.models.ai import AIJobStep, AIMessage
    session.expire_all()
    row = await _row(session, case.job_id)
    steps = (await session.execute(select(AIJobStep.key, AIJobStep.state, AIJobStep.result, AIJobStep.updated_at)
                                   .where(AIJobStep.job_id == case.job_id).order_by(AIJobStep.seq))).all()
    messages = [] if case.conv is None else (await session.execute(
        select(AIMessage.role, AIMessage.content).where(AIMessage.conversation_id == uuid.UUID(case.conv))
        .order_by(AIMessage.created_at))).all()
    return (row.status, row.error, row.results, row.completed_at, row.runner_id), list(steps), list(messages)


async def _reopened_by_the_user(client, token, session, case: Case) -> None:
    """The job's lease lapses while its runner waits on the service, and its user asks
    it again (a reading job: Continue). The reopen takes the job for this process while
    the stalled runner's task still lives, so that task is told to run it again."""
    await _lapse(session, case.job_id)
    assert jobs.task_alive(case.job_id)
    if case.shape == "documents":
        r = await client.post(f"/ai/jobs/{case.job_id}/resume", headers=AUTH(token))
    else:
        path = "/ai/query" if case.shape == "one_off" else f"/ai/conversations/{case.conv}/query"
        r = await client.post(path, headers=AUTH(token), json={"query": QUERY, "operation_id": str(case.job_id)})
    assert r.status_code in (200, 202), r.text
    assert case.job_id in jobs._again


@pytest.mark.parametrize("competitor", ["ended it", "reopened by the user"])
@pytest.mark.parametrize("shape", SHAPES)
async def test_a_runner_whose_claim_another_took_writes_no_step_and_no_end(
        client, owner, cloud, session, monkeypatch, shape, competitor):
    """Two claims in one process: the first stalls on the service past its lease, the
    second takes the job. When the first wakes with its answer, every write it tries is
    fenced on its own claim and refused: its step result, its final status and its
    message. A second claim that ended the job keeps it as it ended it, with one answer,
    and the stalled runner sends nothing more and is not run again. A second claim the
    user's ask again took is run by the job's task, which still lives: the job ends
    answered once, from the service's one execution, and nothing is left to run."""
    token, company_id, user_id = owner
    case = await _start(client, token, company_id, user_id, cloud, monkeypatch, shape, failing=False)
    if competitor == "reopened by the user":
        await _reopened_by_the_user(client, token, session, case)
        _let_go(cloud, case)
        await until(lambda: not jobs.running_tasks(), 20.0)
        (status, _, _, _, runner_id), steps, messages = await _written(session, case)
        assert (status, runner_id) == ("completed", None)
        assert case.job_id not in jobs._again and not jobs.task_alive(case.job_id)
        if shape == "documents":
            assert [state for _, state, _, _ in steps] == ["done"]
        if shape == "conversation":
            assert len([m for m in messages if m.role == "assistant"]) == 1
        assert cloud.executions[case.identity] == 1
        return
    await _ended_by_a_competing_claim(session, case)
    ended = await _written(session, case)
    assert ended[0][0] == "completed"
    if shape == "conversation":
        assert [m.content for m in ended[2] if m.role == "assistant"] == ["Theirs."]

    _let_go(cloud, case)
    await until(lambda: not jobs.running_tasks(), 20.0)
    assert await _written(session, case) == ended
    assert not [b for b in cloud.bodies if b.get("step_id")]
    assert cloud.executions[case.identity] == 1


@pytest.mark.parametrize("status,held,answers", [
    ("running", True, {"cancel": 202, "resume": 409, "retry-failed": 409}),
    ("completed", False, {"cancel": 409, "resume": 409}),
])
async def test_continue_never_reopens_a_job_a_runner_holds_and_stop_never_acts_on_an_ended_one(
        client, owner, cloud, session, status, held, answers):
    """A job another process runs takes a Stop (its runner ends it at the next step)
    but is never reopened under it; a job that ended takes neither."""
    from celerp.models.ai import AIJobStep
    token, company_id, user_id = owner
    job = AIBatchJob(company_id=company_id, user_id=user_id, status=status, query="read", file_ids=["f1", "f2"])
    if held:
        job.runner_id, job.lease_until = "elsewhere", datetime.now(timezone.utc) + timedelta(minutes=5)
    session.add(job)
    await session.flush()
    session.add_all([AIJobStep(job_id=job.id, seq=0, key="f1", operation_id=uuid.uuid4(),
                               state="pending" if held else "done"),
                     AIJobStep(job_id=job.id, seq=1, key="f2", operation_id=uuid.uuid4(), state="failed")])
    await session.commit()
    for action, code in answers.items():
        await session.commit()      # a refused action rolls back the request's shared session
        r = await client.post(f"/ai/jobs/{job.id}/{action}", headers=AUTH(token))
        assert r.status_code == code, (action, r.text)
        if code == 409:
            assert r.json()["detail"]["code"] == "job_not_open"
    row = (await session.execute(select(AIBatchJob).where(AIBatchJob.id == job.id)
                                 .execution_options(populate_existing=True))).scalar_one()
    assert (row.status, row.runner_id) == (status, job.runner_id)


# ── Guard: the job module owns what a job's state means, who owns it, and how it moves ──

def _scanned() -> list[pathlib.Path]:
    paths = [p for p in (ROOT / "celerp").rglob("*.py") if "migrations" not in p.parts]
    paths += [p for p in (ROOT / "default_modules").glob("*/celerp_*/**/*.py")]
    paths += [p for p in (ROOT / "ui").rglob("*.py") if "tests" not in p.parts]
    return [p for p in paths if p != ROOT / "celerp" / "ai" / "jobs.py" and (ROOT / "celerp" / "models") not in p.parents]


JOB_TABLES = {"AIBatchJob", "AIJobStep"}
# The columns a job's lifecycle moves: what it is doing, who runs it, and when it stopped or ended.
JOB_STATE = {"status", "state", "runner_id", "lease_until", "cancel_requested_at", "completed_at", "promoted_at"}
# Every status a job and every state a step takes, open or ended.
STATUSES = {"pending", "running", "done", *jobs.TERMINAL_JOBS, *jobs.RETRYABLE_STEPS}


def _call_name(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)


def _root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Call)):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    return node.id if isinstance(node, ast.Name) else None


def _own_nodes(statement: ast.stmt):
    """The expressions of ``statement``, without those of the statements nested in it."""
    stack = list(ast.iter_child_nodes(statement))
    while stack:
        node = stack.pop()
        if not isinstance(node, ast.stmt):
            yield node
            stack.extend(ast.iter_child_nodes(node))


def _bypasses(source: str) -> list[str]:
    """What ``source`` does with the job tables that is the job module's to do:
    a job-state predicate of its own, a comparison of a job's status or a step's state
    with any status (``jobs.liveness``, ``is_open``, ``runs_now`` and the outcome
    readers say whether a job is open, runs, or how it ended), a job read without its owner (``jobs.owned_job``
    and the listing helpers scope every read to the company and the user), or a write
    that moves a job or a step (every transition is the runner's or a ``jobs`` helper's).
    Reporting reads that reuse those helpers stay where they are."""
    tree = ast.parse(source)
    about_jobs = bool(JOB_TABLES & {_root(n) for n in ast.walk(tree)}) or "jobs" in {
        alias.asname or alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names}
    found = []
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt):
            continue
        nodes = list(_own_nodes(statement))
        owner = {n.attr for n in nodes
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "AIBatchJob"}
        for n in nodes:
            if (isinstance(n, ast.Attribute) and n.attr in JOB_STATE and isinstance(n.value, ast.Name)
                    and n.value.id in JOB_TABLES):
                found.append(f"{n.lineno} predicate {n.value.id}.{n.attr}")
            if (about_jobs and isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute)
                    and n.left.attr in ("status", "state") and any(
                        isinstance(v, ast.Constant) and v.value in STATUSES
                        for c in n.comparators for v in (c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set))
                                                         else [c]))):
                found.append(f"{n.lineno} predicate .{n.left.attr} against a status")
            if not isinstance(n, ast.Call):
                continue
            name, args = _call_name(n), n.args
            if isinstance(n.func, ast.Name) and n.func.id in JOB_TABLES:
                found.append(f"{n.lineno} lifecycle new {n.func.id}")
            elif name in ("update", "delete", "insert") and args and _root(args[0]) in JOB_TABLES:
                found.append(f"{n.lineno} lifecycle {name}({_root(args[0])})")
            elif name == "get" and args and _root(args[0]) == "AIBatchJob":
                found.append(f"{n.lineno} ownership get(AIBatchJob) by id alone")
            elif (name == "select" and args and _root(args[0]) == "AIBatchJob"
                  and not {"company_id", "user_id"} <= owner):
                found.append(f"{n.lineno} ownership select(AIBatchJob) without its company and user")
        if about_jobs and isinstance(statement, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for target in statement.targets if isinstance(statement, ast.Assign) else [statement.target]:
                for n in ast.walk(target):
                    if isinstance(n, ast.Attribute) and n.attr in JOB_STATE - {"status", "state"}:
                        found.append(f"{n.lineno} lifecycle .{n.attr} set")
                    elif isinstance(n, ast.Attribute) and n.attr in ("status", "state") and _root(n) in (
                            "job", "step", "row"):
                        found.append(f"{n.lineno} lifecycle {_root(n)}.{n.attr} set")
    return sorted(set(found))


def test_no_code_outside_the_job_module_decides_a_jobs_state_its_owner_or_its_transitions():
    found = [f"{path.relative_to(ROOT)}:{line}" for path in _scanned()
             for line in _bypasses(path.read_text(encoding="utf-8"))]
    assert found == []


@pytest.mark.parametrize("source,kind", [
    ("from celerp.models.ai import AIBatchJob\nq = select(AIBatchJob.id).where(AIBatchJob.status == 'pending')",
     "predicate AIBatchJob.status"),
    ("from celerp.models.ai import AIBatchJob\nq = select(AIBatchJob).where(AIBatchJob.lease_until > now)",
     "predicate AIBatchJob.lease_until"),
    ("from celerp.ai import jobs\nopen_ = job.status in ('pending', 'running')", "predicate .status"),
    ('from celerp.ai import jobs\nif job.status != "completed":\n    raise busy\n', "predicate .status"),
    ("from celerp.ai import jobs\nfailed = step.state == 'failed'", "predicate .state"),
    ("from celerp.models.ai import AIBatchJob\nasync def f(s, i):\n    return await s.get(AIBatchJob, i)",
     "ownership get(AIBatchJob)"),
    ("from celerp.models.ai import AIBatchJob\nq = select(AIBatchJob).where(AIBatchJob.company_id == c)",
     "ownership select(AIBatchJob)"),
    ("from celerp.models.ai import AIJobStep\nq = update(AIJobStep).values(state='done')", "lifecycle update(AIJobStep)"),
    ("from celerp.models.ai import AIBatchJob\nq = delete(AIBatchJob)", "lifecycle delete(AIBatchJob)"),
    ("from celerp.models.ai import AIJobStep\ns.add(AIJobStep(key='x'))", "lifecycle new AIJobStep"),
    ("from celerp.ai import jobs\njob = await jobs.owned_job(s, i)\njob.status = 'failed'", "lifecycle job.status set"),
    ("from celerp.ai import jobs\njob.lease_until = None", "lifecycle .lease_until set"),
])
def test_the_guard_finds_each_bypass(source, kind):
    assert any(kind in line for line in _bypasses(source)), _bypasses(source)


def test_the_guard_passes_a_scoped_report_through_the_job_helpers():
    source = ("from celerp.ai import jobs\nfrom celerp.models.ai import AIBatchJob, AIJobStep\n"
              "rows = select(AIBatchJob).where(AIBatchJob.company_id == c, AIBatchJob.user_id == u, jobs.is_open())\n"
              "steps = select(AIJobStep.key).where(AIJobStep.job_id == j)\n")
    assert _bypasses(source) == []
