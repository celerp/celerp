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
final. Every reader asks that rule, or its query forms ``runs_now`` and ``is_open``,
and no code but this module reads or writes a job's rows. Each claim of a lease writes
its own runner id, and a runner's renewals and final write are fenced on it: a runner
whose lease lapsed and was taken (by Stop, or a resume) writes nothing more. Stop ends
an unheld job at once, as stopped; Continue or asking again resumes it. Deleting a
conversation ends its open jobs as stopped, and is refused while one runs; a message an
open job answers is never pruned. A runner that raises is never given up on while its
process lives: it runs again after a growing delay and keeps its lease meanwhile, so
the job reads as running and Stop ends it.

Every transaction that writes a company's AI rows takes the company lock first
(``lock_for_ai``), before any job-row lock, as every other write of the company does:
a reset, a permission change or a module switch waits for it, or it waits for them
and then sees their outcome. A company gone meanwhile stops the write. A claim and a
fenced write refuse to run without it. The one exception is a runner's lease renewal
(``_heartbeat``), which writes only its own lease, conditionally on its runner id.

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
before every step. A question run again replays a read it kept only while its asker
may still make that read (AI access and every permission the read needs); otherwise it
ends refused, and only a new question follows. A job's id names one request
(``JobRequest``): reusing the id for anything else is refused as ``operation_conflict``.
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
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update

from celerp.ai import deadlines
from celerp.db import SessionLocal
from celerp.services.company_lock import lock_company, noted
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
# The runner lease: this process's id (the prefix of the runner id every claim it
# makes writes), how long a claim holds, and how often a running job renews it (well
# inside the lease, so a slow renewal never lapses it).
RUNNER_ID = uuid.uuid4().hex[:16]
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
# journaled.
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
    return "one_off" if job.question_id is None else "conversation"


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


def outcome(job) -> str | None:
    """How a job ended (``completed``, ``failed`` or ``cancelled``), or None while it is open."""
    return job.status if liveness(job) == "ended" else None


def answered(step) -> bool:
    """Whether a step ended with its answer."""
    return step.state == "done"


def is_open():
    """The query form of ``liveness(job) != "ended"``."""
    from celerp.models.ai import AIBatchJob
    return AIBatchJob.status.not_in(TERMINAL_JOBS)


def reopenable():
    """The query form of: a job its user may reopen. A question until it is answered,
    a reading job whatever its status (its steps say what is left to send), and
    neither once the conversation it was asked in is gone."""
    from celerp.models.ai import AIBatchJob
    return and_(or_(AIBatchJob.kind == "documents", AIBatchJob.status != "completed"),
                or_(AIBatchJob.question_id.is_(None), AIBatchJob.conversation_id.is_not(None)))


def runs_now():
    """The query form of ``liveness(job) == "running"``."""
    from celerp.models.ai import AIBatchJob
    return and_(is_open(), AIBatchJob.lease_until > _now())


def _lease_end() -> datetime:
    return _now() + timedelta(seconds=LEASE_S)


def _runner_id() -> str:
    """The runner id one claim writes: this process's id, then the claim's own."""
    return RUNNER_ID + uuid.uuid4().hex[:16]


def hold(job) -> None:
    """Give this process the lease on a job row it writes."""
    job.runner_id, job.lease_until = _runner_id(), _lease_end()


def release(job) -> None:
    job.runner_id = job.lease_until = None


async def _claim(session, job_id: uuid.UUID, where=None, *, own: bool = False, **values) -> str | None:
    """Take the lease on a job nobody runs (with ``own``, also one a claim of this
    process holds: only the job's task asks that), with ``values`` in the same update,
    when the job matches ``where`` (by default: open). Returns the runner id the claim
    wrote, which its holder's renewals and final write are fenced on; None, and nothing
    written, otherwise: a stop or a reopen never takes a job a runner holds, this
    process's included. Only under the job's company lock."""
    from celerp.models.ai import AIBatchJob
    free = or_(AIBatchJob.lease_until.is_(None), AIBatchJob.lease_until <= _now(),
               *([AIBatchJob.runner_id.startswith(RUNNER_ID, autoescape=True)] if own else []))
    runner_id = _runner_id()
    claimed = await session.execute(update(AIBatchJob).where(AIBatchJob.id == job_id, free, _under_lock(session),
                                                             is_open() if where is None else where)
                                    .values(runner_id=runner_id, lease_until=_lease_end(), **values))
    return runner_id if claimed.rowcount == 1 else None


