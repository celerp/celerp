# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI jobs: the one gate on model calls, and the durable runner for every AI task.

Every model call runs inside a slot of ``gate`` (``AI_MAX_CONCURRENT`` slots, the one
concurrency setting). A slot is taken as ``interactive`` (a question someone is
waiting on) or ``documents`` (a document reading job): document steps never hold the
last slot while there is more than one, and a waiting question is granted the next
free slot ahead of every waiting document step, so a question never waits behind a
queue of files. ``llm.complete`` refuses to run outside a slot.

A job is an ``AIBatchJob`` row and its ``AIJobStep`` rows: one step per file of a
reading job, one per model turn and tool call of a question. A step's operation id is the identity the AI service answers it by, so a
job that resumes after a restart sends a step again only under the identity it was
sent under: the service replays a stored reply, or reports an outcome it cannot
replay, which ends the step without running it again. Sending a step under a new
identity is always the user's explicit retry, which says what it counts as. The steps
are the job's progress; nothing else counts files.

Whether a job runs now is its runner lease, read by one rule (``liveness``): the
process running a job holds a lease on its row, renews it while it works and lets it
go when the job ends. A job is ``running`` while its lease holds, ``unheld`` (open, run
by nobody: its process is gone) once the lease lapsed, and ``ended`` once its status is
final. Every reader asks that rule, or its query forms ``runs_now`` and ``is_open``.
Stop ends an unheld job at once, as stopped; Continue or asking again resumes it. A
runner that raises is never given up on while its process lives: it runs again after
a growing delay and keeps its lease meanwhile, so the job reads as running and Stop
ends it.

A question is a job too, so no request waits on the model: the thread route answers
202 and the browser polls the question's state, and a synchronous caller (``/ai/query``,
the module API) waits at most ``deadlines.SYNC_WAIT_S`` from its start. A question
starts ``interactive``: a restart leaves it interrupted, for the user to ask again
under its id. One still running ``deadlines.PROMOTE_AFTER_S`` after its first model
step took a slot, or past a synchronous caller's wait, moves to ``background``: it
resumes after a restart and leaves a notification when it ends. Nothing bounds a
job's wall clock, only each step.

A job's run is built from its rows alone, so a run started by a request and one
resumed after a restart are the same run: a question's tools read the app as its
asker, under a bearer minted for each read from the asker's session nonce kept on the
job (a sign-out rotates it, and the read is refused), and the asker is admitted again
before every step. A job's id names one request (``JobRequest``): reusing the id for
anything else is refused as ``operation_conflict``.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import sqlalchemy as sa
from sqlalchemy import and_, or_, select, update

from celerp.ai import deadlines
from celerp.db import SessionLocal
from ui.i18n import t

log = logging.getLogger(__name__)

KINDS = ("interactive", "documents")

_slot: contextvars.ContextVar[str | None] = contextvars.ContextVar("ai_gate_slot", default=None)


def holding_slot() -> bool:
    """Whether the running task holds a gate slot."""
    return _slot.get() is not None


