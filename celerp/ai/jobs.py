# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI jobs: the one gate on model calls, and what a restart does to the jobs it finds.

Every model call runs inside a slot of ``gate`` (``AI_MAX_CONCURRENT`` slots, the one
concurrency setting). A slot is taken as ``interactive`` (a question someone is
waiting on) or ``documents`` (a document reading job): document steps never hold the
last slot while there is more than one, and a waiting question is granted the next
free slot ahead of every waiting document step, so a question never waits behind a
queue of files. ``llm.complete`` refuses to run outside a slot.
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

from sqlalchemy import select

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


def held_operation_ids() -> set[uuid.UUID]:
    return set(_held_operations)


async def recover_on_start(session_factory=None) -> int:
    """Run once at start-up, before the usage reconcile loop: a job left pending or
    running by the last process is marked failed so its owner is told to resend.
    Returns how many were marked."""
    from celerp.ai.batch import interrupted_error
    from celerp.models.ai import AIBatchJob
    if session_factory is None:
        from celerp.db import LifecycleSessionLocal as session_factory
    async with session_factory() as session:
        rows = (await session.execute(
            select(AIBatchJob).where(AIBatchJob.status.in_(("pending", "running")))
        )).scalars().all()
        now = datetime.now(timezone.utc)
        for job in rows:
            job.status = "failed"
            job.error = interrupted_error()
            job.completed_at = now
        await session.commit()
    return len(rows)
