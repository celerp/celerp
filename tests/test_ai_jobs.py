# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One durable job runner for every AI task, and one model gate for the process.

A job is a row and its steps: one step per file of a reading job, one per model
turn and tool call of a question. A step's identity (operation id, step id) is what
the AI service answers it by, so a job that resumes after a restart sends a step
again only under the identity it was sent under: the service replays a stored reply,
and refuses to run again a step whose outcome it cannot report. Running a step under
a new identity is always an explicit user action that says what it counts as.

The AI service is played over HTTP by its step rules (tests/ai_fake_cloud.py). A kill
is ``jobs.shutdown()`` (every task cancelled mid-step), a restart is
``jobs.recover_on_start()``.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import select, update

from ai_fake_cloud import answer, cloud, tool_turn, until  # noqa: F401
from celerp.ai import deadlines, jobs, llm, service, usage
from celerp.config import settings
from celerp.models.ai import AIBatchJob, AIJobStep, AIUsage
from celerp.models.notification import Notification
from celerp.services.company_lock import locked_company
from test_ai_usage import _conversation, owner  # noqa: F401

AUTH = lambda token: {"Authorization": f"Bearer {token}"}  # noqa: E731
TERMINAL = {"completed", "failed", "cancelled"}


def _files(company_id, user_id, n: int) -> list[str]:
    upload_dir = settings.data_dir / "ai_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        fid = f"ai_up_{uuid.uuid4().hex}"
        (upload_dir / f"{fid}.bin").write_bytes(b"%PDF-1.4 receipt")
        (upload_dir / f"{fid}.meta").write_text(json.dumps({
            "filename": f"receipt_{i:03}.pdf", "content_type": "application/pdf", "size": 16,
            "company_id": str(company_id), "user_id": str(user_id)}))
        out.append(fid)
    return out


async def _reading_job(client, token, company_id, user_id, n: int) -> tuple[uuid.UUID, list[str], str]:
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, n)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read these receipts", "file_ids": file_ids, "document_mode": "receipts",
        "operation_id": str(uuid.uuid4())})
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["job_id"]), file_ids, conv


async def _job(client, token, job_id) -> dict:
    r = await client.get(f"/ai/batch/{job_id}", headers=AUTH(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _finished(client, token, job_id, timeout: float = 20.0) -> dict:
    holder: dict = {}

    async def _terminal():
        holder["job"] = await _job(client, token, job_id)
        return holder["job"]["status"] in TERMINAL
    await until(_terminal, timeout)
    return holder["job"]


def _op(job_id, file_id: str, generation: int = 0) -> str:
    return str(jobs.file_operation_id(job_id, file_id, generation))


def _by_file(job: dict) -> dict[str, dict]:
    return {f["file_id"]: f for f in job["files"]}


async def _kill_and_restart() -> None:
    await jobs.shutdown()
    await jobs.recover_on_start()


# ── Identities ────────────────────────────────────────────────────────────────

def test_a_file_keeps_its_generation_zero_id_and_a_retry_takes_the_next_generation():
    job_id = uuid.uuid4()
    assert jobs.file_operation_id(job_id, "f1", 0) == uuid.uuid5(job_id, "f1")
    assert jobs.file_operation_id(job_id, "f1", 1) == uuid.uuid5(job_id, "f1:1")


# ── Red 25: a reading job resumes after a kill ────────────────────────────────

async def test_a_killed_reading_job_resumes_and_no_file_runs_twice_under_one_id(client, owner, cloud, monkeypatch):
    """100 files, gate at 4 (documents take at most 3 slots); killed with 10 done, 3
    in the model and 87 pending. On restart the done files are not sent again, the
    three in flight are sent again under their own ids (one gets its stored reply, two
    are reported lost, charged and not charged, and are never sent a third time), and
    the pending files run."""
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(4))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 100)
    job_id = uuid.uuid4()
    ops = [str(jobs.file_operation_id(job_id, f, 0)) for f in file_ids]
    stored, lost_charged, lost_uncharged = ops[10], ops[11], ops[12]
    cloud.held(stored, end="store")
    cloud.held(lost_charged, end="lost_charged")
    cloud.held(lost_uncharged, end="lost_uncharged")
    held = {stored, lost_charged, lost_uncharged}
    # Files after the held ones wait at the gate: the three document slots are taken.
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read these receipts", "file_ids": file_ids, "document_mode": "receipts",
        "operation_id": str(job_id)})
    assert r.status_code == 202, r.text
    await until(lambda: held <= set(cloud.arrived) and len(cloud.arrived) == 13)

    await _kill_and_restart()
    job = await _finished(client, token, job_id)

    files = _by_file(job)
    assert all(cloud.executions[op] == 1 for op in ops)
    assert all(cloud.sends[op] == 1 for op in ops[:10])
    assert cloud.sends[stored] == 2 and files[file_ids[10]]["status"] == "success"
    assert (files[file_ids[11]]["status"], files[file_ids[11]]["charged"]) == ("not_processed", True)
    assert (files[file_ids[12]]["status"], files[file_ids[12]]["charged"]) == ("not_processed", False)
    assert cloud.sends[lost_charged] == cloud.sends[lost_uncharged] == 2
    assert [files[f]["status"] for f in file_ids[13:]] == ["success"] * 87
    assert (job["status"], job["completed_files"], job["failed_files"]) == ("completed", 98, 2)