class ModelGate:
    """``size`` model calls at a time across the app, questions first."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("AI_MAX_CONCURRENT must be at least 1")
        self.size = size
        self._held = dict.fromkeys(KINDS, 0)
        self._waiting = dict.fromkeys(KINDS, 0)
        self._changed = asyncio.Condition()

    def _free_for(self, kind: str) -> bool:
        if sum(self._held.values()) >= self.size:
            return False
        if kind == "documents":
            return self._waiting["interactive"] == 0 and self._held["documents"] < max(1, self.size - 1)
        return True

    def busy_with(self) -> str:
        """What a waiter waits for: ``documents`` while a reading job holds a slot,
        otherwise ``questions``."""
        return "documents" if self._held["documents"] else "questions"

    @asynccontextmanager
    async def slot(self, kind: str, on_wait: Callable[[str | None], None] | None = None):
        """Hold one slot for the block. ``on_wait`` is told what the caller waits for
        while it waits, and None once the slot is granted."""
        if kind not in KINDS:
            raise ValueError(f"unknown gate slot kind {kind!r}")
        async with self._changed:
            if not self._free_for(kind):
                self._waiting[kind] += 1
                try:
                    while not self._free_for(kind):
                        if on_wait is not None:
                            on_wait(self.busy_with())
                        await self._changed.wait()
                finally:
                    self._waiting[kind] -= 1
                    self._changed.notify_all()
            self._held[kind] += 1
        if on_wait is not None:
            on_wait(None)
        token = _slot.set(kind)
        try:
            yield
        finally:
            _slot.reset(token)
            async with self._changed:
                self._held[kind] -= 1
                self._changed.notify_all()


gate = ModelGate(int(os.getenv("AI_MAX_CONCURRENT", "3")))

# Operation ids a job in this process may still send; the usage reconcile pass
# leaves their rows for later.
_held_operations: set[uuid.UUID] = set()
_tasks: dict[uuid.UUID, asyncio.Task] = {}
# Jobs reopened while their task was ending: the task runs them once more.
_again: set[uuid.UUID] = set()
# What a question waits for at the gate (``documents`` or ``questions``), while it waits.
_waiting: dict[uuid.UUID, str] = {}
# The questions running in background mode in this process (see ``promote``).
_background: set[uuid.UUID] = set()

# A step that raised something unexpected is run again under its own identity (the
# service replays what it already answered), then ends failed, which the user may
# retry. A runner that raised runs again after a growing delay, at most
# RUNNER_RESTART_MAX_DELAY_S apart, for as long as its process lives.
STEP_ATTEMPTS = 3
STEP_RETRY_DELAY_S = 1.0
RUNNER_RESTART_MAX_DELAY_S = 60.0
# The runner lease: this process's id, how long a claim holds, and how often a
# running job renews it (well inside the lease, so a slow renewal never lapses it).
RUNNER_ID = uuid.uuid4().hex
LEASE_S = 30.0
LEASE_RENEW_S = 10.0
# Sends of a background step under its own identity after the service surely did not
# run it, or after no reply came back in the call's budget.
RESEND_LIMIT = 3

OPEN_STEPS = ("pending", "running")
# A step that ended without an answer, which the user may run again under a new identity.
RETRYABLE_STEPS = ("failed", "unknown", "expired")
TERMINAL_JOBS = ("completed", "failed", "cancelled")

# The refusals the app's authorization answers a read with because of who asked at that
# moment (signed out, a permission taken away, a module turned off), by the code the
# question ends with. Such a result says nothing about the request, so it is never
# journaled: asking again reads again, under the session that asks.
CALLER_REFUSALS = {401: "session_expired", 403: "read_refused"}


def caller_refusal(result: dict) -> str | None:
    """The one rule for whether a read's result is a refusal of its caller: the code
    the question ends with, or None for a result the request itself decided."""
    if result.get("ok"):
        return None
    if (result.get("error") or {}).get("code") == "missing_authorization":
        return "session_expired"
    return CALLER_REFUSALS.get(result.get("status"))


@dataclasses.dataclass(frozen=True)
class JobRequest:
    """What a job was asked: everything the asker sends that its answer depends on.
    A job keeps its digest, and every path that reuses a job by its id holds the new
    request to it (``reused_job``)."""
    company_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    conversation_id: uuid.UUID | None
    query: str
    file_ids: tuple[str, ...]

    @classmethod
    def question(cls, *, company_id: uuid.UUID, user_id: uuid.UUID, query: str, file_ids: list[str] | None = None,
                 conversation_id: uuid.UUID | None = None) -> "JobRequest":
        return cls(company_id=company_id, user_id=user_id, kind="question", conversation_id=conversation_id,
                   query=query, file_ids=tuple(file_ids or ()))

    def digest(self) -> str:
        canonical = json.dumps(dataclasses.asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# What a caller names a job as: a question asked outside a thread, a question of a
# thread, or a reading job.
JOB_SHAPES = ("one_off", "conversation", "documents")


def job_shape(job) -> str:
    if job.kind == "documents":
        return "documents"
    return "one_off" if job.conversation_id is None else "conversation"


async def owned_job(session, job_id: uuid.UUID, *, company_id: uuid.UUID, user_id: uuid.UUID,
                    shape: str | None = None, conversation_id: uuid.UUID | None = None):
    """The one way a caller reads a job by its id: the row when it is this company's
    and this user's, of ``shape`` when given and of ``conversation_id`` when given.
    Otherwise None, which every caller answers alike as not found, so a job id never
    tells one user whether another's job exists."""
    from celerp.models.ai import AIBatchJob
    job = await session.get(AIBatchJob, job_id)
    if job is None or (job.company_id, job.user_id) != (company_id, user_id):
        return None
    if (shape is not None and job_shape(job) != shape) or (
            conversation_id is not None and job.conversation_id != conversation_id):
        return None
    return job


async def reused_job(session, job_id: uuid.UUID, request: JobRequest):
    """The one way a send reuses a job by its id, and the one comparison it is held to
    (the request digest): None when no job has the id, the job when it was created for
    ``request``. Another user's job is not found, as ``owned_job``; this user's job of
    another request is ``operation_conflict``. A job stored without an identity is
    never reused."""
    from fastapi import HTTPException

    from celerp.models.ai import AIBatchJob
    job = await session.get(AIBatchJob, job_id)
    if job is None:
        return None
    if (job.company_id, job.user_id) != (request.company_id, request.user_id):
        raise HTTPException(status_code=404, detail=t("ai.err_job_not_found"))
    if job.request_digest != request.digest():
        raise HTTPException(status_code=409, detail={"code": "operation_conflict",
                                                     "message": t("ai.err_operation_conflict")})
    return job


def held_operation_ids() -> set[uuid.UUID]:
    return set(_held_operations)


def file_operation_id(job_id: uuid.UUID, file_id: str, generation: int = 0) -> uuid.UUID:
    """The operation id one file of a reading job is asked under: generation 0 on the
    first run, the next generation on each explicit retry."""
    return uuid.uuid5(job_id, file_id if generation == 0 else f"{file_id}:{generation}")


def step_id(question_id: uuid.UUID, *, turn: int, generation: int = 0) -> uuid.UUID:
    """The identity of a question's model step after its first (which is sent under
    the question's own id)."""
    return uuid.uuid5(question_id, f"{turn}:{generation}")


# ── Liveness: the runner lease ────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def liveness(job, now: datetime | None = None) -> str:
    """Whether a job runs now: ``running`` while a runner's lease on it holds,
    ``unheld`` when it is open and nobody runs it, ``ended`` once its status is final.
    The one rule every reader asks (``runs_now`` and ``is_open`` are its query forms)."""
    if job.status in TERMINAL_JOBS:
        return "ended"
    return "running" if job.lease_until is not None and job.lease_until > (now or _now()) else "unheld"


def is_open():
    """The query form of ``liveness(job) != "ended"``."""
    from celerp.models.ai import AIBatchJob
    return AIBatchJob.status.not_in(TERMINAL_JOBS)


def runs_now():
    """The query form of ``liveness(job) == "running"``."""
    from celerp.models.ai import AIBatchJob
    return and_(is_open(), AIBatchJob.lease_until > _now())


