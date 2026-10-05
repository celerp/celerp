# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Notification service - CRUD + SSE publishing.

All notification operations go through this module. The SSE pub/sub
is in-process (asyncio.Queue per subscriber). No Redis required.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, delete, update
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
    i18n: dict | None = None,
) -> Notification:
    """Create a notification, prune old ones, and publish to SSE subscribers.

    ``i18n`` ({"title": key, "body": key, "params": {...}}) names the message keys the
    English ``title`` and ``body`` were written from."""
    notif = Notification(
        company_id=company_id,
        user_id=user_id,
        category=category,
        title=title,
        body=body,
        action_url=action_url,
        priority=priority,
        i18n=i18n,
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


async def notify_once(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    *,
    i18n: dict | None = None,
) -> bool:
    """A high-priority notice told to the company once: never again with the same title and
    body, read or not. ``i18n`` as for ``create``. Caller commits. Returns whether it was created."""
    already = (await session.execute(
        select(Notification.id)
        .where(
            Notification.company_id == company_id,
            Notification.category == category,
            Notification.title == title,
            Notification.body == body,
        )
        .limit(1)
    )).first()
    if already:
        return False
    await create(session, company_id, category, title, body, priority="high", i18n=i18n)
    return True


async def notify_every_company(
    session: AsyncSession,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
) -> int:
    """An instance-wide condition, told to every company as a high-priority notice.

    Deduped on the unread notice: at most one stands per company per title, so a
    condition that persists re-notifies only after the prior notice was read. A notice
    that still stands is brought up to date with this body, so it never describes an
    earlier cause. Caller commits. Returns the number of notifications created."""
    from celerp.models.company import Company

    created = 0
    for cid in (await session.execute(select(Company.id))).scalars().all():
        already = (await session.execute(
            select(Notification.id)
            .where(
                Notification.company_id == cid,
                Notification.category == category,
                Notification.title == title,
                Notification.read == False,  # noqa: E712
            )
            .limit(1)
        )).first()
        if already:
            await session.execute(update(Notification).where(Notification.id == already[0])
                                  .values(body=body, action_url=action_url))
            continue
        await create(session, cid, category, title, body, action_url=action_url, priority="high")
        created += 1
    return created


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
    unread_only: bool = False,
) -> list[Notification]:
    """List notifications for a user, newest first.

    unread_only powers the bell, which is an unread inbox: a read (dismissed)
    notification must not reappear on the next fetch, and the high-priority ones,
    which ask the user to act, come first.
    """
    order = [Notification.created_at.desc(), Notification.id.desc()]  # id tiebreaker → stable pagination
    if unread_only:
        order.insert(0, (Notification.priority == "high").desc())
    q = (
        select(Notification)
        .where(
            Notification.company_id == company_id,
            (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
        )
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        q = q.where(Notification.read == False)  # noqa: E712
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


async def mark_done(
    session: AsyncSession,
    company_id: uuid.UUID,
    action_url: str,
) -> int:
    """The action a notice asks for has been taken: every unread notice of the company
    linking to it is marked read, so the bell never asks for it again. Caller commits.
    Returns count updated."""
    result = await session.execute(
        update(Notification)
        .where(
            Notification.company_id == company_id,
            Notification.action_url == action_url,
            Notification.read == False,  # noqa: E712
        )
        .values(read=True)
    )
    return result.rowcount


async def mark_all_read(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Mark all notifications as read for a user. Returns count updated."""
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
