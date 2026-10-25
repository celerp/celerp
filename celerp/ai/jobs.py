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
reading job. A step's operation id is the identity the AI service answers it by, so a
job that resumes after a restart sends a step again only under the identity it was
sent under: the service replays a stored reply, or reports an outcome it cannot
replay, which ends the step without running it again. Sending a step under a new
identity is always the user's explicit retry, which says what it counts as. The steps
are the job's progress; nothing else counts files.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select, update

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

OPEN_STEPS = ("pending", "running")
# A step that ended without an answer, which the user may run again under a new identity.
RETRYABLE_STEPS = ("failed", "unknown", "expired")
TERMINAL_JOBS = ("completed", "failed", "cancelled")


def held_operation_ids() -> set[uuid.UUID]:
    return set(_held_operations)


def file_operation_id(job_id: uuid.UUID, file_id: str, generation: int = 0) -> uuid.UUID:
    """The operation id one file of a reading job is asked under: generation 0 on the
    first run, the next generation on each explicit retry."""
    return uuid.uuid5(job_id, file_id if generation == 0 else f"{file_id}:{generation}")


def is_held(job_id: uuid.UUID) -> bool:
    """Whether a task of this process is running the job."""
    task = _tasks.get(job_id)
    return task is not None and not task.done()


def start_job(job_id: uuid.UUID) -> None:
    """Run the job's open steps in a task of this process (no-op while one runs)."""
    if is_held(job_id):
        return
    task = asyncio.create_task(_run_job(job_id), name=f"ai-job-{job_id}")
    _tasks[job_id] = task

    def _forget(done: asyncio.Task) -> None:
        if _tasks.get(job_id) is done:
            del _tasks[job_id]
        if not done.cancelled() and done.exception() is not None:
            log.error("AI job %s stopped", job_id, exc_info=done.exception())
    task.add_done_callback(_forget)


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
    _held_operations.clear()


async def _may_use_ai(session, job) -> bool:
    from celerp.services.permissions import read_authority, role_has_permission
    role, settings = await read_authority(session, job.company_id, job.user_id)
    return bool(role) and role_has_permission(settings, role, "use_ai_assistant")


async def recover_on_start() -> int:
    """Run once at start-up, before the usage reconcile loop. Every open job resumes:
    its open steps are registered as held before this returns, so no reconcile pass
    asks the service about a step this process is about to send, and then its task
    starts. A job whose user may no longer use the assistant ends failed with that
    reason. Returns how many jobs resumed."""
    from celerp.models.ai import AIBatchJob, AIJobStep
    resumed: list[uuid.UUID] = []
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(AIBatchJob).where(AIBatchJob.status.in_(("pending", "running")))
        )).scalars().all()
        for job in rows:
            if not await _may_use_ai(session, job):
                job.status, job.error = "failed", t("ai.job_permission_lost")
                job.completed_at = datetime.now(timezone.utc)
                continue
            ops = (await session.execute(
                select(AIJobStep.operation_id).where(AIJobStep.job_id == job.id, AIJobStep.state.in_(OPEN_STEPS))
            )).scalars().all()
            _held_operations.update(op for op in ops if op is not None)
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
        job = await session.get(AIBatchJob, job_id)
        if job is None or (job.company_id, job.user_id) != (company_id, user_id) or is_held(job_id):
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
        await asyncio.gather(*(_document_step(run, *step) for step in steps))
    finally:
        _held_operations.difference_update(op for _, _, op in steps)
    await _end_job(run)


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
        if await _cancel_requested(run.job_id):
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
        if job.cancel_requested_at is not None and any(s in OPEN_STEPS for s in states):
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