def _lease_end() -> datetime:
    return _now() + timedelta(seconds=LEASE_S)


def hold(job) -> None:
    """Give this process the lease on a job row it writes."""
    job.runner_id, job.lease_until = RUNNER_ID, _lease_end()


def release(job) -> None:
    job.runner_id = job.lease_until = None


async def _claim(session, job_id: uuid.UUID, where=None, *, own: bool = False, **values) -> bool:
    """Take the lease on a job nobody runs (with ``own``, also renew this process's
    own: only the job's task asks that), with ``values`` in the same update, when the
    job matches ``where`` (by default: open). False, and nothing written, otherwise: a
    stop or a reopen never takes a job a runner holds, this process's included."""
    from celerp.models.ai import AIBatchJob
    free = or_(AIBatchJob.lease_until.is_(None), AIBatchJob.lease_until <= _now(),
               *([AIBatchJob.runner_id == RUNNER_ID] if own else []))
    claimed = await session.execute(update(AIBatchJob).where(AIBatchJob.id == job_id, free,
                                                             is_open() if where is None else where)
                                    .values(runner_id=RUNNER_ID, lease_until=_lease_end(), **values))
    return claimed.rowcount == 1


def task_alive(job_id: uuid.UUID) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


def start_job(job_id: uuid.UUID) -> bool:
    """Run the job in a task of this process. False when a task of this process
    already runs it: that task runs it once more when its current run ends, as the
    job may have been reopened meanwhile."""
    if task_alive(job_id):
        _again.add(job_id)
        return False
    _tasks[job_id] = asyncio.create_task(_supervise(job_id), name=f"ai-job-{job_id}")
    return True


async def _supervise(job_id: uuid.UUID) -> None:
    """The job's task: it holds the job's lease and renews it, runs the job, and when
    the run raises runs it again after a growing delay, for as long as the process
    lives. Meanwhile the job reads as running: a question is moved to the background
    so the thread offers Stop, and a Stop ends the job at the next failure."""
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(job_id, stop))
    failures = 0
    try:
        while True:
            async with SessionLocal() as session:
                claimed = await _claim(session, job_id, own=True)
                await session.commit()
            if not claimed:
                return      # ended, or another process runs it
            try:
                await _run(job_id)
            except Exception:
                failures += 1
                log.exception("AI job %s stopped (%d in a row); running it again", job_id, failures)
                await _after_failure(job_id)
                await asyncio.sleep(min(STEP_RETRY_DELAY_S * 2 ** failures, RUNNER_RESTART_MAX_DELAY_S))
                continue
            failures = 0
            if job_id not in _again:
                return
            _again.discard(job_id)
    finally:
        # Never cancelled mid-query: the renewal in flight ends, then the heartbeat does.
        stop.set()
        await asyncio.gather(beat, return_exceptions=True)
        if _tasks.get(job_id) is asyncio.current_task():
            del _tasks[job_id]


async def _heartbeat(job_id: uuid.UUID, stop: asyncio.Event) -> None:
    """Renew this process's lease on the job every LEASE_RENEW_S until ``stop``. A
    renewal that fails is tried again at the next beat."""
    from celerp.models.ai import AIBatchJob
    while True:
        try:
            await asyncio.wait_for(stop.wait(), LEASE_RENEW_S)
            return
        except TimeoutError:
            pass
        try:
            async with SessionLocal() as session:
                await session.execute(update(AIBatchJob).where(
                    AIBatchJob.id == job_id, AIBatchJob.runner_id == RUNNER_ID, is_open()
                ).values(lease_until=_lease_end()))
                await session.commit()
        except Exception:
            log.warning("AI job %s: its lease was not renewed", job_id, exc_info=True)


async def _after_failure(job_id: uuid.UUID) -> None:
    """After a run raised: a job its user stopped ends as stopped; a question moves to
    the background, where the thread offers Stop and a restart resumes it. Best effort:
    the next run tries again."""
    from celerp.models.ai import AIBatchJob
    try:
        async with SessionLocal() as session:
            job = await session.get(AIBatchJob, job_id)
            stopped, question = job.cancel_requested_at is not None, job.kind == "question"
        if stopped:
            await _end_stopped(job_id)
        elif question:
            await promote(job_id)
    except Exception:
        log.warning("AI job %s: its failure could not be recorded", job_id, exc_info=True)


async def _run(job_id: uuid.UUID) -> None:
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        kind = (await session.execute(select(AIBatchJob.kind).where(AIBatchJob.id == job_id))).scalar_one_or_none()
    await (_run_question(job_id) if kind == "question" else _run_job(job_id))


def running_tasks() -> list[asyncio.Task]:
    """The job tasks of this process, for the app's shutdown to cancel and wait for."""
    return list(_tasks.values())


async def shutdown() -> None:
    """Cancel every job task and wait for it. A step cut off mid-call keeps its state,
    so the next start resumes it under its own identity. Leases are left to lapse, as
    a crash leaves them; the next start lets them go (``recover_on_start``)."""
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _again.clear()
    _held_operations.clear()
    _waiting.clear()
    _background.clear()