async def _fenced(session, job_id: uuid.UUID, runner_id: str):
    """The job's row, locked for its final write, while the claim ``runner_id`` still
    holds it open; None, and the write refused, once another claim took it or it ended.
    Only under the job's company lock."""
    from celerp.models.ai import AIBatchJob
    job = (await session.execute(select(AIBatchJob).where(AIBatchJob.id == job_id, _under_lock(session))
                                 .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if job is None or job.runner_id != runner_id or liveness(job) == "ended":
        log.warning("AI job %s: a runner whose claim was taken or whose job ended wrote; its write was refused", job_id)
        return None
    return job


class ClaimTaken(Exception):
    """Another claim took the job, or it ended: the runner that held it writes nothing
    more, sends nothing more, and stops."""


class CompanyGone(ClaimTaken, HTTPException):
    """The job's company is gone: nothing more is written for it. A request that waited
    for the lock of a company reset meanwhile is answered not found."""

    def __init__(self) -> None:
        HTTPException.__init__(self, status_code=404, detail=t("error.company_unavailable"))


async def lock_for_ai(session, company_id: uuid.UUID) -> None:
    """The company lock, taken once at the start of every transaction that writes the
    AI's rows of a company (its jobs, steps, conversations, messages and usage), before
    any job-row lock. Raises CompanyGone, and nothing is written, when the company is
    gone. A session outside a request carries no request authority for the lock to judge
    again, so a job's runner asks ``_refusal`` for its asker's access."""
    if not await lock_company(session, company_id):
        raise CompanyGone()


async def _lock_job(session, job_id: uuid.UUID):
    """Take the company lock of a job, then read the job as committed under it; None
    when the job is gone."""
    from celerp.models.ai import AIBatchJob
    company_id = await session.scalar(select(AIBatchJob.company_id).where(AIBatchJob.id == job_id))
    if company_id is None:
        return None
    await lock_for_ai(session, company_id)
    return await session.get(AIBatchJob, job_id, populate_existing=True)


def _under_lock(session):
    """The condition a claim or a fenced write puts on a job row: its company is one
    whose lock this transaction holds. With none held, the write is refused outright."""
    from celerp.models.ai import AIBatchJob
    held = noted(session, "locked")
    if not held:
        raise RuntimeError("an AI job row written without its company lock (lock_for_ai)")
    return AIBatchJob.company_id.in_([uuid.UUID(str(c)) for c in held])


async def _hold(session, claim) -> None:
    """Lock ``claim``'s job (its ``job_id``) for a write while ``claim.runner_id`` still
    holds it open; raise ClaimTaken otherwise. Every step write of a runner goes through it."""
    if await _fenced(session, claim.job_id, claim.runner_id) is None:
        raise ClaimTaken()


# ── The job rows, as every caller outside this module reads and writes them ──

async def conversation_jobs(session, conversation_id: uuid.UUID, *, company_id: uuid.UUID, user_id: uuid.UUID,
                            shape: str) -> list:
    """This user's jobs of ``shape`` started in a conversation, oldest first."""
    from celerp.models.ai import AIBatchJob
    rows = (await session.execute(
        select(AIBatchJob).where(AIBatchJob.conversation_id == conversation_id, AIBatchJob.company_id == company_id,
                                 AIBatchJob.user_id == user_id).order_by(AIBatchJob.created_at.asc())
    )).scalars().all()
    return [job for job in rows if job_shape(job) == shape]


async def open_jobs(session, conversation_ids) -> list:
    """The open jobs of the given conversations, of every kind."""
    from celerp.models.ai import AIBatchJob
    ids = list(conversation_ids)
    if not ids:
        return []
    return list((await session.execute(
        select(AIBatchJob).where(AIBatchJob.conversation_id.in_(ids), is_open()))).scalars().all())


async def company_runs_now(session, company_id: uuid.UUID) -> bool:
    """Whether a runner works on any job of the company."""
    from celerp.models.ai import AIBatchJob
    return await session.scalar(
        select(AIBatchJob.id).where(AIBatchJob.company_id == company_id, runs_now()).limit(1)) is not None


def documents_job(*, request: JobRequest, file_ids: list[str], job_id: uuid.UUID | None = None) -> tuple:
    """A new reading job for ``request``, held by this process, and one step per file
    of ``file_ids``; the caller adds them to its session, commits, then starts it. A job
    started in a thread takes the id of the question that asked for it, which it
    answers."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    job_id = job_id or uuid.uuid4()
    job = AIBatchJob(id=job_id, company_id=request.company_id, user_id=request.user_id,
                     conversation_id=request.conversation_id,
                     question_id=job_id if request.conversation_id else None, kind="documents", mode="background",
                     status="pending", query=request.query, file_ids=list(file_ids),
                     request_digest=request.digest())
    hold(job)
    steps = []
    for seq, file_id in enumerate(file_ids):
        op = file_operation_id(job_id, file_id, 0)
        steps.append(AIJobStep(job_id=job_id, seq=seq, key=file_id, operation_id=op, step_id=op))
    return job, steps


async def _end_unheld(session, *where) -> bool:
    """End, in the caller's transaction, every open job matching ``where`` as stopped:
    a reading job cancelled, a question failed. False, and nothing written, while a
    runner holds one of them. Only under the jobs' company lock."""
    from celerp.models.ai import AIBatchJob
    rows = (await session.execute(select(AIBatchJob).where(is_open(), _under_lock(session), *where).with_for_update()
                                  .execution_options(populate_existing=True))).scalars().all()
    if any(liveness(job) == "running" for job in rows):
        return False
    for job in rows:
        job.status = "cancelled" if job.kind == "documents" else "failed"
        job.cancel_requested_at = None
        job.completed_at = _now()
        release(job)
    return True


async def end_conversation_jobs(session, conversation_id: uuid.UUID, *, company_id: uuid.UUID,
                                user_id: uuid.UUID) -> bool:
    """Before a conversation is deleted in the same transaction: end its open jobs as
    stopped, so none outlives it open to be resumed or charged. False, and nothing
    written, while a runner works in it."""
    from celerp.models.ai import AIBatchJob
    return await _end_unheld(session, AIBatchJob.conversation_id == conversation_id,
                             AIBatchJob.company_id == company_id, AIBatchJob.user_id == user_id)


async def end_question(session, job_id: uuid.UUID) -> bool:
    """End a question whose own message is gone, as stopped, unless a runner holds it
    (its run ends it: ``_finish_question``)."""
    from celerp.models.ai import AIBatchJob
    return await _end_unheld(session, AIBatchJob.id == job_id)


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
    so the thread offers Stop, and a Stop ends the job at the next failure.

    A claim this process took for the job while this task lives (a reopen: the user
    asked again) is always run by this task: whichever way a run ends, a reopen it
    was told of (``_again``) is claimed again and run. The task leaves ``_tasks`` in
    the same step that it decides to end, so a later reopen starts a new task."""
    stop = asyncio.Event()
    claim: list[str | None] = [None]
    beat = asyncio.create_task(_heartbeat(job_id, claim, stop))
    failures = 0
    try:
        while True:
            try:
                async with SessionLocal() as session:
                    claim[0] = None if await _lock_job(session, job_id) is None else \
                        await _claim(session, job_id, own=True)
                    await session.commit()
            except CompanyGone:
                claim[0] = None
            if claim[0] is None:
                break       # ended, or another process runs it
            try:
                await _run(job_id, claim[0])
                failures = 0
            except ClaimTaken:
                pass        # a stop or a reopen took the claim: only a reopen runs again
            except Exception:
                failures += 1
                log.exception("AI job %s stopped (%d in a row); running it again", job_id, failures)
                await _after_failure(job_id, claim[0])
                await asyncio.sleep(min(STEP_RETRY_DELAY_S * 2 ** failures, RUNNER_RESTART_MAX_DELAY_S))
                continue
            if job_id not in _again:
                break
            _again.discard(job_id)
    finally:
        # No await between the decision to end and leaving ``_tasks``: a reopen either
        # reached this task or finds none alive and starts another.
        _again.discard(job_id)
        if _tasks.get(job_id) is asyncio.current_task():
            del _tasks[job_id]
        # Never cancelled mid-query: the renewal in flight ends, then the heartbeat does.
        stop.set()
        await asyncio.gather(beat, return_exceptions=True)


async def _heartbeat(job_id: uuid.UUID, claim: list[str | None], stop: asyncio.Event) -> None:
    """Renew the task's claim on the job (``claim[0]``, its runner id) every
    LEASE_RENEW_S until ``stop``. A renewal that fails is tried again at the next beat;
    a claim another took is never renewed.

    The one write of a job row made without its company lock: it sets the lease end
    only, of the row its own claim holds, under that row's lock. A renewal that waited
    for the company lock would wait behind any long holder of it (an import, a
    migration batch) past the lease, and a healthy runner's job would read as nobody's,
    for a Stop or another process to take."""
    from celerp.models.ai import AIBatchJob
    while True:
        try:
            await asyncio.wait_for(stop.wait(), LEASE_RENEW_S)
            return
        except TimeoutError:
            pass
        try:
            async with SessionLocal() as session:
                held = (AIBatchJob.id == job_id, AIBatchJob.runner_id == claim[0], is_open())
                if await session.scalar(select(AIBatchJob.id).where(*held).with_for_update()) is not None:
                    await session.execute(update(AIBatchJob).where(*held).values(lease_until=_lease_end()))
                await session.commit()
        except Exception:
            log.warning("AI job %s: its lease was not renewed", job_id, exc_info=True)


async def _after_failure(job_id: uuid.UUID, runner_id: str) -> None:
    """After a run raised: a job its user stopped ends as stopped; a question moves to
    the background, where the thread offers Stop and a restart resumes it. Best effort:
    the next run tries again."""
    from celerp.models.ai import AIBatchJob
    try:
        async with SessionLocal() as session:
            job = await session.get(AIBatchJob, job_id)
            stopped, question = job.cancel_requested_at is not None, job.kind == "question"
        if stopped:
            await _end_stopped(job_id, runner_id)
        elif question:
            await promote(job_id)
    except Exception:
        log.warning("AI job %s: its failure could not be recorded", job_id, exc_info=True)


async def _run(job_id: uuid.UUID, runner_id: str) -> None:
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        kind = (await session.execute(select(AIBatchJob.kind).where(AIBatchJob.id == job_id))).scalar_one_or_none()
    await (_run_question if kind == "question" else _run_job)(job_id, runner_id)


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


class Refused(Exception):
    """A step's asker may no longer send it: it leaves unsent. ``key`` is the job's
    error key (``_refusal``)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def _admitted(session, claim) -> None:
    """Take the company lock of ``claim``'s job and admit its asker again under it, in
    the transaction that records a step's send. A permission change commits under the
    same lock, so one committed first leaves the step unsent (Refused) and one committed
    after it finds the step already sent."""
    await lock_for_ai(session, claim.company_id)
    if (refusal := await _refusal(session, claim.company_id, claim.user_id)) is not None:
        raise Refused(refusal)


async def recover_on_start() -> int:
    """Run once at start-up, before the usage reconcile loop. This process is the one
    that runs AI jobs, so a lease no task of it holds belongs to a process that is
    gone: it is let go. Every open background job then resumes under this process's
    lease: its open steps are registered as held before this returns, so no reconcile
    pass asks the service about a step this process is about to send, and then its
    task starts. A job whose user may no longer use the assistant ends failed with
    that reason. An interactive question is left unheld, which reads as interrupted:
    it is never sent again but by the user asking again under its id. Returns how
    many jobs resumed. Each company's jobs are taken in a transaction of their own,
    under its company lock; a company gone meanwhile has no jobs left to resume."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    resumed: list[uuid.UUID] = []
    async with SessionLocal() as session:
        companies = (await session.execute(select(AIBatchJob.company_id).where(is_open()).distinct())).scalars().all()
        await session.commit()
    for company_id in sorted(companies, key=str):
        async with SessionLocal() as session:
            try:
                await lock_for_ai(session, company_id)
            except CompanyGone:
                continue
            resumed += await _recover_company(session, company_id)
            await session.commit()
    for job_id in resumed:
        start_job(job_id)
    return len(resumed)


async def _recover_company(session, company_id: uuid.UUID) -> list[uuid.UUID]:
    """``recover_on_start`` for one company's open jobs, in the caller's transaction
    under its company lock: the ids of the jobs to start."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    resumed: list[uuid.UUID] = []
    rows = (await session.execute(select(AIBatchJob).where(is_open(), AIBatchJob.company_id == company_id)
                                  .execution_options(populate_existing=True))).scalars().all()
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
    return resumed


async def _reopen(job_id: uuid.UUID, company_id: uuid.UUID, user_id: uuid.UUID, *, retry: bool) -> int | None:
    """Mark a reading job running again and start it; with ``retry``, every step that
    ended without an answer first moves to its next generation (a new file identity).
    Returns the number of steps reopened, or None when the job is not this user's
    reading job or still runs. A question is never reopened here: it continues only by
    being asked again under its id (``reopen_question``)."""
    from celerp.models.ai import AIJobStep
    async with SessionLocal() as session:
        await lock_for_ai(session, company_id)
        job = await owned_job(session, job_id, company_id=company_id, user_id=user_id, shape="documents")
        if job is None or liveness(job) == "running":
            return None     # not this user's reading job, or run by a runner
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
        if not await _claim(session, job_id, reopenable(), status="running", error=None, completed_at=None,
                            cancel_requested_at=None):
            await session.rollback()        # another send took it first, or its conversation is gone
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
        await lock_for_ai(session, company_id)
        job = await owned_job(session, job_id, company_id=company_id, user_id=user_id)
        if job is None or liveness(job) == "ended":
            return False
        job.cancel_requested_at = _now()
        runner_id = await _claim(session, job_id) if liveness(job) == "unheld" else None
        await session.commit()
    if runner_id is not None:
        await _end_stopped(job_id, runner_id)
    return True


async def _end_stopped(job_id: uuid.UUID, runner_id: str) -> None:
    """End a job its user stopped, from its rows, as its runner would have, under the
    claim ``runner_id``: a reading job with steps left is cancelled (Continue sends
    them), a question ends failed as stopped (asking again continues it)."""
    from celerp.ai import service
    from celerp.models.ai import AIBatchJob
    async with SessionLocal() as session:
        job = await session.get(AIBatchJob, job_id)
        run = _file_run(job, runner_id) if job.kind == "documents" else None
    if run is None:
        await _finish_question(job_id, service.failure(Stopped(), service.RunTally()), None, runner_id)
    else:
        await _end_job(run)


def _file_run(job, runner_id: str):
    from celerp.ai import batch
    return batch.FileRun(job_id=job.id, company_id=job.company_id, user_id=job.user_id,
                         conversation_id=job.conversation_id, query=job.query, runner_id=runner_id)


# ── The runner ────────────────────────────────────────────────────────────────

async def _run_job(job_id: uuid.UUID, runner_id: str) -> None:
    from celerp.models.ai import AIJobStep
    async with SessionLocal() as session:
        job = await _lock_job(session, job_id)
        if job is None or liveness(job) == "ended":
            return
        rows = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id, AIJobStep.state.in_(OPEN_STEPS))
            .order_by(AIJobStep.seq)
        )).scalars().all()
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        await session.commit()
        run = _file_run(job, runner_id)
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
    try:
        for attempt in range(1, STEP_ATTEMPTS + 1):
            try:
                await _document_step(run, step_pk, file_id, operation_id)
                return
            except ClaimTaken:
                raise
            except Exception:
                log.exception("Step %s of AI job %s failed (attempt %d)", file_id, run.job_id, attempt)
                if attempt < STEP_ATTEMPTS:
                    await asyncio.sleep(STEP_RETRY_DELAY_S * attempt)
        async with SessionLocal() as session:
            await lock_for_ai(session, run.company_id)
            await _set_step(session, run, step_pk, state="failed",
                            result={"filename": file_id, "error": t("ai.err_file_unexpected")})
            await session.commit()
    except ClaimTaken:
        return      # the claim that runs the job now owns the step
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
            await _admitted(session, run)
            await _set_step(session, run, step_pk, state="running")
            await usage.record_usage(session, company_id=run.company_id, user_id=run.user_id,
                                     operation_id=operation_id, send_bytes=nbytes)
            await session.commit()

    async with gate.slot("documents"):
        if run.stop_reason is not None or await _cancel_requested(run.job_id):
            return
        async with SessionLocal() as session:
            refusal = await _refusal(session, run.company_id, run.user_id)
            await session.commit()
        if refusal is None:
            try:
                outcome = await batch.read_file(run, file_id, operation_id, before_send=_before_send)
            except Refused as exc:
                refusal = exc.key
        if refusal is not None:
            # Unsent steps stay pending and uncharged; finished ones keep their results.
            run.stop_reason = refusal
            return
    async with SessionLocal() as session:
        await lock_for_ai(session, run.company_id)
        await _set_step(session, run, step_pk, state=outcome.state, result=outcome.result,
                        credits=outcome.credits, charge_known=outcome.known)
        if outcome.sent:
            await usage.record_usage(session, company_id=run.company_id, user_id=run.user_id,
                                     operation_id=operation_id, credits=outcome.credits, known=outcome.known)
        await session.commit()
    _held_operations.discard(operation_id)
    await batch.publish_progress(run, file_id, outcome.state)