async def test_the_job_progress_is_read_from_its_steps(client, owner, cloud, session):
    """One source: the job's files and counts come from ai_job_steps."""
    token, company_id, user_id = owner
    job_id, file_ids, _ = await _reading_job(client, token, company_id, user_id, 3)
    job = await _finished(client, token, job_id)
    steps = (await session.execute(select(AIJobStep).where(AIJobStep.job_id == job_id)
                                   .order_by(AIJobStep.seq))).scalars().all()
    assert [s.key for s in steps] == file_ids
    assert [str(s.operation_id) for s in steps] == [_op(job_id, f) for f in file_ids]
    assert all(s.state == "done" and s.step_id == s.operation_id and s.generation == 0 for s in steps)
    assert [f["file_id"] for f in job["files"]] == file_ids
    row = await session.get(AIBatchJob, job_id)
    assert "files" not in (row.results or {})


async def test_retry_failed_files_states_what_it_counts_as_then_runs_each_under_a_new_id(client, owner, cloud):
    token, company_id, user_id = owner
    # 14 files, 12 of which come back lost (not charged): the service never ran them to an answer.
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 14)
    job_id = uuid.uuid4()
    for f in file_ids[:12]:
        cloud.state[_op(job_id, f)] = "lost_uncharged"
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    job = await _finished(client, token, job_id)
    assert job["retry_failed"] == {"count": 12, "label": "Retry 12 files",
                                   "confirm": "This counts as 12 questions, one per file."}
    assert all(cloud.executions[_op(job_id, f)] == 0 for f in file_ids[:12])

    r = await client.post(f"/ai/jobs/{job_id}/retry-failed", headers=AUTH(token))
    assert r.status_code == 202, r.text
    job = await _finished(client, token, job_id)
    assert all(cloud.executions[_op(job_id, f, 1)] == 1 for f in file_ids[:12])
    assert all(cloud.sends[_op(job_id, f)] == 1 for f in file_ids)
    assert [f["status"] for f in job["files"]] == ["success"] * 14
    assert job["retry_failed"] is None


async def test_retry_of_one_file_says_one_question(client, owner, cloud):
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 2)
    job_id = uuid.uuid4()
    cloud.state[_op(job_id, file_ids[0])] = "lost_charged"
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    job = await _finished(client, token, job_id)
    assert job["retry_failed"] == {"count": 1, "label": "Retry 1 file", "confirm": "This counts as 1 question."}


