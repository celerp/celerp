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

        async def _broken(job_id_):
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
        await jobs.recover_on_start()

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

    async def _run_job(job_id):
        attempts.append(job_id)
        if outage["on"]:
            raise RuntimeError("database unavailable")
        return await real(job_id)
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


# ── Guard: nothing outside the job module reads liveness from the status column ──

def _scanned() -> list[pathlib.Path]:
    paths = [p for p in (ROOT / "celerp").rglob("*.py") if "migrations" not in p.parts]
    paths += [p for p in (ROOT / "default_modules").glob("*/celerp_*/**/*.py")]
    return [p for p in paths if p != ROOT / "celerp" / "ai" / "jobs.py"]


def _open_literal(node: ast.AST) -> bool:
    values = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else [node]
    return any(isinstance(v, ast.Constant) and v.value in ("pending", "running") for v in values)


def test_no_code_outside_the_job_module_reads_whether_a_job_runs_from_its_status():
    """Whether a job runs is the lease's to say: no code but the job module names the
    job status column in a query, or compares a job row's status with an open status."""
    found = []
    for path in _scanned():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        about_jobs = "AIBatchJob" in source or "jobs." in source
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "status"
                    and isinstance(node.value, ast.Name) and node.value.id == "AIBatchJob"):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} AIBatchJob.status")
            if (about_jobs and isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute)
                    and node.left.attr == "status" and any(_open_literal(c) for c in node.comparators)):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} .status against an open status")
    assert found == []