async def _refusal(session, company_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    """Why a job may not send its next step, as the job's error key, or None. The one
    admission rule for a job's work, checked at start-up and before every step is sent:
    the same permission and AI access rules that admit an AI route."""
    from celerp.services.permissions import read_authority, role_has_permission
    from celerp.session_gate import ai_access_available
    role, settings = await read_authority(session, company_id, user_id)
    if not (role and role_has_permission(settings, role, "use_ai_assistant")):
        return "ai.job_permission_lost"
    if not await ai_access_available():
        return "ai.job_ai_unavailable"
    return None


async def recover_on_start() -> int:
    """Run once at start-up, before the usage reconcile loop. This process is the one
    that runs AI jobs, so a lease no task of it holds belongs to a process that is
    gone: it is let go. Every open background job then resumes under this process's
    lease: its open steps are registered as held before this returns, so no reconcile
    pass asks the service about a step this process is about to send, and then its
    task starts. A job whose user may no longer use the assistant ends failed with
    that reason. An interactive question is left unheld, which reads as interrupted:
    it is never sent again but by the user asking again under its id. Returns how
    many jobs resumed."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    resumed: list[uuid.UUID] = []
    async with SessionLocal() as session:
        rows = (await session.execute(select(AIBatchJob).where(is_open()))).scalars().all()
        for job in rows:
            if task_alive(job.id):
                continue
            release(job)
            if job.mode == "interactive":
                continue
            # A question is admitted before each of its model steps, and stores a refusal
            # as its answer (``_JobJournal.admit``).
            refusal = await _refusal(session, job.company_id, job.user_id) if job.kind == "documents" else None
            if refusal is not None:
                job.status, job.error = "failed", t(refusal)
                job.completed_at = _now()
                continue
            ops = (await session.execute(
                select(AIJobStep.operation_id).where(AIJobStep.job_id == job.id, AIJobStep.state.in_(OPEN_STEPS))
            )).scalars().all()
            _held_operations.update(op for op in ops if op is not None)
            if job.kind == "question":
                _held_operations.add(job.id)
            hold(job)
            resumed.append(job.id)
        await session.commit()
    for job_id in resumed:
        start_job(job_id)
    return len(resumed)


async def _reopen(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID, *, retry: bool) -> int | None:
    """Mark a job running again and start it; with ``retry``, every step that ended
    without an answer first moves to its next generation (a new identity). Returns the
    number of steps reopened, or None when the job is not this user's or still runs."""
    from celerp.models.ai import AIJobStep
    async with SessionLocal() as session:
        job = await owned_job(session, job_id, company_id=company_id, user_id=user_id)
        if job is None or liveness(job) == "running":
            return None
        steps = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id,
                                    AIJobStep.state.in_(RETRYABLE_STEPS if retry else OPEN_STEPS))
        )).scalars().all()
        if not steps:
            return 0
        if retry:
            for step in steps:
                step.generation += 1
                step.operation_id = step.step_id = file_operation_id(job_id, step.key, step.generation)
                step.state, step.result, step.charge_known = "pending", None, False
        if not await _claim(session, job_id, sa.true(), status="running", error=None, completed_at=None,
                            cancel_requested_at=None):
            await session.rollback()        # another send took it first
            return None
        await session.commit()
    start_job(job_id)
    return len(steps)


async def resume(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
    """Send the steps a cancelled job left unsent, each under its own identity."""
    return await _reopen(job_id, company_id, user_id, retry=False)


async def retry_failed(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
    """Send again, each under a new identity, the files that ended without an answer
    (see ``retry_offer`` for what that counts as)."""
    return await _reopen(job_id, company_id, user_id, retry=True)


async def cancel(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Stop a job. One that runs stops at its next step: steps not yet sent stay
    pending (``resume`` sends them), a step already sent finishes inside its own bound
    and is kept. One nobody runs ends now, as stopped. False when the job is not this
    user's or has ended."""
    async with SessionLocal() as session:
        job = await owned_job(session, job_id, company_id=company_id, user_id=user_id)
        if job is None or liveness(job) == "ended":
            return False
        job.cancel_requested_at = _now()
        unheld = liveness(job) == "unheld"
        await session.commit()
        claimed = unheld and await _claim(session, job_id)
        await session.commit()
    if claimed:
        await _end_stopped(job_id)
    return True


async def _end_stopped(job_id: uuid.UUID) -> None:
    """End a job its user stopped, from its rows, as its runner would have: a reading
    job with steps left is cancelled (Continue sends them), a question ends failed as
    stopped (asking again continues it)."""
    from celerp.ai import service
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        run = _file_run(job) if job.kind == "documents" else None
    if run is None:
        await _finish_question(job_id, service.failure(Stopped(), service.RunTally()), None)
    else:
        await _end_job(run)


def _file_run(job):
    from celerp.ai import batch
    return batch.FileRun(job_id=job.id, company_id=job.company_id, user_id=job.user_id,
                         conversation_id=job.conversation_id, query=job.query)


# ── The runner ────────────────────────────────────────────────────────────────

async def _run_job(job_id: uuid.UUID) -> None:
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        if job is None or liveness(job) == "ended":
            return
        rows = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id, AIJobStep.state.in_(OPEN_STEPS))
            .order_by(AIJobStep.seq)
        )).scalars().all()
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        await session.commit()
        run = _file_run(job)
        steps = [(s.id, s.key, s.operation_id) for s in rows]
    _held_operations.update(op for _, _, op in steps)
    try:
        # The group owns every step task: a cancelled or failed runner cancels and
        # drains them all before it ends, so no step outlives its job's runner.
        async with asyncio.TaskGroup() as group:
            for step in steps:
                group.create_task(_supervised_step(run, *step))
    finally:
        _held_operations.difference_update(op for _, _, op in steps)
    await _end_job(run)


