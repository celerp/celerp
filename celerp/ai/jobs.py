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
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

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
# Consecutive unexpected failures of a job's runner in this process.
_failures: dict[uuid.UUID, int] = {}
# What a question waits for at the gate (``documents`` or ``questions``), while it waits.
_waiting: dict[uuid.UUID, str] = {}
# The questions running in background mode in this process (see ``promote``).
_background: set[uuid.UUID] = set()

# A step that raised something unexpected is run again under its own identity (the
# service replays what it already answered), then ends failed, which the user may
# retry. A runner that raised is started again after a growing delay, and after
# RUNNER_RESTARTS failures in a row the job is left open for the next start.
STEP_ATTEMPTS = 3
STEP_RETRY_DELAY_S = 1.0
RUNNER_RESTARTS = 5
RUNNER_RESTART_MAX_DELAY_S = 60.0
# Sends of a background step under its own identity after the service surely did not
# run it, or after no reply came back in the call's budget.
RESEND_LIMIT = 3

OPEN_STEPS = ("pending", "running")
# A step that ended without an answer, which the user may run again under a new identity.
RETRYABLE_STEPS = ("failed", "unknown", "expired")
TERMINAL_JOBS = ("completed", "failed", "cancelled")


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
    """The one way a send reuses a job by its id: None when no job has the id, the job
    when it was created for ``request``, else ``operation_conflict``. A job stored
    without an identity is never reused."""
    from celerp.models.ai import AIBatchJob
    job = await session.get(AIBatchJob, job_id)
    if job is not None and job.request_digest != request.digest():
        from fastapi import HTTPException
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


def is_held(job_id: uuid.UUID) -> bool:
    """Whether a task of this process is running the job."""
    task = _tasks.get(job_id)
    return task is not None and not task.done()


def start_job(job_id: uuid.UUID, *, delay_s: float = 0.0) -> bool:
    """Run the job's open steps in a task of this process. False, and nothing
    started, while a task already runs it.

    The task is supervised: when its runner raises, the job keeps its open steps and
    is started again, after ``delay_s`` growing with each failure in a row."""
    if is_held(job_id):
        return False
    task = asyncio.create_task(_run_job_after(job_id, delay_s), name=f"ai-job-{job_id}")
    _tasks[job_id] = task

    def _supervise(done: asyncio.Task) -> None:
        if _tasks.get(job_id) is done:
            del _tasks[job_id]
        if done.cancelled():
            return
        if done.exception() is None:
            _failures.pop(job_id, None)
            return
        failures = _failures[job_id] = _failures.get(job_id, 0) + 1
        if failures > RUNNER_RESTARTS:
            # No task runs it now: it reads as interrupted, never as running in the background.
            _failures.pop(job_id, None)
            _background.discard(job_id)
            log.error("AI job %s stopped %d times; it resumes at the next start", job_id, failures,
                      exc_info=done.exception())
            return
        log.error("AI job %s stopped; starting it again", job_id, exc_info=done.exception())
        start_job(job_id, delay_s=min(STEP_RETRY_DELAY_S * 2 ** failures, RUNNER_RESTART_MAX_DELAY_S))
    task.add_done_callback(_supervise)
    return True


async def _run_job_after(job_id: uuid.UUID, delay_s: float) -> None:
    if delay_s:
        await asyncio.sleep(delay_s)
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        kind = (await session.execute(select(AIBatchJob.kind).where(AIBatchJob.id == job_id))).scalar_one_or_none()
    await (_run_question(job_id) if kind == "question" else _run_job(job_id))


def running_tasks() -> list[asyncio.Task]:
    """The job tasks of this process, for the app's shutdown to cancel and wait for."""
    return list(_tasks.values())