async def test_cancel_keeps_finished_files_and_resume_sends_no_file_twice(client, owner, cloud, monkeypatch):
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 6)
    job_id = uuid.uuid4()
    in_flight = _op(job_id, file_ids[2])
    cloud.held(in_flight)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: in_flight in cloud.arrived)

    r = await client.post(f"/ai/jobs/{job_id}/cancel", headers=AUTH(token))
    assert r.status_code == 202, r.text
    cloud.release(in_flight)   # the step in flight finishes inside its own bound and is kept
    job = await _finished(client, token, job_id)
    assert job["status"] == "cancelled"
    assert [f["status"] for f in job["files"]] == ["success"] * 3 + ["pending"] * 3

    r = await client.post(f"/ai/jobs/{job_id}/resume", headers=AUTH(token))
    assert r.status_code == 202, r.text
    job = await _finished(client, token, job_id)
    assert job["status"] == "completed"
    assert all(cloud.sends[_op(job_id, f)] == 1 for f in file_ids)


async def test_a_restart_resumes_before_the_first_reconcile_so_a_dead_upload_is_sent_not_fenced(
        client, owner, cloud, session, monkeypatch):
    """G2: the upload of a file died before the service admitted it and the app was down
    past the file's reconcile time. The restart registers and re-arms the step before
    any reconcile pass, so the pass does not ask about it and the file is sent."""
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 2)
    job_id = uuid.uuid4()
    first = _op(job_id, file_ids[0])
    cloud.held(first)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: first in cloud.arrived)
    await jobs.shutdown()
    # The service never admitted the second file: the app died between its usage row and the send.
    second = _op(job_id, file_ids[1])
    await session.execute(update(AIJobStep).where(AIJobStep.operation_id == uuid.UUID(second))
                          .values(state="running"))
    await session.execute(update(AIUsage).values(reconcile_at=AIUsage.created_at))
    await session.commit()
    cloud.release(first)

    gate_open = asyncio.Event()
    real_slot = jobs.gate.slot

    def _slot(kind):
        async def _wait_then_take():
            await gate_open.wait()
        cm = real_slot(kind)
        class _Held:
            async def __aenter__(self):
                await _wait_then_take()
                return await cm.__aenter__()
            async def __aexit__(self, *exc):
                return await cm.__aexit__(*exc)
        return _Held()
    monkeypatch.setattr(jobs.gate, "slot", _slot)

    await jobs.recover_on_start()
    assert uuid.UUID(second) in jobs.held_operation_ids()
    await usage.reconcile_usage(session)
    assert all(second not in ids for ids in cloud.status_asks)
    gate_open.set()
    job = await _finished(client, token, job_id)
    assert cloud.executions[second] == 1
    assert _by_file(job)[file_ids[1]]["status"] == "success"


async def test_a_fenced_file_is_not_processed_not_charged_once(client, owner, cloud):
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 2)
    job_id = uuid.uuid4()
    fenced = _op(job_id, file_ids[0])
    cloud.state[fenced] = "fenced"
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    job = await _finished(client, token, job_id)
    f = _by_file(job)[file_ids[0]]
    assert (f["status"], f["charged"]) == ("not_processed", False)
    assert cloud.sends[fenced] == 1 and cloud.executions[fenced] == 0
    assert job["retry_failed"]["count"] == 1


async def test_a_resumed_job_whose_user_lost_the_permission_ends_failed(client, owner, cloud, session, monkeypatch):
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    job_id = uuid.uuid4()
    cloud.held(_op(job_id, file_ids[0]))
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: _op(job_id, file_ids[0]) in cloud.arrived)
    await jobs.shutdown()
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "role_grants": {"use_ai_assistant": []}}
    await session.commit()

    await jobs.recover_on_start()
    # The batch route itself needs the permission, so the job is read from its row.
    async def _ended():
        row = await session.get(AIBatchJob, job_id, populate_existing=True)
        return row.status in TERMINAL
    await until(_ended)
    row = await session.get(AIBatchJob, job_id)
    assert row.status == "failed"
    assert row.error == "This job stopped because you no longer have permission to use the AI assistant."
    assert cloud.sends[_op(job_id, file_ids[1])] == 0