async def _supervised_step(run, step_pk: uuid.UUID, file_id: str, operation_id: uuid.UUID) -> None:
    """Run one step to an end without stopping the others: an unexpected error runs it
    again under its own identity, then ends it failed for the user to retry. Only when
    even that cannot be written does the error stop the runner (the step stays open
    and the supervised restart sends it again under its identity)."""
    for attempt in range(1, STEP_ATTEMPTS + 1):
        try:
            await _document_step(run, step_pk, file_id, operation_id)
            return
        except Exception:
            log.exception("Step %s of AI job %s failed (attempt %d)", file_id, run.job_id, attempt)
            if attempt < STEP_ATTEMPTS:
                await asyncio.sleep(STEP_RETRY_DELAY_S * attempt)
    async with SessionLocal() as session:
        await _set_step(session, step_pk, state="failed",
                        result={"filename": file_id, "error": t("ai.err_file_unexpected")})
        await session.commit()
    _held_operations.discard(operation_id)


async def _cancel_requested(job_id: uuid.UUID) -> bool:
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        return (await session.execute(
            select(AIBatchJob.cancel_requested_at).where(AIBatchJob.id == job_id)
        )).scalar_one_or_none() is not None


async def _document_step(run, step_pk: uuid.UUID, file_id: str, operation_id: uuid.UUID) -> None:
    """One file: it takes a ``documents`` slot, is marked running in the transaction
    that writes its usage row just before it is sent, and ends with what the service
    answered. Cancelled by shutdown, it keeps its state for the resume."""
    from celerp.ai import batch, usage

    async def _before_send(nbytes: int) -> None:
        async with SessionLocal() as session:
            await _set_step(session, step_pk, state="running")
            await usage.record_usage(session, company_id=run.company_id, user_id=run.user_id,
                                     operation_id=operation_id, send_bytes=nbytes)
            await session.commit()

    async with gate.slot("documents"):
        if run.stop_reason is not None or await _cancel_requested(run.job_id):
            return
        async with SessionLocal() as session:
            refusal = await _refusal(session, run.company_id, run.user_id)
            await session.commit()
        if refusal is not None:
            # Unsent steps stay pending and uncharged; finished ones keep their results.
            run.stop_reason = refusal
            return
        outcome = await batch.read_file(run, file_id, operation_id, before_send=_before_send)
    async with SessionLocal() as session:
        await _set_step(session, step_pk, state=outcome.state, result=outcome.result,
                        credits=outcome.credits, charge_known=outcome.known)
        if outcome.sent:
            await usage.record_usage(session, company_id=run.company_id, user_id=run.user_id,
                                     operation_id=operation_id, credits=outcome.credits, known=outcome.known)
        await session.commit()
    _held_operations.discard(operation_id)
    await batch.publish_progress(run, file_id, outcome.state)


async def _set_step(session, step_pk: uuid.UUID, *, credits: int | None = None, **values) -> None:
    from celerp.models.ai import AIJobStep
    if credits is not None:
        # A retried file keeps what its earlier generations were charged.
        values["credits"] = AIJobStep.credits + credits
    await session.execute(update(AIJobStep).where(AIJobStep.id == step_pk).values(
        updated_at=datetime.now(timezone.utc), **values))


async def _end_job(run) -> None:
    from celerp.ai import batch
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, run.job_id)
        states = (await session.execute(
            select(AIJobStep.state).where(AIJobStep.job_id == run.job_id))).scalars().all()
        if run.stop_reason is not None:
            job.status, job.error = "failed", t(run.stop_reason)
        elif job.cancel_requested_at is not None and any(s in OPEN_STEPS for s in states):
            job.status = "cancelled"
        elif all(s in RETRYABLE_STEPS for s in states):
            job.status, job.error = "failed", t("ai.job_failed")
        else:
            job.status = "completed"
        job.cancel_requested_at = None
        job.completed_at = _now()
        release(job)
        status = job.status
        await session.commit()
    if status != "cancelled":
        done = states.count("done")
        await batch.notify_finished(run, done, len(states) - done)


# ── Sending a step ────────────────────────────────────────────────────────────

async def send_step(call: Callable[[], Awaitable], *, resend: bool,
                    on_refusal: Callable[[BaseException], None] | None = None):
    """Send one model step (the caller holds its gate slot) and return the reply. The
    step is sent again only under its own identity: while its first send still runs on
    the service, and, when ``resend`` (a background step), after the service surely did
    not run it or no reply came back, at most RESEND_LIMIT times. Anything else is
    raised. ``on_refusal`` is told every refusal, with the credits it reports."""
    from celerp.ai.llm import RelayError
    resends = 0
    while True:
        try:
            return await call()
        except RelayError as exc:
            if on_refusal is not None:
                on_refusal(exc)
            if exc.code == "operation_in_progress":
                await asyncio.sleep(min(exc.retry_after or deadlines.RESEND_DELAY_S, deadlines.IN_PROGRESS_POLL_S))
                continue
            if resend and exc.code in ("service_unavailable", "busy") and resends < RESEND_LIMIT:
                resends += 1
                await asyncio.sleep(deadlines.RESEND_DELAY_S)
                continue
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            if resend and resends < RESEND_LIMIT:
                resends += 1
                continue
            raise


async def notify(company_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID | None,
                 title: str, body: str) -> None:
    """The notification a finished background job leaves for its user (best effort)."""
    try:
        from celerp.notifications.service import create as create_notification
        async with SessionLocal() as session:
            await create_notification(
                session, company_id, "ai", title, body, user_id=user_id,
                action_url=f"/ai?conversation={conversation_id}" if conversation_id else "/ai",
                priority="high",
            )
            await session.commit()
    except Exception:
        log.warning("Failed to create an AI job notification", exc_info=True)


# ── Questions ─────────────────────────────────────────────────────────────────