async def shutdown() -> None:
    """Cancel every job task and wait for it. A step cut off mid-call keeps its state,
    so the next start resumes it under its own identity."""
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _failures.clear()
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
    """Run once at start-up, before the usage reconcile loop. Every open background
    job resumes: its open steps are registered as held before this returns, so no
    reconcile pass asks the service about a step this process is about to send, and
    then its task starts. A job whose user may no longer use the assistant ends
    failed with that reason. An interactive question is left as it was, which reads
    as interrupted: it is never sent again but by the user asking again under its
    id. Returns how many jobs resumed."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    resumed: list[uuid.UUID] = []
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(AIBatchJob).where(AIBatchJob.status.in_(("pending", "running")))
        )).scalars().all()
        for job in rows:
            if job.mode == "interactive":
                continue
            # A question is admitted before each of its model steps, and stores a refusal
            # as its answer (``_JobJournal.admit``).
            refusal = await _refusal(session, job.company_id, job.user_id) if job.kind == "documents" else None
            if refusal is not None:
                job.status, job.error = "failed", t(refusal)
                job.completed_at = datetime.now(timezone.utc)
                continue
            ops = (await session.execute(
                select(AIJobStep.operation_id).where(AIJobStep.job_id == job.id, AIJobStep.state.in_(OPEN_STEPS))
            )).scalars().all()
            _held_operations.update(op for op in ops if op is not None)
            if job.kind == "question":
                _held_operations.add(job.id)
            resumed.append(job.id)
        await session.commit()
    for job_id in resumed:
        start_job(job_id)
    return len(resumed)


async def _reopen(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID, *, retry: bool) -> int | None:
    """Mark a job running again and start it; with ``retry``, every step that ended
    without an answer first moves to its next generation (a new identity). Returns the
    number of steps reopened, or None when the job is not this user's or still runs."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await owned_job(session, job_id, company_id=company_id, user_id=user_id)
        if job is None or is_held(job_id):
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
        job.status, job.error, job.completed_at, job.cancel_requested_at = "running", None, None, None
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
    """Ask a running job to stop: steps not yet sent stay pending (``resume`` sends
    them), a step already sent finishes inside its own bound and is kept."""
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        result = await session.execute(
            update(AIBatchJob)
            .where(AIBatchJob.id == job_id, AIBatchJob.company_id == company_id, AIBatchJob.user_id == user_id,
                   AIBatchJob.status.in_(("pending", "running")))
            .values(cancel_requested_at=datetime.now(timezone.utc))
        )
        await session.commit()
    return result.rowcount == 1


# ── The runner ────────────────────────────────────────────────────────────────

async def _run_job(job_id: uuid.UUID) -> None:
    from celerp.ai import batch
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        if job is None or job.status in TERMINAL_JOBS:
            return
        rows = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id, AIJobStep.state.in_(OPEN_STEPS))
            .order_by(AIJobStep.seq)
        )).scalars().all()
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        await session.commit()
        run = batch.FileRun(job_id=job.id, company_id=job.company_id, user_id=job.user_id,
                            conversation_id=job.conversation_id, query=job.query)
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
        job.completed_at = datetime.now(timezone.utc)
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
    """A new question job for ``request``; the caller adds it to its session, commits,
    then starts it. A thread question's job takes the question's id, which its first
    model step is sent under; a question asked outside a thread proposes no changes.
    ``auth_nonce`` is the asker's session nonce, which its tools read the app under;
    a question asked without a user session reads nothing."""
    from celerp.models.ai import AIBatchJob
    return AIBatchJob(id=job_id, company_id=request.company_id, user_id=request.user_id,
                      conversation_id=request.conversation_id,
                      question_id=job_id if request.conversation_id else None, kind="question", mode="interactive",
                      status="pending", query=request.query, file_ids=list(request.file_ids),
                      request_digest=request.digest(), auth_nonce=auth_nonce)