# ── Owner audit N1: a supervised runner ──────────────────────────────────────

def _step_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_supervised_step"]


async def test_a_result_write_that_fails_once_resends_the_file_under_its_id_and_the_job_finishes(
        client, owner, cloud, monkeypatch):
    """The database refuses the write of one file's answer once: the file is sent again
    under its own id (the service replays it, it is not run twice) and every other
    file still finishes."""
    monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.01)
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    job_id = uuid.uuid4()
    target = _step_pk(job_id, file_ids[1])
    real_set_step = jobs._set_step
    failed = []

    async def _set_step(session, claim, step_pk, **values):
        if values.get("state") == "done" and step_pk == await target() and not failed:
            failed.append(step_pk)
            raise RuntimeError("database write refused")
        return await real_set_step(session, claim, step_pk, **values)
    monkeypatch.setattr(jobs, "_set_step", _set_step)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    job = await _finished(client, token, job_id)

    assert failed
    assert job["status"] == "completed"
    assert [f["status"] for f in job["files"]] == ["success"] * 3
    op = _op(job_id, file_ids[1])
    assert (cloud.executions[op], cloud.sends[op]) == (1, 2)


def _step_pk(job_id, key):
    """Reads the primary key of the job's step for ``key`` once the step exists."""
    cache: dict = {}

    async def _pk():
        if "pk" not in cache:
            async with jobs.SessionLocal() as s:
                cache["pk"] = (await s.execute(select(AIJobStep.id).where(
                    AIJobStep.job_id == job_id, AIJobStep.key == key))).scalar_one_or_none()
        return cache["pk"]
    return _pk


async def test_a_file_that_keeps_raising_ends_retryable_and_the_other_files_finish(
        client, owner, cloud, monkeypatch):
    from celerp.ai import batch
    monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.01)
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    real_read = batch.read_file
    tries = []

    async def _read(run, file_id, operation_id, **kw):
        if file_id == file_ids[1]:
            tries.append(file_id)
            raise RuntimeError("unexpected")
        return await real_read(run, file_id, operation_id, **kw)
    monkeypatch.setattr(batch, "read_file", _read)
    r = await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(uuid.uuid4())})
    job = await _finished(client, token, uuid.UUID(r.json()["job_id"]))

    assert len(tries) == jobs.STEP_ATTEMPTS
    files = _by_file(job)
    assert [files[f]["status"] for f in file_ids] == ["success", "error", "success"]
    assert files[file_ids[1]]["error"] == "Something went wrong while reading this file. Retry it to read it again."
    assert (job["status"], job["retry_failed"]["count"]) == ("completed", 1)


async def test_a_runner_that_raises_is_started_again_and_finishes_without_resending_a_file(
        client, owner, cloud, monkeypatch):
    monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.01)
    token, company_id, user_id = owner
    real_end = jobs._end_job
    ends = []

    async def _end_job(run):
        ends.append(run.job_id)
        if len(ends) == 1:
            raise RuntimeError("database gone for a moment")
        return await real_end(run)
    monkeypatch.setattr(jobs, "_end_job", _end_job)
    job_id, file_ids, _ = await _reading_job(client, token, company_id, user_id, 3)
    job = await _finished(client, token, job_id)

    assert len(ends) == 2 and job["status"] == "completed"
    assert all(cloud.executions[_op(job_id, f)] == 1 for f in file_ids)


