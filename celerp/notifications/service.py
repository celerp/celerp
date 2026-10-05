# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Notification service - CRUD + SSE publishing.

All notification operations go through this module. The SSE pub/sub
is in-process (asyncio.Queue per subscriber). No Redis required. A new
notification is published when the transaction that wrote it commits, so a
client that fetches it on seeing the event finds the row.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from celerp.models.notification import Notification
from celerp.notifications.sse import deliver

log = logging.getLogger(__name__)

MAX_PER_COMPANY = 100

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
    """Create a notification readers see in their own language: it is stored as the
    message *key* (``<key>.title`` / ``<key>.body`` in the catalogs) plus *params*.
    A param given as ``{"key": k}`` is itself the message *k*. See ``readable``."""
    body = json.dumps({"message_key": key, "params": params})
    return await create(session, company_id, category, key, body, **kwargs)


def readable(title: str, body: str) -> dict[str, Any]:
    """The title and body to show for a stored notification, with the message key
    and params a client translates from (None for a plain-text notification)."""
    try:
        keyed = json.loads(body)
    except ValueError:
        keyed = None
    if not isinstance(keyed, dict) or "message_key" not in keyed:
        return {"title": title, "body": body, "message_key": None, "message_params": None}
    from ui.i18n import localize_notification
    return localize_notification({"message_key": keyed["message_key"],
                                  "message_params": keyed.get("params") or {}}, "en")


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
    """Create a notification, prune old ones, and publish it to SSE subscribers
    once the session commits."""
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

    _publish_after_commit(session, company_id, user_id, {
        "type": "notification",
        "id": str(notif.id),
        "category": category,
        **readable(title, body),
        "action_url": action_url,
        "priority": priority,
    })

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
    unread_only: bool = False,
) -> list[Notification]:
    """List notifications for a user, newest first.

    unread_only powers the bell, which is an unread inbox: a read (dismissed)
    notification must not reappear on the next fetch.
    """
    q = (
        select(Notification)
        .where(
            Notification.company_id == company_id,
            (Notification.user_id == user_id) | (Notification.user_id.is_(None)),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())  # id tiebreaker → stable pagination
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