class Journal:
    """What each step of one question returned, by key (``model:{turn}`` or
    ``tool:{call_id}``). The agent loop asks it before it runs a step, so a question
    run again is answered from what its ended steps returned: no model step that
    ended is sent again, no tool runs twice, and a model step is sent again only
    under its identity. This one keeps the journal for one run in memory;
    ``_JobJournal`` keeps it in the job's steps."""

    def __init__(self, question_id: uuid.UUID) -> None:
        self.question_id = question_id
        self._done: dict[str, dict] = {}
        self._generation: dict[str, int] = {}

    @property
    def background(self) -> bool:
        """Whether nobody waits on the question (its steps are resent on their own)."""
        return False

    def stored(self, key: str) -> dict | None:
        return self._done.get(key)

    def identity(self, turn: int) -> dict:
        """What model turn ``turn`` is sent under: the question's id on the first turn,
        its step id after."""
        if turn == 0:
            return {"operation_id": self.question_id}
        generation = self._generation.get(f"model:{turn}", 0)
        return {"step_id": step_id(self.question_id, turn=turn, generation=generation)}

    async def admit(self) -> None:
        """Raise when the question may not send its next model step."""

    def on_wait(self, what: str | None) -> None:
        """Told what the next model step waits for at the gate, and None once it holds a slot."""

    async def sending(self, key: str, identity: dict, nbytes: int) -> None:
        """Called just before a model step leaves, with its body's size."""

    async def ended(self, key: str, state: str, result: dict | None = None) -> None:
        """A step ended: ``done`` with what it returned, or without an answer."""
        if state == "done":
            self._done[key] = result


class Stopped(Exception):
    """The user stopped the question: it sends no further model step. Asking it again
    under its id continues it from the steps that ended."""


class _JobJournal(Journal):
    def __init__(self, job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID, steps: list) -> None:
        super().__init__(job_id)
        self.job_id, self.company_id, self.user_id = job_id, company_id, user_id
        self._pk: dict[str, uuid.UUID] = {}
        self._seq = max((step.seq for step in steps), default=-1) + 1
        self._promoter: asyncio.Task | None = None
        for step in steps:
            self._pk[step.key], self._generation[step.key] = step.id, step.generation
            if step.state == "done":
                self._done[step.key] = step.result

    @property
    def background(self) -> bool:
        return self.job_id in _background

    async def admit(self) -> None:
        from fastapi import HTTPException

        from celerp.models.ai import AIBatchJob
        async with SessionLocal() as session:
            refusal = await _refusal(session, self.company_id, self.user_id)
            stopped = (await session.execute(
                select(AIBatchJob.cancel_requested_at).where(AIBatchJob.id == self.job_id))).scalar_one_or_none()
            await session.commit()
        if stopped is not None:
            raise Stopped()
        if refusal is not None:
            raise HTTPException(status_code=403, detail={"code": refusal.removeprefix("ai."), "message": t(refusal)})

    def on_wait(self, what: str | None) -> None:
        if what is not None:
            _waiting[self.job_id] = what
            return
        _waiting.pop(self.job_id, None)
        if self._promoter is None and not self.background:
            # P counts from the first model step's slot, never from time spent queued.
            self._promoter = asyncio.create_task(self._promote_later())

    async def _promote_later(self) -> None:
        await asyncio.sleep(deadlines.PROMOTE_AFTER_S)
        await promote(self.job_id)

    async def _step(self, session, key: str, state: str, identity: dict | None = None) -> uuid.UUID:
        from celerp.models.ai import AIJobStep
        if key in self._pk:
            await _set_step(session, self._pk[key], state=state)
            return self._pk[key]
        identity = identity or {}
        step = AIJobStep(job_id=self.job_id, seq=self._seq, key=key, state=state,
                         operation_id=self.question_id if key.startswith("model:") else None,
                         step_id=identity.get("step_id") or identity.get("operation_id"))
        session.add(step)
        await session.flush()
        self._seq += 1
        self._pk[key] = step.id
        return step.id

    async def sending(self, key: str, identity: dict, nbytes: int) -> None:
        from celerp.ai import usage
        async with SessionLocal() as session:
            await self._step(session, key, "running", identity)
            await usage.record_usage(session, company_id=self.company_id, user_id=self.user_id,
                                     operation_id=self.question_id, send_bytes=nbytes)
            await session.commit()

    async def ended(self, key: str, state: str, result: dict | None = None) -> None:
        await super().ended(key, state, result)
        if state != "done" and key not in self._pk:
            return      # never sent: nothing to record
        async with SessionLocal() as session:
            pk = await self._step(session, key, state)
            await _set_step(session, pk, state=state, result=result, credits=(result or {}).get("credits") or None)
            await session.commit()

    def close(self) -> None:
        if self._promoter is not None:
            self._promoter.cancel()
        _waiting.pop(self.job_id, None)


def waiting_for(job_id: uuid.UUID) -> str | None:
    """What a running question waits for at the gate (``documents`` or ``questions``), or None."""
    return _waiting.get(job_id)


async def promote(job_id: uuid.UUID) -> None:
    """Move a question to background mode: it resumes after a restart, its steps are
    resent on their own, and it leaves a notification when it ends."""
    from celerp.models.ai import AIBatchJob
    _background.add(job_id)
    async with SessionLocal() as session:
        await session.execute(
            update(AIBatchJob).where(AIBatchJob.id == job_id, AIBatchJob.promoted_at.is_(None))
            .values(mode="background", promoted_at=datetime.now(timezone.utc)))
        await session.commit()