async def test_a_runner_that_cannot_record_a_failure_drains_its_steps_and_runs_the_job_again(
        client, owner, cloud, session, monkeypatch):
    """No step outlives its run: when a step's failure cannot even be written, every
    other step of the job is cancelled and awaited before the run ends. The job is
    never left stranded: its runner keeps it, reading as running, and runs it again
    until it finishes, without running any file twice."""
    from celerp.ai import batch
    monkeypatch.setattr(jobs, "STEP_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(jobs, "RUNNER_RESTART_MAX_DELAY_S", 0.01)
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(4))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    job_id = uuid.uuid4()
    held = _op(job_id, file_ids[0])
    cloud.held(held, end="store")
    real_read, real_set_step = batch.read_file, jobs._set_step
    broken = {"on": True}

    async def _read(run, file_id, operation_id, **kw):
        if file_id == file_ids[1] and broken["on"]:
            await until(lambda: held in cloud.arrived)
            raise RuntimeError("unexpected")
        return await real_read(run, file_id, operation_id, **kw)

    async def _set_step(session_, claim, step_pk, **values):
        if values.get("state") == "failed" and broken["on"]:
            raise RuntimeError("database write refused")
        return await real_set_step(session_, claim, step_pk, **values)
    left_running: list[list] = []
    real_after_failure = jobs._after_failure

    async def _after_failure(job_id_, runner_id):
        left_running.append(_step_tasks())
        broken["on"] = False                    # the next run can write again
        await real_after_failure(job_id_, runner_id)
    monkeypatch.setattr(batch, "read_file", _read)
    monkeypatch.setattr(jobs, "_set_step", _set_step)
    monkeypatch.setattr(jobs, "_after_failure", _after_failure)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    job = await _finished(client, token, job_id)

    assert left_running == [[]]
    assert [f["status"] for f in job["files"]] == ["success"] * 3
    assert all(cloud.executions[_op(job_id, f)] == 1 for f in file_ids)


# ── Owner audit N2: every step is admitted before it is sent ─────────────────

async def test_a_permission_revoked_mid_job_stops_the_unsent_files_and_keeps_the_finished_one(
        client, owner, cloud, session, monkeypatch):
    token, company_id, user_id = owner
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(2))  # documents take one slot: one file at a time
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    job_id = uuid.uuid4()
    first = _op(job_id, file_ids[0])
    cloud.held(first, end="store")
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: first in cloud.arrived)
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "role_grants": {"use_ai_assistant": []}}
    await session.commit()
    cloud.release(first)

    async def _ended():
        return (await session.get(AIBatchJob, job_id, populate_existing=True)).status in TERMINAL
    await until(_ended)
    row = await session.get(AIBatchJob, job_id)
    steps = {s.key: s for s in (await session.execute(
        select(AIJobStep).where(AIJobStep.job_id == job_id).execution_options(populate_existing=True))).scalars()}
    assert (row.status, row.error) == (
        "failed", "This job stopped because you no longer have permission to use the AI assistant.")
    assert (steps[file_ids[0]].state, steps[file_ids[0]].credits) == ("done", 1)
    assert [steps[f].state for f in file_ids[1:]] == ["pending", "pending"]
    assert all(cloud.sends[_op(job_id, f)] == 0 for f in file_ids[1:])
    usage_ops = set((await session.execute(select(AIUsage.operation_id).where(
        AIUsage.company_id == company_id))).scalars())
    assert uuid.UUID(first) in usage_ops and uuid.UUID(_op(job_id, file_ids[1])) not in usage_ops


async def test_ai_access_lost_mid_job_stops_the_unsent_files_and_resume_sends_them(
        client, owner, cloud, monkeypatch):
    from celerp import session_gate
    token, company_id, user_id = owner
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(2))
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 3)
    job_id = uuid.uuid4()
    first = _op(job_id, file_ids[0])
    cloud.held(first, end="store")
    access = {"on": True}

    async def _available():
        return access["on"]
    monkeypatch.setattr(session_gate, "ai_access_available", _available)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: first in cloud.arrived)
    access["on"] = False
    cloud.release(first)
    await until(lambda: not jobs.task_alive(job_id))
    access["on"] = True  # the routes are admitted by the same rule
    job = await _job(client, token, job_id)
    assert job["status"] == "failed"
    assert job["error"] == ("This job stopped because Celerp is no longer connected to the AI service. "
                            "Reconnect, then resume the job.")
    assert [f["status"] for f in job["files"]] == ["success", "pending", "pending"]
    assert all(cloud.sends[_op(job_id, f)] == 0 for f in file_ids[1:])

    r = await client.post(f"/ai/jobs/{job_id}/resume", headers=AUTH(token))
    assert r.status_code == 202, r.text
    job = await _finished(client, token, job_id)
    assert job["status"] == "completed" and [f["status"] for f in job["files"]] == ["success"] * 3
    assert all(cloud.executions[_op(job_id, f)] == 1 for f in file_ids)