async def reopen_question(session, job_id: uuid.UUID, auth_nonce: str | None) -> bool:
    """Ask a question again under its id: its ended steps are kept, so the run is
    answered from them, and any other step is sent again under its own identity. Its
    tools read the app under the session of whoever asks it again (``auth_nonce``).
    One conditional update, so a send that read the question before its job answered
    it never reopens the answer; False when the question is answered."""
    from celerp.models.ai import AIBatchJob
    reopened = await session.execute(
        update(AIBatchJob).where(AIBatchJob.id == job_id, AIBatchJob.status != "completed")
        .values(status="pending", results=None, error=None, completed_at=None, cancel_requested_at=None,
                auth_nonce=auth_nonce))
    return reopened.rowcount == 1


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
        if job is None or job.status in TERMINAL_JOBS:
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
    """Store how the question ended, before its task leaves the registry: in its
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
        job.completed_at = datetime.now(timezone.utc)
        promoted = job.promoted_at is not None
        company_id, user_id, conversation_id = job.company_id, job.user_id, job.conversation_id
        await session.commit()
    _background.discard(job_id)
    if promoted:
        key = "ai.question_failed_title" if failed else "ai.question_answered_title"
        await notify(company_id, user_id, conversation_id, t(key), t("ai.question_done_body"))


async def wait_for_job(job_id: uuid.UUID, *, started: float) -> bool:
    """Wait for the job until ``deadlines.SYNC_WAIT_S`` after ``started`` (a monotonic
    instant: the request's start, so time queued at the gate counts). True when it
    ended; otherwise it is promoted, as the caller is told it keeps running."""
    while is_held(job_id):
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
    returned as it is. ``auth_nonce`` is the asker's session nonce (``question_job``)."""
    job_id = job_id or uuid.uuid4()
    request = JobRequest.question(company_id=company_id, user_id=user_id, query=query, file_ids=file_ids)
    async with SessionLocal() as session:
        job = await reused_job(session, job_id, request)
        if job is None:
            session.add(question_job(job_id=job_id, request=request, auth_nonce=auth_nonce))
        else:
            if job.status not in TERMINAL_JOBS and not is_held(job_id):
                job.auth_nonce = auth_nonce     # an interrupted question, continued by this asker
        reopened = job is not None and job.status == "failed" and not is_held(job_id) and (
            await reopen_question(session, job_id, auth_nonce))
        await session.commit()
    if job is None or reopened or job.status not in TERMINAL_JOBS:
        start_job(job_id)
        await wait_for_job(job_id, started=started)
    return await question_result(job_id, company_id, user_id)


async def question_result(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """Where a question this user asked outside a thread stands: ``running``,
    ``interrupted`` (no task of this process runs it: a restart, or a runner that kept
    failing, ended it; asking again under its id resumes it), or ended (``completed``
    or ``failed``) with what it returned. None for no such question of this user's."""
    held = is_held(job_id)
    async with SessionLocal() as session:
        row = await owned_job(session, job_id, company_id=company_id, user_id=user_id, shape="one_off")
    if row is None:
        return None
    if row.status in TERMINAL_JOBS:
        return {"status": row.status, "job_id": str(job_id), **(row.results or {})}
    return {"status": "running" if held or job_id in _background else "interrupted", "job_id": str(job_id)}


async def stored_question_state(session, question_id: uuid.UUID, *, company_id: uuid.UUID, user_id: uuid.UUID,
                                conversation_id: uuid.UUID) -> dict | None:
    """A thread question's state as stored: ``answered`` or ``failed`` with its
    assistant message once its job ended, else ``interrupted`` (no task holds it: a
    restart, or a runner that kept failing, ended it). Read after the registry (``is_held``): a job commits
    its message before it leaves the registry, so a held question is never read as
    interrupted. None when the conversation has no such question job."""
    from celerp.ai.conversations import message_failure
    from celerp.models.ai import AIMessage
    row = await owned_job(session, question_id, company_id=company_id, user_id=user_id, shape="conversation",
                          conversation_id=conversation_id)
    if row is None:
        return None
    out = {"state": "interrupted", "promoted": row.promoted_at is not None, "message": None}
    if row.status in TERMINAL_JOBS:
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