async def _set_step(session, claim, step_pk: uuid.UUID, *, credits: int | None = None, **values) -> None:
    """Write a step of ``claim``'s job, fenced on the claim (``_hold``)."""
    from celerp.models.ai import AIJobStep
    await _hold(session, claim)
    if credits is not None:
        # A retried file keeps what its earlier generations were charged.
        values["credits"] = AIJobStep.credits + credits
    await session.execute(update(AIJobStep).where(AIJobStep.id == step_pk).values(
        updated_at=datetime.now(timezone.utc), **values))


async def _end_job(run) -> None:
    from celerp.ai import batch
    from celerp.models.ai import AIJobStep
    async with SessionLocal() as session:
        await lock_for_ai(session, run.company_id)
        job = await _fenced(session, run.job_id, run.runner_id)
        if job is None:
            return
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
            await lock_for_ai(session, company_id)
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

    def stored_reads(self) -> dict[str, str | None]:
        """The tool each kept read was made with, by call id (``kept_reads``)."""
        return kept_reads(self._done)

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


def kept_reads(done: dict[str, dict]) -> dict[str, str | None]:
    """The tool each read in a question's ended steps (``done``, by key) was made
    with, by call id, as the kept model step that asked for it names it (None when
    none does)."""
    names: dict[str, str] = {}
    turn = 0
    while (model := done.get(f"model:{turn}")) is not None:
        for call in (model.get("message") or {}).get("tool_calls") or []:
            names[call.get("id") or ""] = (call.get("function") or {}).get("name")
        turn += 1
    return {key.removeprefix("tool:"): names.get(key.removeprefix("tool:"))
            for key in done if key.startswith("tool:")}


