# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI usage: the one record of what the AI service charged, read by the usage report.

Every path that asks the AI service a question records the credits its response
reported here: an answer's ``usage.credits`` and the ``credits`` of a question whose
outcome is unknown. A retry of a charged question reports 0 and records nothing, so
adding the rows up never counts a question twice.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.ai import AIUsage


def record_usage(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: uuid.UUID,
    credits: int,
) -> None:
    """Add the charge to ``session``; the caller commits. Nothing when nothing was charged."""
    if credits > 0:
        session.add(AIUsage(
            company_id=company_id, user_id=user_id, operation_id=operation_id, credits=credits,
        ))


async def record_usage_now(
    bind,
    *,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    operation_id: uuid.UUID,
    credits: int,
) -> None:
    """Record the charge in a transaction of its own on ``bind``, for callers whose
    session belongs to someone else's request."""
    if credits <= 0:
        return
    async with AsyncSession(bind) as session:
        record_usage(session, company_id=company_id, user_id=user_id,
                     operation_id=operation_id, credits=credits)
        await session.commit()
