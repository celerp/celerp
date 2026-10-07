# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Notification service - CRUD + SSE publishing.

All notification operations go through this module. The SSE pub/sub
is in-process (asyncio.Queue per subscriber). No Redis required. A new
notification is published when the transaction that wrote it commits, so a
client that fetches it on seeing the event finds the row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, func, select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from celerp.models.notification import Notification
from celerp.notifications.sse import deliver

log = logging.getLogger(__name__)

MAX_PER_COMPANY = 100

# Notices stored as English text before a notice could carry its message keys, by the
# (category, title) they were stored with. The upgrade that wrote one cannot be changed,
# so the keys are supplied when it is listed.
STORED_NOTICE_KEYS = {
    # migration f8a9b0c1d2e3 (Hours per day moved onto work centers)
    ("manufacturing", "Hours per day moved to work centers"): {
        "title": "notice.work_centers_moved.title", "body": "notice.work_centers_moved.body"},
}


def message_keys(notif: Notification) -> dict | None:
    """The message keys a notice is shown from in the reader's language: its own, or those of
    a notice an earlier release stored without them; none for a notice shown as stored."""
    return notif.i18n or STORED_NOTICE_KEYS.get((notif.category, notif.title))

# session.info key: events waiting for their transaction to commit, each with
# the (possibly nested) transaction that wrote its row.
_PENDING = "celerp_notifications_pending"


def _publish_after_commit(session: AsyncSession, company_id: uuid.UUID,
                          user_id: uuid.UUID | None, event_data: dict[str, Any]) -> None:
    sync = session.sync_session
    if not event.contains(sync, "after_commit", _deliver_pending):
        event.listen(sync, "after_commit", _deliver_pending)
        event.listen(sync, "after_soft_rollback", _drop_rolled_back)
        event.listen(sync, "after_transaction_end", _drop_on_end)
    writer = sync.get_nested_transaction() or sync.get_transaction()
    sync.info.setdefault(_PENDING, []).append((writer, company_id, user_id, event_data))


def _deliver_pending(sync: Session) -> None:
    """after_commit, which also fires when a savepoint is released: only the
    outermost commit makes the rows visible to other connections."""
    if sync.in_nested_transaction():
        return
    for _, company_id, user_id, event_data in sync.info.pop(_PENDING, []):
        try:
            deliver(company_id, user_id, event_data)
        except Exception:
            log.warning("Failed to publish SSE notification", exc_info=True)


def _within(txn: SessionTransaction | None, ended: SessionTransaction) -> bool:
    while txn is not None:
        if txn is ended:
            return True
        txn = txn.parent
    return False


def _drop_rolled_back(sync: Session, previous_transaction: SessionTransaction) -> None:
    """A rolled-back savepoint (or the whole transaction) takes its rows with it."""
    pending = sync.info.get(_PENDING)
    if pending:
        pending[:] = [p for p in pending if not _within(p[0], previous_transaction)]


def _drop_on_end(sync: Session, transaction: SessionTransaction) -> None:
    """The outermost transaction ended without a commit (closed or rolled back):
    nothing it wrote exists, so nothing waits for a later one."""
    if transaction.parent is None:
        sync.info.pop(_PENDING, None)


async def create_keyed(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    key: str,
    params: dict[str, Any],
    **kwargs: Any,
) -> Notification:
    """Create a notification from catalog message *key* (``<key>.title`` / ``<key>.body``)
    with *params*: stored in English, with the keys each reader's language is shown from
    (``i18n``). A param given as ``{"key": k}`` is itself the message *k*."""
    from ui.i18n import t

    english = {name: t(v["key"], "en") if isinstance(v, dict) else v for name, v in params.items()}
    keyed = {name: {"message": english[name], "message_key": v["key"]} if isinstance(v, dict) else v
             for name, v in params.items()}
    return await create(session, company_id, category, t(f"{key}.title", "en", **english),
                        t(f"{key}.body", "en", **english),
                        i18n={"title": f"{key}.title", "body": f"{key}.body", "params": keyed}, **kwargs)


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
    """Create a notification, prune old ones, and publish it to SSE subscribers
    once the session commits.

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

    _publish_after_commit(session, company_id, user_id, {
        "type": "notification",
        "id": str(notif.id),
        "category": category,
        "title": title,
        "body": body,
        "action_url": action_url,
        "priority": priority,
    })

    return notif


async def notify_once(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    i18n: dict | None = None,
) -> bool:
    """A high-priority notice told to the company once: never again with the same title and
    body, read or not. ``action_url`` and ``i18n`` as for ``create``. Caller commits. Returns
    whether it was created."""
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
    await create(session, company_id, category, title, body, action_url=action_url, priority="high", i18n=i18n)
    return True


async def notify_every_company(
    session: AsyncSession,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    i18n: dict | None = None,
) -> int:
    """An instance-wide condition, told to every company as a high-priority notice.

    Deduped on the unread notice: at most one stands per company per title, so a
    condition that persists re-notifies only after the prior notice was read. A notice
    that still stands is brought up to date with this body, so it never describes an
    earlier cause. ``action_url`` and ``i18n`` as for ``create``. Caller commits. Returns
    the number of notifications created."""
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
                                  .values(body=body, action_url=action_url, i18n=i18n))
            continue
        await create(session, cid, category, title, body, action_url=action_url, priority="high", i18n=i18n)
        created += 1
    return created


async def clear_every_company(session: AsyncSession, category: str, title: str) -> int:
    """An instance-wide condition (see notify_every_company) no longer holds: every
    company's unread notice of it is marked read, so a notice that stands always means
    the condition holds now. Caller commits. Returns count updated."""
    result = await session.execute(
        update(Notification)
        .where(
            Notification.category == category,
            Notification.title == title,
            Notification.read == False,  # noqa: E712
        )
        .values(read=True)
    )
    return result.rowcount


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