@dataclasses.dataclass(frozen=True)
class Earlier:
    """One earlier message of a question's thread. For an assistant answer, ``reads``
    holds what its question's journal kept of each read it made, as (tool name, kept
    step); None when no journal tells (``message`` is None for an answer the thread
    no longer holds, whose question's journal is kept)."""
    message: object | None
    reads: list[tuple[str | None, dict]] | None = None

    @property
    def answer(self) -> bool:
        return self.message is None or self.message.role == "assistant"


class Stopped(Exception):
    """The user stopped the question: it sends no further model step. Asking it again
    under its id continues it from the steps that ended."""


class _JobJournal(Journal):
    def __init__(self, job_id: uuid.UUID, runner_id: str, company_id: uuid.UUID, user_id: uuid.UUID,
                 steps: list) -> None:
        super().__init__(job_id)
        self.job_id, self.runner_id, self.company_id, self.user_id = job_id, runner_id, company_id, user_id
        self._pk: dict[str, uuid.UUID] = {}
        self._seq = max((step.seq for step in steps), default=-1) + 1
        self._promoter: asyncio.Task | None = None
        for step in steps:
            self._pk[step.key], self._generation[step.key] = step.id, step.generation
            if answered(step):
                self._done[step.key] = step.result

    @property
    def background(self) -> bool:
        return self.job_id in _background

    @staticmethod
    def _refused(refusal: str):
        from fastapi import HTTPException
        return HTTPException(status_code=403, detail={"code": refusal.removeprefix("ai."), "message": t(refusal)})

    async def admit(self) -> None:
        from celerp.models.ai import AIBatchJob
        async with SessionLocal() as session:
            refusal = await _refusal(session, self.company_id, self.user_id)
            stopped = (await session.execute(
                select(AIBatchJob.cancel_requested_at).where(AIBatchJob.id == self.job_id))).scalar_one_or_none()
            await session.commit()
        if stopped is not None:
            raise Stopped()
        if refusal is not None:
            raise self._refused(refusal)

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
            await _set_step(session, self, self._pk[key], state=state)
            return self._pk[key]
        await _hold(session, self)
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
            try:
                await _admitted(session, self)
            except Refused as exc:
                raise self._refused(exc.key) from None
            await self._step(session, key, "running", identity)
            await usage.record_usage(session, company_id=self.company_id, user_id=self.user_id,
                                     operation_id=self.question_id, send_bytes=nbytes)
            await session.commit()

    async def ended(self, key: str, state: str, result: dict | None = None) -> None:
        await super().ended(key, state, result)
        if state != "done" and key not in self._pk:
            return      # never sent: nothing to record
        async with SessionLocal() as session:
            await lock_for_ai(session, self.company_id)
            pk = await self._step(session, key, state)
            await _set_step(session, self, pk, state=state, result=result, credits=(result or {}).get("credits") or None)
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
        if await _lock_job(session, job_id) is None:
            return
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
    one that runs, or one whose conversation is gone, is never reopened; False then."""
    return await _claim(session, job_id, reopenable(),
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


async def _question_context(session, job) -> dict | None:
    """Every argument of the question's agent run but its journal, from the job's rows
    and the app's current authorization: the one source for a run started by a request
    and for one resumed after a restart. None when the question's own message is gone."""
    from celerp.ai.conversations import get_messages
    from celerp.ai.memory import get_memory
    from celerp.main import app
    from celerp.models.ai import AIMessage
    from celerp.services.permissions import read_authority
    role, company_settings = await read_authority(session, job.company_id, job.user_id)
    history: list[Earlier] = []
    if job.conversation_id is not None:
        asked = await session.get(AIMessage, job.question_id)
        if asked is None:
            return None
        prior = await get_messages(session, job.conversation_id)
        history = await _earlier(session, job, asked, [m for m in prior if m.created_at < asked.created_at])
    return dict(app=app, bearer=_bearer(job, role or ""), operation_id=job.id, query=job.query,
                company_id=job.company_id, user_id=job.user_id, company_settings=company_settings,
                role=role or "", memory=await get_memory(session, job.company_id),
                file_ids=job.file_ids or None, history=history, read_only=job.conversation_id is None)


