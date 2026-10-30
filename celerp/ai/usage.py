# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI usage: the one record of what the AI service charged, read by the usage report.

One row per question asked, keyed by company, user and the question's operation id,
written before every send under the id (so a restart cannot lose it) and updated with
what came back. The service names a question's whole charge on every reply to its
first turn, a retry's included: that charge replaces what the row held and settles
it. A reply that names no charge (an older service, or one that could not settle)
adds the credits it reported and leaves the row pending. Each send sets the row's
``reconcile_at`` to when the service has surely ended and settled it; after that a
pending row is settled from the operation's status by the background
``reconcile_usage`` pass, so a lost reply is counted once, never twice, and the
usage report never waits on the service. A pending row the service can no longer
confirm (past its retry window) is marked ``unresolved``: never asked about again,
never set to zero, shown apart from confirmed usage, until a reply names its charge.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import deadlines, jobs, llm
from celerp.models.ai import AIUsage

log = logging.getLogger(__name__)


async def record_usage(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: uuid.UUID,
    credits: int = 0,
    known: bool = False,
    send_bytes: int | None = None,
) -> None:
    """Record what the service reported for one question in ``session``; the caller
    commits. ``known``: ``credits`` is the operation's whole charge, named by the
    service; otherwise ``credits`` were added by the replies this run received.
    ``send_bytes``: the question is about to be sent with a body of that size, so the
    row is pending again until the service has ended and settled that send."""
    now = datetime.now(timezone.utc)
    values: dict = {"credits": credits if known else AIUsage.credits + credits}
    if known:
        values.update(pending=False, unresolved=False)
    if send_bytes is not None:
        delay = deadlines.reconcile_delay_s(await deadlines.limits(), send_bytes)
        values.update(pending=True, reconcile_at=now + timedelta(seconds=delay))
    await session.execute(
        pg_insert(AIUsage)
        .values(id=uuid.uuid4(), company_id=company_id, user_id=user_id, operation_id=operation_id,
                credits=credits, pending=values.get("pending", True),
                reconcile_at=values.get("reconcile_at", now), created_at=now)
        .on_conflict_do_update(constraint="uq_ai_usage_operation", set_=values)
    )


async def reconcile_usage(session: AsyncSession) -> None:
    """One pass: mark unresolved every pending row the service can no longer confirm,
    then settle up to RECONCILE_BATCH other pending rows whose reconcile time has
    passed, from ONE status read of their operations. A charged one takes its charge;
    one never charged takes 0. One still running, or with no terminal status (not yet
    known to the service, or past its fencing capacity), is asked again after
    RECONCILE_RETRY_S so the next due rows get their turn; every row stays as it was
    when the service cannot be asked or answers no status. A row sent again after the
    read has a new reconcile time and is left for a later pass; so is a row a job in
    this process still holds.

    The rows are read, and the service asked, with no transaction open; what came
    back is written one company at a time, each in a short transaction under its
    company lock, so a company whose lock is held (or that is gone) waits for a later
    pass without holding up any other."""
    now = datetime.now(timezone.utc)
    stale_before = deadlines.confirmable_since(await deadlines.limits(), now)
    held = jobs.held_operation_ids()
    stale = set((await session.execute(
        select(AIUsage.company_id).where(AIUsage.pending, ~AIUsage.unresolved, AIUsage.created_at < stale_before)
        .distinct())).scalars())
    rows = (await session.execute(
        select(AIUsage.id, AIUsage.company_id, AIUsage.operation_id, AIUsage.reconcile_at)
        .where(AIUsage.pending, ~AIUsage.unresolved, AIUsage.reconcile_at < now,
               AIUsage.created_at >= stale_before,
               *([AIUsage.operation_id.not_in(held)] if held else []))
        .order_by(AIUsage.reconcile_at)
        .limit(deadlines.RECONCILE_BATCH)
    )).all()
    await session.commit()
    by_id: dict = {}
    if rows:
        try:
            by_id = {s["operation_id"]: s for s in await llm.operations_status([r.operation_id for r in rows])}
        except (llm.RelayError, httpx.HTTPError, ValueError, TimeoutError) as exc:
            log.info("AI usage reconcile skipped: %s", type(exc).__name__)
            rows = []
    for company_id in sorted(stale | {r.company_id for r in rows}, key=str):
        try:
            await jobs.lock_for_ai(session, company_id)
            if company_id in stale:
                await session.execute(
                    update(AIUsage)
                    .where(AIUsage.company_id == company_id, AIUsage.pending, ~AIUsage.unresolved,
                           AIUsage.created_at < stale_before)
                    .values(unresolved=True))
            for row in (r for r in rows if r.company_id == company_id):
                status = by_id.get(str(row.operation_id))
                if status is None or status["state"] not in ("charged", "not_charged"):
                    settled = {"reconcile_at": now + timedelta(seconds=deadlines.RECONCILE_RETRY_S)}
                else:
                    settled = {"pending": False,
                               "credits": status["credits"] if status["state"] == "charged" else 0}
                await session.execute(
                    update(AIUsage)
                    .where(AIUsage.id == row.id, AIUsage.pending, ~AIUsage.unresolved,
                           AIUsage.reconcile_at == row.reconcile_at)
                    .values(**settled))
            await session.commit()
        except Exception:
            await session.rollback()
            log.warning("AI usage of company %s not reconciled this pass", company_id, exc_info=True)


async def reconcile_usage_loop(session_factory) -> None:
    """Run the reconcile pass every RECONCILE_INTERVAL_S for the life of the app."""
    while True:
        try:
            async with session_factory() as session:
                await reconcile_usage(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("AI usage reconcile pass failed", exc_info=True)
        await asyncio.sleep(deadlines.RECONCILE_INTERVAL_S)