# ── Red 25: questions as jobs ─────────────────────────────────────────────────

_CAPS = {"help_search": {
    "name": "help_search", "method": "GET", "path": "/help/search", "path_names": (), "query_names": ("q",),
    "expects_body": False, "inject_idempotency": False, "required_permissions": (), "result_permissions": (), "field_visibility": False,
    "tool": {"type": "function", "function": {"name": "help_search", "description": "help_search",
                                              "parameters": {}}}}}


async def _ask(client, token, conv, qid, query="How do I add a supplier?"):
    return await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token),
                             json={"query": query, "operation_id": str(qid)})


async def _question(client, token, conv, qid) -> dict:
    r = await client.get(f"/ai/conversations/{conv}/questions/{qid}", headers=AUTH(token))
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_question_past_p_is_promoted_notified_and_resumes_from_its_journal(
        client, owner, cloud, session, monkeypatch):
    """Promoted at P after its first model step took the gate; killed during its second
    model step; resumed from the journal: the first step is not run again and the
    second carries the original tool call id."""
    monkeypatch.setattr(deadlines, "PROMOTE_AFTER_S", 0.2)
    monkeypatch.setattr(service, "compile_agent_capabilities", lambda app, settings, role=None: _CAPS)

    async def _exec(app, authz, capability, arguments, call_id, *, result_max_bytes=None):
        return {"ok": True, "status": 200, "data": {"pages": ["contacts.add-supplier"]}}
    monkeypatch.setattr(service, "execute_agent_capability", _exec)
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.script = lambda identity, body: (tool_turn(("help_search", {"query": {"q": "supplier"}}, "call-1"))
                                           if identity == str(qid) else answer("Open Contacts."))
    second = str(jobs.step_id(qid, turn=1, generation=0))
    cloud.held(second, end="store")

    r = await _ask(client, token, conv, qid)
    assert r.status_code == 202, r.text
    await until(lambda: second in cloud.arrived)
    await asyncio.sleep(0.3)
    state = await _question(client, token, conv, qid)
    assert (state["state"], state["promoted"]) == ("running", True)
    assert state["notice"] == ("This is taking longer than usual. It keeps working in the background, "
                               "and you'll get a notification when it's done.")

    await _kill_and_restart()
    await until(lambda: cloud.sends[second] == 2)
    async def _answered():
        return (await _question(client, token, conv, qid))["state"] == "answered"
    await until(_answered)
    assert cloud.executions[str(qid)] == 1 and cloud.sends[str(qid)] == 1
    resent = [b for b in cloud.bodies if b.get("step_id") == second][-1]
    tool_ids = [m.get("tool_call_id") for m in resent["messages"] if m.get("role") == "tool"]
    assert tool_ids == ["call-1"]
    notes = (await session.execute(select(Notification).where(Notification.company_id == company_id))).scalars().all()
    assert len(notes) == 1


async def test_an_interactive_question_at_restart_ends_interrupted_and_ask_again_is_charged_once(
        client, owner, cloud, session):
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    qid = uuid.uuid4()
    cloud.held(str(qid), end="store")
    assert (await _ask(client, token, conv, qid)).status_code == 202
    await until(lambda: str(qid) in cloud.arrived)

    await _kill_and_restart()
    state = await _question(client, token, conv, qid)
    assert state["state"] == "interrupted"
    assert cloud.sends[str(qid)] == 1   # an interactive question is never resent by the restart

    assert (await _ask(client, token, conv, qid)).status_code == 202   # "Ask again": the same id
    async def _answered():
        return (await _question(client, token, conv, qid))["state"] == "answered"
    await until(_answered)
    assert cloud.executions[str(qid)] == 1
    (row,) = (await session.execute(select(AIUsage).where(AIUsage.operation_id == qid))).scalars().all()
    assert row.credits == 1


