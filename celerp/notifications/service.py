# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Notification service - CRUD + SSE publishing.

All notification operations go through this module. The SSE pub/sub
is in-process (asyncio.Queue per subscriber). No Redis required.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.notification import Notification
from celerp.notifications.sse import publish

log = logging.getLogger(__name__)

MAX_PER_COMPANY = 100


async def create(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    *,
    user_id: uuid.UUID | None = None,
    action_url: str | None = None,
    priority: str = "medium",
) -> Notification:
    """Create a notification, prune old ones, and publish to SSE subscribers."""
    notif = Notification(
        company_id=company_id,
        user_id=user_id,
        category=category,
        title=title,
        body=body,
        action_url=action_url,
        priority=priority,
    )
    session.add(notif)
    await session.flush()

    # Prune: keep only newest MAX_PER_COMPANY per company
    count_q = select(func.count()).select_from(Notification).where(
        Notification.company_id == company_id,
    )
    total = (await session.execute(count_q)).scalar() or 0
    if total > MAX_PER_COMPANY:
        # Find the ID threshold - delete everything below it
        cutoff_q = (
            select(Notification.id)
            .where(Notification.company_id == company_id)
            .order_by(Notification.created_at.desc())
            .offset(MAX_PER_COMPANY)
        )
        old_ids = list((await session.execute(cutoff_q)).scalars().all())
        if old_ids:
            await session.execute(
                delete(Notification).where(Notification.id.in_(old_ids))
            )

    # Publish to SSE (fire-and-forget, don't fail the DB transaction)
    try:
        await publish(
            company_id,
            user_id,
            {
                "type": "notification",
                "id": str(notif.id),
                "category": category,
                "title": title,
                "body": body,
                "action_url": action_url,
                "priority": priority,
            },
        )
    except Exception:
        log.warning("Failed to publish SSE notification", exc_info=True)

    return notif


async def get_unread_count(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Count unread notifications for a user (including company-wide ones)."""
    q = select(func.count()).select_from(Notification).where(
        Notification.company_id == company_id,
        Notification.read == False,  # noqa: E712
        (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
    )
    return (await session.execute(q)).scalar() or 0


async def list_notifications(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[Notification]:
    """List notifications for a user, newest first."""
    q = (
        select(Notification)
        .where(
            Notification.company_id == company_id,
            (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
        )
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(q)).scalars().all())


async def mark_read(
    session: AsyncSession,
    notification_id: uuid.UUID,
    company_id: uuid.UUID,
) -> bool:
    """Mark a single notification as read. Returns True if found and updated."""
    notif = await session.get(Notification, notification_id)
    if notif is None or notif.company_id != company_id:
        return False
    notif.read = True
    session.add(notif)
    return True


async def mark_all_read(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Mark all notifications as read for a user. Returns count updated."""
    from sqlalchemy import update

    stmt = (
        update(Notification)
        .where(
            Notification.company_id == company_id,
            Notification.read == False,  # noqa: E712
            (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
        )
        .values(read=True)
    )
    result = await session.execute(stmt)
    return result.rowcount