def question_job(*, job_id: uuid.UUID, request: JobRequest, auth_nonce: str | None = None):
    """A new question job for ``request``, held by this process; the caller adds it to
    its session, commits, then starts it. A thread question's job takes the question's
    id, which its first model step is sent under; a question asked outside a thread
    proposes no changes. ``auth_nonce`` is the asker's session nonce, which its tools
    read the app under; a question asked without a user session reads nothing."""
    from celerp.models.ai import AIBatchJob
    job = AIBatchJob(id=job_id, company_id=request.company_id, user_id=request.user_id,
                     conversation_id=request.conversation_id,
                     question_id=job_id if request.conversation_id else None, kind="question", mode="interactive",
                     status="pending", query=request.query, file_ids=list(request.file_ids),
                     request_digest=request.digest(), auth_nonce=auth_nonce)
    hold(job)
    return job


async def reopen_question(session, job_id: uuid.UUID, auth_nonce: str | None) -> bool:
    """Ask a question again under its id: its ended steps are kept, so the run is
    answered from them, and any other step is sent again under its own identity. Its
    tools read the app under the session of whoever asks it again (``auth_nonce``).
    One conditional update, which takes the job's lease for this process, so a send
    that read the question before its job answered it never reopens the answer and
    one that runs is never reopened; False then."""
    from celerp.models.ai import AIBatchJob
    return await _claim(session, job_id, AIBatchJob.status != "completed",
                        status="pending", results=None, error=None, completed_at=None, cancel_requested_at=None,
                        auth_nonce=auth_nonce)


def _bearer(job, role: str) -> Callable[[], str] | None:
    """What a question's tools read the app with: a bearer for the job's asker, minted
    for each read from the session nonce kept on the job, so every read is authorized
    by the app as the asker's own request is (a sign-out rotates the nonce and the read
    is refused). None, and no tools, for a question asked without a user session."""
    if not job.auth_nonce:
        return None
    from celerp.credentials import create_access_token
    user_id, company_id, nonce = str(job.user_id), str(job.company_id), job.auth_nonce
    return lambda: "Bearer " + create_access_token(user_id, company_id, role, snonce=nonce)[0]


async def _question_context(session, job) -> dict:
    """Every argument of the question's agent run but its journal, from the job's rows
    and the app's current authorization: the one source for a run started by a request
    and for one resumed after a restart."""
    from celerp.ai.conversations import build_history_context, get_messages
    from celerp.ai.memory import get_memory
    from celerp.main import app
    from celerp.models.ai import AIMessage
    from celerp.services.permissions import read_authority
    role, company_settings = await read_authority(session, job.company_id, job.user_id)
    history: list[dict] = []
    if job.conversation_id is not None:
        asked = await session.get(AIMessage, job.question_id)
        prior = await get_messages(session, job.conversation_id)
        history = build_history_context([m for m in prior if m.created_at < asked.created_at])
    return dict(app=app, bearer=_bearer(job, role or ""), operation_id=job.id, query=job.query,
                company_id=job.company_id, user_id=job.user_id, company_settings=company_settings,
                role=role or "", memory=await get_memory(session, job.company_id),
                file_ids=job.file_ids or None, history=history, read_only=job.conversation_id is None)


async def _run_question(job_id: uuid.UUID) -> None:
    from celerp.ai import service
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        if job is None or liveness(job) == "ended":
            return
        steps = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id).order_by(AIJobStep.seq))).scalars().all()
        run = await _question_context(session, job)
        if job.mode == "background":
            _background.add(job_id)
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        journal = _JobJournal(job_id, job.company_id, job.user_id, steps)
        await session.commit()
    _held_operations.add(job_id)
    try:
        refusal = None
        try:
            result = await service.run_agent(journal=journal, **run)
        except service.AgentRefusal as exc:
            result, refusal = None, exc
        await _finish_question(job_id, result, refusal)
    finally:
        journal.close()
        _held_operations.discard(job_id)


async def _finish_question(job_id: uuid.UUID, result, refusal) -> None:
    """Store how the question ended, in the same commit that ends its lease: in its
    thread, the answer, the failure or the refusal as the assistant message; for a
    question outside a thread, on the job. Then a promoted question notifies."""
    from dataclasses import asdict

    from celerp.ai import usage
    from celerp.ai.conversations import add_message, failure_record, help_record, record_credits
    from celerp.models.ai import AIBatchJob, AIMessage
    ended = refusal or result
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        if job.conversation_id is not None:
            asked = await session.get(AIMessage, job.question_id)
            await record_credits(session, asked, ended.credits, known=ended.charge_known,
                                 company_id=job.company_id, user_id=job.user_id)
            if refusal is not None:
                detail = refusal.detail if isinstance(refusal.detail, dict) else {"message": str(refusal.detail)}
                msg = await add_message(session, job.conversation_id, "assistant", str(detail.get("message") or ""),
                                        tools_called=[failure_record(detail.get("code"), status=refusal.status_code,
                                                                     refusal=detail)])
            else:
                if result.error:
                    tools_called = [*result.tools_called, failure_record(result.error_code)]
                else:
                    tools_called = [*result.tools_called, *[asdict(p) for p in result.pending_actions],
                                    *help_record(result.help_pages)]
                msg = await add_message(session, job.conversation_id, "assistant", result.error or result.answer,
                                        model_used=result.model_used, tools_called=tools_called)
            job.results = {"message_id": str(msg.id)}
        else:
            await usage.record_usage(session, company_id=job.company_id, user_id=job.user_id, operation_id=job_id,
                                     credits=ended.credits, known=ended.charge_known)
            if refusal is not None:
                job.results = {"refusal": {"status": refusal.status_code, "detail": refusal.detail,
                                           "headers": dict(refusal.headers or {})}}
            else:
                job.results = {"answer": result.answer, "model_used": result.model_used,
                               "tools_called": result.tools_called, "help_pages": result.help_pages,
                               "error": result.error, "error_code": result.error_code}
        failed = refusal is not None or bool(result.error)
        job.status = "failed" if failed else "completed"
        job.completed_at = _now()
        release(job)
        promoted = job.promoted_at is not None
        company_id, user_id, conversation_id = job.company_id, job.user_id, job.conversation_id
        await session.commit()
    _background.discard(job_id)
    if promoted:
        key = "ai.question_failed_title" if failed else "ai.question_answered_title"
        await notify(company_id, user_id, conversation_id, t(key), t("ai.question_done_body"))