async def _earlier(session, job, asked, messages: list) -> list[Earlier]:
    """The thread's ``messages`` before ``asked`` as ``Earlier`` entries, in time order:
    each assistant answer with the reads its question's journal kept, and each earlier
    question of the thread whose answer the thread no longer holds, as an answer at the
    time it was asked. A failed run's message holds only why it failed, so it read nothing."""
    from celerp.ai.conversations import message_failure
    from celerp.models.ai import AIBatchJob
    rows = (await session.execute(select(AIBatchJob.id, AIBatchJob.results, AIBatchJob.created_at).where(
        AIBatchJob.conversation_id == job.conversation_id, AIBatchJob.kind == "question",
        AIBatchJob.id != job.id, AIBatchJob.created_at < asked.created_at))).all()
    answered_by = {str(results["message_id"]): (job_id, created_at) for job_id, results, created_at in rows
                   if (results or {}).get("message_id")}
    steps = await job_steps(session, [job_id for job_id, _ in answered_by.values()])

    def reads(job_id) -> list[tuple[str | None, dict]]:
        done = {step.key: step.result for step in steps[job_id] if answered(step)}
        return [(name, done[f"tool:{call_id}"]) for call_id, name in kept_reads(done).items()]

    def entry(msg) -> Earlier:
        if msg.role != "assistant":
            return Earlier(msg)
        if message_failure(msg.tools_called) is not None:
            return Earlier(msg, [])
        job = answered_by.get(str(msg.id))
        return Earlier(msg, None if job is None else reads(job[0]))
    held = {str(msg.id) for msg in messages}
    gone = [(created_at, Earlier(None, reads(job_id))) for message_id, (job_id, created_at) in answered_by.items()
            if message_id not in held]
    timed = [(msg.created_at, entry(msg)) for msg in messages] + gone
    return [earlier for _, earlier in sorted(timed, key=lambda pair: pair[0])]