async def test_query_and_module_api_return_running_at_y_and_the_job_answers_later(
        client, owner, cloud, session, monkeypatch):
    from celerp.modules.api import ai_job, ai_query
    from test_ai_usage import signed_request

    monkeypatch.setattr(deadlines, "SYNC_WAIT_S", 0.3)
    monkeypatch.setattr("celerp.session_gate.get_session_token", lambda: "session-1")
    token, company_id, user_id = owner
    blocked: list[str] = []

    def _hold_first(identity, body):
        if not blocked:
            blocked.append(identity)
            cloud.held(identity)
        return answer("Forty-two.")
    cloud.script = _hold_first

    started = time.monotonic()
    r = await client.post("/ai/query", headers=AUTH(token), json={"query": "How many?"})
    assert r.status_code == 202, r.text
    assert time.monotonic() - started < 0.3 + 2.0
    body = r.json()
    assert body["status"] == "running"
    job = await session.get(AIBatchJob, uuid.UUID(body["job_id"]))
    assert job.mode == "background" and job.promoted_at is not None
    cloud.release(blocked[0])

    blocked.clear()
    async with signed_request(session, company_id, user_id):
        out = await ai_query(query="How many?", company_id=str(company_id), session_token="session-1",
                             db_session=session)
    assert out["status"] == "running"
    cloud.release(blocked[0])
    async with signed_request(session, company_id, user_id):
        async def _done():
            got = await ai_job(out["job_id"], company_id=str(company_id), db_session=session)
            return got["status"] == "completed" and got["answer"] == "Forty-two."
        await until(_done)


async def test_a_queued_synchronous_call_returns_running_within_y_of_its_start(client, owner, cloud, monkeypatch):
    """G3: with one slot held by a long step, /ai/query waits at the gate; Y counts from
    the request start, so it still returns `running` by Y and the job is promoted."""
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    monkeypatch.setattr(deadlines, "SYNC_WAIT_S", 0.3)
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 1)
    job_id = uuid.uuid4()
    long_step = _op(job_id, file_ids[0])
    cloud.held(long_step)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: long_step in cloud.arrived)

    started = time.monotonic()
    r = await client.post("/ai/query", headers=AUTH(token), json={"query": "How many?"})
    assert time.monotonic() - started < 0.3 + 2.0
    assert r.status_code == 202 and r.json()["status"] == "running"
    queued = uuid.UUID(r.json()["job_id"])
    assert len(cloud.arrived) == 1          # it never reached the model: the slot is held

    await jobs.shutdown()
    cloud.release(long_step)
    await jobs.recover_on_start()           # promoted at Y, so it resumes after a restart
    await until(lambda: len(cloud.arrived) == 2)
    assert cloud.arrived[1] == str(queued)  # the queued question, resumed under its own id


# ── Red 27: one gate, interactive priority ────────────────────────────────────

async def test_a_question_takes_a_slot_while_a_reading_job_runs(client, owner, cloud, monkeypatch):
    """N = 3: document steps hold at most 2 slots, so a question never waits for one."""
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(3))
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 100)
    job_id = uuid.uuid4()
    file_ops = {_op(job_id, f) for f in file_ids}
    cloud.held(*file_ops)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: len(cloud.arrived) == 2)
    await asyncio.sleep(0.1)
    assert len(cloud.arrived) == 2          # the job never holds the third slot

    qid = uuid.uuid4()
    await _ask(client, token, (await _conversation(client, token)), qid)
    await until(lambda: str(qid) in cloud.arrived, timeout=2)
    for op in list(cloud.hold):
        cloud.release(op)


