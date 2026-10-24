# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI usage: the one record of what the AI service charged, read by the usage report.

One row per question asked, keyed by company, user and the question's operation id,
written before the question leaves (so a restart cannot lose it) and updated with
what came back. The service names a question's whole charge on every reply to its
first turn, a retry's included: that charge replaces what the row held and settles
it. A reply that names no charge (an older service) adds the credits it reported and
leaves the row pending. A pending row is settled from the operation's status by
``reconcile_usage``, so a lost reply is counted once, never twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.ai import llm
from celerp.models.ai import AIUsage

log = logging.getLogger(__name__)

# A question may still be with the service this long after it was asked.
RECONCILE_AFTER = timedelta(minutes=1)
# Pending rows read from the service per usage report.
RECONCILE_BATCH = 50


async def record_usage(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: uuid.UUID,
    credits: int = 0,
    known: bool = False,
) -> None:
    """Record what the service reported for one question in ``session``; the caller
    commits. ``known``: ``credits`` is the operation's whole charge, named by the
    service; otherwise ``credits`` were added by the replies this run received. With
    nothing reported it opens the question's row, pending."""
    total = credits if known else AIUsage.credits + credits
    await session.execute(
        pg_insert(AIUsage)
        .values(id=uuid.uuid4(), company_id=company_id, user_id=user_id, operation_id=operation_id,
                credits=credits, pending=not known, created_at=datetime.now(timezone.utc))
        .on_conflict_do_update(constraint="uq_ai_usage_operation",
                               set_={"credits": total, "pending": False if known else AIUsage.pending})
    )


async def record_usage_now(bind, **fields) -> None:
    """``record_usage`` in a transaction of its own on ``bind``, for callers whose
    session belongs to someone else's request."""
    async with AsyncSession(bind) as session:
        await record_usage(session, **fields)
        await session.commit()


async def reconcile_usage(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Settle the company's pending questions old enough to have finished from the
    service's status of each operation, then commit. A charged one takes its charge;
    one never charged, or unknown to the service, takes 0; one an older service keeps
    no status for keeps what its replies reported. A question still running stays
    pending, and so does every row when the service cannot be asked."""
    rows = (await session.execute(
        select(AIUsage)
        .where(AIUsage.company_id == company_id, AIUsage.pending,
               AIUsage.created_at < datetime.now(timezone.utc) - RECONCILE_AFTER)
        .order_by(AIUsage.created_at)
        .limit(RECONCILE_BATCH)
    )).scalars().all()
    for row in rows:
        try:
            status = await llm.operation_status(row.operation_id)
        except (llm.RelayError, httpx.HTTPError, ValueError) as exc:
            log.info("AI usage reconcile stopped: %s", type(exc).__name__)
            break
        if status is None:
            settled = {}
        elif status["state"] == "in_progress":
            continue
        else:
            settled = {"credits": status["credits"] if status["state"] == "charged" else 0}
        # Only while still pending: a reply that named the charge meanwhile stands.
        await session.execute(update(AIUsage).where(AIUsage.id == row.id, AIUsage.pending)
                              .values(pending=False, **settled))
    await session.commit()