async def _run_question(job_id: uuid.UUID, runner_id: str) -> None:
    from celerp.ai import service
    from celerp.models.ai import AIBatchJob, AIJobStep
    async with SessionLocal() as session:
        job = await _lock_job(session, job_id)
        if job is None or liveness(job) == "ended":
            return
        run = await _question_context(session, job)
        if run is None:
            await session.commit()
            await _finish_question(job_id, service.failure(Stopped(), service.RunTally()), None, runner_id)
            return
        steps = (await session.execute(
            select(AIJobStep).where(AIJobStep.job_id == job_id).order_by(AIJobStep.seq))).scalars().all()
        if job.mode == "background":
            _background.add(job_id)
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        journal = _JobJournal(job_id, runner_id, job.company_id, job.user_id, steps)
        await session.commit()
    _held_operations.add(job_id)
    try:
        refusal = None
        try:
            result = await service.run_agent(journal=journal, **run)
        except service.AgentRefusal as exc:
            result, refusal = None, exc
        await _finish_question(job_id, result, refusal, runner_id)
    finally:
        journal.close()
        _held_operations.discard(job_id)


async def _finish_question(job_id: uuid.UUID, result, refusal, runner_id: str) -> None:
    """Store how the question ended, under the claim ``runner_id``, in the same commit
    that ends its lease: in its thread, the answer, the failure or the refusal as the
    assistant message; for a question outside a thread, on the job. A thread question
    whose own message is gone ends failed with nothing stored but its charge. Then a
    promoted question notifies."""
    from dataclasses import asdict

    from celerp.ai import usage
    from celerp.ai.conversations import add_message, failure_record, help_record, record_credits
    from celerp.models.ai import AIMessage
    ended = refusal or result
    async with SessionLocal() as session:
        if await _lock_job(session, job_id) is None:
            return
        job = await _fenced(session, job_id, runner_id)
        if job is None:
            return
        asked = None if job.conversation_id is None else await session.get(AIMessage, job.question_id)
        if asked is None:
            await usage.record_usage(session, company_id=job.company_id, user_id=job.user_id, operation_id=job_id,
                                     credits=ended.credits, known=ended.charge_known)
        if asked is not None:
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
        elif job.question_id is None:
            if refusal is not None:
                job.results = {"refusal": {"status": refusal.status_code, "detail": refusal.detail,
                                           "headers": dict(refusal.headers or {})}}
            else:
                job.results = {"answer": result.answer, "model_used": result.model_used,
                               "tools_called": result.tools_called, "help_pages": result.help_pages,
                               "error": result.error, "error_code": result.error_code}
        failed = refusal is not None or bool(result.error) or (job.question_id is not None and asked is None)
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
        await lock_for_ai(session, company_id)
        job = await reused_job(session, job_id, request)
        started_here = False
        if job is None:
            session.add(question_job(job_id=job_id, request=request, auth_nonce=auth_nonce))
            try:
                await session.commit()
                started_here = True
            except IntegrityError:
                await session.rollback()
                await lock_for_ai(session, company_id)
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
    message once its job ended, with ``asked``, the question's own message. None when
    the conversation has no such question job, or its messages are gone."""
    from celerp.ai.conversations import message_failure
    from celerp.models.ai import AIMessage
    row = await owned_job(session, question_id, company_id=company_id, user_id=user_id, shape="conversation",
                          conversation_id=conversation_id)
    asked = None if row is None else await session.get(AIMessage, question_id)
    if asked is None:
        return None
    state = liveness(row)
    out = {"state": "interrupted" if state == "unheld" else "running", "promoted": row.promoted_at is not None,
           "asked": asked, "message": None}
    if state == "ended":
        msg = await answer_message(session, row)
        if msg is None:
            return None
        out.update(state="failed" if message_failure(msg.tools_called) else "answered", message=msg)
    return out


async def answer_message(session, job):
    """The assistant message an ended thread question stored, or None when it is gone."""
    from celerp.models.ai import AIMessage
    message_id = (job.results or {}).get("message_id")
    return None if message_id is None else await session.get(AIMessage, uuid.UUID(message_id))


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
