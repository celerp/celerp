# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Notification service - CRUD + SSE publishing.

All notification operations go through this module. The SSE pub/sub
is in-process (asyncio.Queue per subscriber). No Redis required.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.notification import Notification, NotificationRead
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


def _addressed_to(user_id: uuid.UUID):
    """A notice reaches a user when it is addressed to them or to the whole company."""
    return (Notification.user_id == user_id) | (Notification.user_id.is_(None))


def _read_by(user_id: uuid.UUID):
    """The user has a read receipt for the notice."""
    return exists().where(
        NotificationRead.notification_id == Notification.id,
        NotificationRead.user_id == user_id,
    )


async def has_standing(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
) -> bool:
    """A notice with this category and title that no user has dismissed yet. Notifiers
    dedupe on it: while it stands nothing new is created, and once someone has
    dismissed it a state that persists notifies again."""
    return (await session.execute(
        select(Notification.id).where(
            Notification.company_id == company_id,
            Notification.category == category,
            Notification.title == title,
            ~exists().where(NotificationRead.notification_id == Notification.id),
        ).limit(1)
    )).first() is not None


async def notify_every_company(
    session: AsyncSession,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    priority: str = "medium",
) -> int:
    """Create one company-wide notice per company, deduped on a standing notice with
    the same category and title (has_standing): a reboot while it stands creates
    nothing new, and a state that persists notifies again only after the notice was
    dismissed. Caller commits. Returns the number of notifications created."""
    from celerp.models.company import Company

    created = 0
    for cid in (await session.execute(select(Company.id))).scalars().all():
        if await has_standing(session, cid, category, title):
            continue
        await create(session, cid, category, title, body, action_url=action_url, priority=priority)
        created += 1
    return created


async def get_unread_count(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Count notices unread by a user (including company-wide ones)."""
    q = select(func.count()).select_from(Notification).where(
        Notification.company_id == company_id,
        _addressed_to(user_id),
        ~_read_by(user_id),
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
) -> list[tuple[Notification, bool]]:
    """List notices for a user, newest first, each with whether this user read it.

    unread_only powers the bell, which is an unread inbox: a notice the user read
    (dismissed) must not reappear on their next fetch.
    """
    read = _read_by(user_id)
    q = (
        select(Notification, read.label("read"))
        .where(
            Notification.company_id == company_id,
            _addressed_to(user_id),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())  # id tiebreaker → stable pagination
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        q = q.where(~read)
    return [(n, bool(r)) for n, r in (await session.execute(q)).all()]


async def mark_read(
    session: AsyncSession,
    notification_id: uuid.UUID,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Mark one notice read for this user. Returns False when no notice with this id
    reaches the user in this company. Marking it again changes nothing."""
    found = (await session.execute(
        select(Notification.id).where(
            Notification.id == notification_id,
            Notification.company_id == company_id,
            _addressed_to(user_id),
        )
    )).first()
    if found is None:
        return False
    try:
        async with session.begin_nested():
            await session.execute(
                pg_insert(NotificationRead)
                .values(notification_id=notification_id, user_id=user_id)
                .on_conflict_do_nothing()
            )
    except IntegrityError:
        # The notice was deleted after it was found.
        return False
    return True


async def mark_all_read(
    session: AsyncSession,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Mark every notice that reaches this user read, for this user only. Returns the
    number newly marked."""
    unread = select(Notification.id, literal(user_id, sa.Uuid())).where(
        Notification.company_id == company_id,
        _addressed_to(user_id),
        ~_read_by(user_id),
    )
    result = await session.execute(
        pg_insert(NotificationRead)
        .from_select(["notification_id", "user_id"], unread)
        .on_conflict_do_nothing()
    )
    return result.rowcount