async def wait_for_job(job_id: uuid.UUID, *, started: float) -> bool:
    """Wait for this process's task running the job until ``deadlines.SYNC_WAIT_S``
    after ``started`` (a monotonic instant: the request's start, so time queued at the
    gate counts). True when no such task runs any more; otherwise the job is promoted,
    as the caller is told it keeps running."""
    while task_alive(job_id):
        left = deadlines.SYNC_WAIT_S - (time.monotonic() - started)
        if left <= 0:
            await promote(job_id)
            return False
        await asyncio.wait({_tasks[job_id]}, timeout=left)
    return True


async def ask(*, company_id: uuid.UUID, user_id: uuid.UUID, query: str, file_ids: list[str] | None = None,
              job_id: uuid.UUID | None = None, auth_nonce: str | None = None, started: float) -> dict:
    """A question from a synchronous caller, outside any thread: its result within
    ``deadlines.SYNC_WAIT_S`` of ``started``, or ``{"status": "running", "job_id"}``.
    The same ``job_id`` with the same request asks it again: a finished answer is
    returned as it is, one that failed or that nobody runs continues under this
    asker's session (``auth_nonce``, as ``question_job``). Two first sends under one id
    both get the one question."""
    from sqlalchemy.exc import IntegrityError
    job_id = job_id or uuid.uuid4()
    request = JobRequest.question(company_id=company_id, user_id=user_id, query=query, file_ids=file_ids)
    async with SessionLocal() as session:
        job = await reused_job(session, job_id, request)
        started_here = False
        if job is None:
            session.add(question_job(job_id=job_id, request=request, auth_nonce=auth_nonce))
            try:
                await session.commit()
                started_here = True
            except IntegrityError:
                await session.rollback()
                job = await reused_job(session, job_id, request)     # another send created it first
        if job is not None and liveness(job) != "running":
            started_here = await reopen_question(session, job_id, auth_nonce)
            await session.commit()
    if started_here:
        start_job(job_id)
    await wait_for_job(job_id, started=started)
    return await question_result(job_id, company_id, user_id)


async def question_result(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """Where a question this user asked outside a thread stands: ``running``,
    ``interrupted`` (nobody runs it: asking again under its id continues it), or ended
    (``completed`` or ``failed``) with what it returned. None for no such question of
    this user's."""
    async with SessionLocal() as session:
        row = await owned_job(session, job_id, company_id=company_id, user_id=user_id, shape="one_off")
    if row is None:
        return None
    state = liveness(row)
    if state == "ended":
        return {"status": row.status, "job_id": str(job_id), **(row.results or {})}
    return {"status": "running" if state == "running" else "interrupted", "job_id": str(job_id)}


async def stored_question_state(session, question_id: uuid.UUID, *, company_id: uuid.UUID, user_id: uuid.UUID,
                                conversation_id: uuid.UUID) -> dict | None:
    """A thread question's state: ``running``, ``interrupted`` (nobody runs it: asking
    again under its id continues it), or ``answered`` or ``failed`` with its assistant
    message once its job ended. None when the conversation has no such question job."""
    from celerp.ai.conversations import message_failure
    from celerp.models.ai import AIMessage
    row = await owned_job(session, question_id, company_id=company_id, user_id=user_id, shape="conversation",
                          conversation_id=conversation_id)
    if row is None:
        return None
    state = liveness(row)
    out = {"state": "interrupted" if state == "unheld" else "running", "promoted": row.promoted_at is not None,
           "message": None}
    if state == "ended":
        msg = await session.get(AIMessage, uuid.UUID(row.results["message_id"]))
        out.update(state="failed" if message_failure(msg.tools_called) else "answered", message=msg)
    return out


# ── What a job shows ──────────────────────────────────────────────────────────

FILE_STATUS = {"done": "success", "failed": "error", "unknown": "not_processed", "expired": "not_processed",
                "pending": "pending", "running": "running"}


async def job_steps(session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    """Every step of each job, in order, by job id."""
    from celerp.models.ai import AIJobStep
    out: dict[uuid.UUID, list] = {job_id: [] for job_id in job_ids}
    if job_ids:
        rows = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id.in_(job_ids)).order_by(AIJobStep.job_id, AIJobStep.seq)
        )).scalars().all()
        for step in rows:
            out[step.job_id].append(step)
    return out


def file_view(step) -> dict:
    """One file of a reading job as the user sees it."""
    result = step.result or {}
    return {"file_id": step.key, "filename": result.get("filename") or step.key,
            "status": FILE_STATUS[step.state], "charged": step.credits > 0, "credits": step.credits,
            "error": result.get("error"), "answer": result.get("answer"), "extraction": result.get("extraction")}


def retry_offer(steps: list) -> dict | None:
    """What retrying the files that ended without an answer counts as, or None."""
    n = sum(1 for s in steps if s.state in RETRYABLE_STEPS)
    if n == 0:
        return None
    if n == 1:
        return {"count": 1, "label": t("ai.retry_one_file"), "confirm": t("ai.retry_one_file_confirm")}
    return {"count": n, "label": t("ai.retry_files", n=n), "confirm": t("ai.retry_files_confirm", n=n)}