async def test_with_one_slot_a_question_goes_ahead_of_every_queued_file_and_says_why_it_waits(
        client, owner, cloud, monkeypatch):
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    monkeypatch.setattr(deadlines, "PROMOTE_AFTER_S", 0.2)
    token, company_id, user_id = owner
    conv = await _conversation(client, token)
    file_ids = _files(company_id, user_id, 5)
    job_id = uuid.uuid4()
    first = _op(job_id, file_ids[0])
    cloud.held(first)
    await client.post(f"/ai/conversations/{conv}/query", headers=AUTH(token), json={
        "query": "Read", "file_ids": file_ids, "document_mode": "receipts", "operation_id": str(job_id)})
    await until(lambda: first in cloud.arrived)

    qconv = await _conversation(client, token)
    qid = uuid.uuid4()
    assert (await _ask(client, token, qconv, qid)).status_code == 202
    await asyncio.sleep(0.4)                # longer than P, all of it at the gate
    state = await _question(client, token, qconv, qid)
    assert (state["state"], state["waiting"], state["promoted"]) == ("running", "documents", False)
    assert state["notice"] == "Waiting for a document reading job to free up"

    cloud.release(first)
    await until(lambda: len(cloud.arrived) >= 2)
    assert cloud.arrived[1] == str(qid)     # granted the next free slot, ahead of 4 queued files
    await _finished(client, token, job_id)
    assert cloud.max_in_model == 1          # one gate: one model call in total at N = 1


async def test_a_waiting_question_names_another_question(client, owner, cloud, monkeypatch):
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    token, company_id, user_id = owner
    a, b = uuid.uuid4(), uuid.uuid4()
    cloud.held(str(a))
    await _ask(client, token, await _conversation(client, token), a)
    await until(lambda: str(a) in cloud.arrived)
    conv = await _conversation(client, token)
    await _ask(client, token, conv, b)
    await asyncio.sleep(0.05)
    state = await _question(client, token, conv, b)
    assert (state["waiting"], state["notice"]) == ("questions", "Waiting for another AI question to finish")
    cloud.release(str(a))


async def test_the_model_cannot_be_called_outside_the_gate(cloud):
    with pytest.raises(AssertionError):
        await llm.complete([{"role": "user", "content": "hi"}])


async def test_a_steps_declared_budget_is_computed_after_it_takes_the_slot(cloud, monkeypatch):
    monkeypatch.setattr(jobs, "gate", jobs.ModelGate(1))
    order: list[str] = []
    real_budget = deadlines.call_budget

    def _budget(limits, n):
        order.append("budget")
        return real_budget(limits, n)
    monkeypatch.setattr(deadlines, "call_budget", _budget)
    async with jobs.gate.slot("interactive"):
        order.append("slot")
        await llm.complete([{"role": "user", "content": "hi"}])
    assert order == ["slot", "budget"]


async def test_a_finished_jobs_notification_is_written_under_the_company_lock(client, owner, session):
    """A job's task runs inside the request that started it; the notification it leaves
    when it ends takes the company lock first, as every company write does."""
    import mutation_observer
    _token, company_id, user_id = owner
    with mutation_observer.serving("POST", "/ai/conversations/{conversation_id}/query"):
        await jobs.notify(company_id, user_id, None, "2 of 2 file(s) read", "Open the conversation.")
    findings = mutation_observer.CENSUS.findings
    assert findings == [], mutation_observer.explain(findings)
    notes = (await session.execute(select(Notification).where(Notification.company_id == company_id))).scalars().all()
    assert [n.title for n in notes] == ["2 of 2 file(s) read"]


@pytest.mark.parametrize("write", ["claim", "fenced", "end_unheld"])
async def test_a_job_row_is_never_claimed_or_ended_without_its_company_lock(session, write):
    """A claim, a runner's final write and a stop each lock a job row, and only ever
    after the company lock of its transaction (``jobs.lock_for_ai``): one asked for
    without it is refused before it reads or writes anything."""
    job_id = uuid.uuid4()
    calls = {"claim": lambda: jobs._claim(session, job_id),
             "fenced": lambda: jobs._fenced(session, job_id, "runner"),
             "end_unheld": lambda: jobs._end_unheld(session, AIBatchJob.id == job_id)}
    with pytest.raises(RuntimeError, match="without its company lock"):
        await calls[write]()
