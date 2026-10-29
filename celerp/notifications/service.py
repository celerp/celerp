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
from functools import partial
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy import delete, event, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from celerp.models.notification import Notification, NotificationRead
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


def link_for(notif: Notification, settings: dict | None, role: str) -> str | None:
    """The page a notice links a reader with ``role`` to. Settings > Accounting is only for
    those who manage accounting, so anyone else is shown such a notice with no link."""
    from celerp.accounting_roles import ACCOUNTING_SETTINGS_PATH
    from celerp.services.permissions import role_has_permission

    url = notif.action_url
    if url and url.startswith(ACCOUNTING_SETTINGS_PATH) and not role_has_permission(settings, role, "manage_accounting"):
        return None
    return url

# session.info keys: notification writes waiting for their transaction to commit, and
# events waiting for it to have committed, each with the (possibly nested) transaction
# that made it.
_WRITES = "celerp_notifications_writes"
_PENDING = "celerp_notifications_pending"


def _listen(sync: Session) -> None:
    if not event.contains(sync, "before_commit", _write_pending):
        event.listen(sync, "before_commit", _write_pending)
        event.listen(sync, "after_commit", _deliver_pending)
        event.listen(sync, "after_soft_rollback", _drop_rolled_back)
        event.listen(sync, "after_transaction_end", _drop_on_end)


def _made_in(sync: Session) -> SessionTransaction:
    """The (possibly nested) transaction a write is asked in, begun now when the session has
    not begun one yet, so its rollback or close drops the write."""
    return sync.get_nested_transaction() or sync.get_transaction() or sync.begin()


def _defer(session: AsyncSession, company_id: uuid.UUID | None, write: Callable[[Session], None]) -> None:
    """A notification write to ``company_id``'s notices (every company's when None) runs
    when its transaction commits, as its last statements. Notifications rank after every
    other lock (tests/lock_rank.py RANKS), so a transaction never holds a notice row while it
    waits for another lock, and a writer holding one never waits for a notice another
    transaction took first. A rolled-back savepoint drops the
    writes made in it, as it would their rows."""
    sync = session.sync_session
    _listen(sync)
    sync.info.setdefault(_WRITES, []).append((_made_in(sync), company_id, write))


def _write_pending(sync: Session) -> None:
    """before_commit, which also fires when a savepoint is released: the writes wait for
    the outermost commit, in the order they were asked for.

    First the companies they write are shared by key, in id order, before any notice: a
    company being reset holds its key until the reset commits, and a new notice's foreign
    key would otherwise wait for it while holding the notices table the reset locks next,
    which PostgreSQL breaks by aborting one of the two. A company the reset removed has no
    notices left to write. Then every notice of those companies is locked in one order,
    (company, id), so two commits that update or remove the same notices in different
    orders wait for each other instead of each holding one the other needs."""
    from celerp.models.company import Company

    if sync.in_nested_transaction():
        return
    writes = sync.info.pop(_WRITES, [])
    if not writes:
        return
    named = sorted({company_id for _, company_id, _ in writes if company_id is not None}, key=str)
    if named:
        present = {str(c) for c in sync.execute(
            select(Company.id).where(Company.id.in_(named)).order_by(Company.id)
            .with_for_update(read=True, key_share=True)).scalars()}
        writes = [w for w in writes if w[1] is None or str(w[1]) in present]
        if not writes:
            return
    companies = {company_id for _, company_id, _ in writes}
    notices = select(Notification.id).order_by(Notification.company_id, Notification.id).with_for_update()
    if None not in companies:
        notices = notices.where(Notification.company_id.in_(sorted(companies)))
    sync.execute(notices)
    for _, _, write in writes:
        write(sync)


def _publish_after_commit(sync: Session, company_id: uuid.UUID,
                          user_id: uuid.UUID | None, event_data: dict[str, Any]) -> None:
    _listen(sync)
    sync.info.setdefault(_PENDING, []).append((_made_in(sync), company_id, user_id, event_data))


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
    """A rolled-back savepoint (or the whole transaction) takes its writes with it."""
    for key in (_WRITES, _PENDING):
        pending = sync.info.get(key)
        if pending:
            pending[:] = [p for p in pending if not _within(p[0], previous_transaction)]


def _drop_on_end(sync: Session, transaction: SessionTransaction) -> None:
    """The outermost transaction ended without a commit (closed or rolled back):
    nothing it asked for is written, so nothing waits for a later one."""
    if transaction.parent is None:
        sync.info.pop(_WRITES, None)
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
    """Create a notification when the session commits (_defer), prune the company's
    oldest beyond MAX_PER_COMPANY, and publish it to SSE subscribers once committed.

    ``i18n`` ({"title": key, "body": key, "params": {...}}) names the message keys the
    English ``title`` and ``body`` were written from."""
    notif = Notification(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        category=category,
        title=title,
        body=body,
        action_url=action_url,
        priority=priority,
        i18n=i18n,
    )
    _defer(session, company_id, partial(_add, notif=notif))
    return notif


def _add(sync: Session, *, notif: Notification) -> None:
    sync.add(notif)
    sync.flush()

    # Prune: keep only newest MAX_PER_COMPANY per company
    company = Notification.company_id == notif.company_id
    total = sync.execute(select(func.count()).select_from(Notification).where(company)).scalar() or 0
    if total > MAX_PER_COMPANY:
        old_ids = list(sync.execute(
            select(Notification.id).where(company).order_by(Notification.created_at.desc()).offset(MAX_PER_COMPANY)
        ).scalars().all())
        if old_ids:
            sync.execute(delete(Notification).where(company, Notification.id.in_(old_ids)))

    _publish_after_commit(sync, notif.company_id, notif.user_id, {
        "type": "notification",
        "id": str(notif.id),
        "category": notif.category,
        "title": notif.title,
        "body": notif.body,
        "action_url": notif.action_url,
        "priority": notif.priority,
    })


def _addressed_to(user_id: uuid.UUID):
    """A notice reaches a user when it is addressed to them or to the whole company."""
    return (Notification.user_id == user_id) | (Notification.user_id.is_(None))


def _read_by(user_id: uuid.UUID):
    """The user has a read receipt for the notice."""
    return exists().where(
        NotificationRead.notification_id == Notification.id,
        NotificationRead.user_id == user_id,
    )


async def notify_once(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    *,
    user_id: uuid.UUID | None = None,
    action_url: str | None = None,
    i18n: dict | None = None,
) -> None:
    """A high-priority notice told once to the company, or to the member ``user_id``: never
    again to the same addressee with the same title and body, read or not. A notice already
    told takes ``action_url`` as its link, so the link follows the condition to where it is now.
    ``action_url`` and ``i18n`` as for ``create``; written when the session commits."""
    _defer(session, company_id, partial(_notify_once, company_id=company_id, category=category, title=title, body=body,
                            user_id=user_id, action_url=action_url, i18n=i18n))


def _notify_once(sync: Session, *, company_id, category, title, body, user_id, action_url, i18n) -> None:
    same = (
        Notification.company_id == company_id,
        Notification.category == category,
        Notification.title == title,
        Notification.body == body,
        Notification.user_id == user_id if user_id is not None else Notification.user_id.is_(None),
    )
    if sync.execute(select(Notification.id).where(*same).limit(1)).first():
        sync.execute(update(Notification).where(*same, Notification.action_url.is_distinct_from(action_url))
                     .values(action_url=action_url))
        return
    _add(sync, notif=Notification(company_id=company_id, user_id=user_id, category=category, title=title,
                                  body=body, action_url=action_url, priority="high", i18n=i18n))


async def notify_standing(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    i18n: dict | None = None,
) -> None:
    """A condition told to the company as a high-priority notice that stays current.

    Deduped on a standing notice with the same category and title, one no user has
    dismissed yet: while it stands it is brought up to date with this body, so it never
    shows an earlier state, and once dismissed a condition that persists notifies again.
    ``action_url`` and ``i18n`` as for ``create``; written when the session commits."""
    _defer(session, company_id, partial(_notify_standing, company_id=company_id, category=category, title=title, body=body,
                            action_url=action_url, i18n=i18n))


def _notify_standing(sync: Session, *, company_id, category, title, body, action_url, i18n) -> None:
    standing = sync.execute(
        select(Notification.id).where(
            Notification.company_id == company_id,
            Notification.category == category,
            Notification.title == title,
            ~exists().where(NotificationRead.notification_id == Notification.id),
        ).limit(1)
    ).scalar()
    if standing is not None:
        sync.execute(update(Notification).where(Notification.id == standing)
                     .values(body=body, action_url=action_url, i18n=i18n))
        return
    _add(sync, notif=Notification(company_id=company_id, category=category, title=title, body=body,
                                  action_url=action_url, priority="high", i18n=i18n))


async def notify_every_company(
    session: AsyncSession,
    category: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    i18n: dict | None = None,
) -> None:
    """An instance-wide condition, told to every company (notify_standing): a reboot
    while a notice stands brings it up to date instead of adding another. Written when
    the session commits."""
    from celerp.models.company import Company

    for cid in (await session.execute(select(Company.id))).scalars().all():
        await notify_standing(session, cid, category, title, body, action_url=action_url, i18n=i18n)


async def clear_every_company(session: AsyncSession, category: str, title: str) -> None:
    """An instance-wide condition (see notify_every_company) no longer holds: every
    company's notice of it is removed when the session commits, so a notice that stands
    always means the condition holds now."""
    _defer(session, None, lambda sync: sync.execute(
        delete(Notification).where(Notification.category == category, Notification.title == title)))


async def clear_standing(session: AsyncSession, company_id: uuid.UUID, category: str, title: str) -> None:
    """A company's condition (see notify_standing) no longer holds: its notice is
    removed when the session commits, so a notice that stands always means the
    condition holds now."""
    _defer(session, company_id, lambda sync: sync.execute(
        delete(Notification).where(Notification.company_id == company_id, Notification.category == category,
                                   Notification.title == title)))


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
    (dismissed) must not reappear on their next fetch, and the high-priority ones,
    which ask the user to act, come first.
    """
    read = _read_by(user_id)
    order = [Notification.created_at.desc(), Notification.id.desc()]  # id tiebreaker → stable pagination
    if unread_only:
        order.insert(0, (Notification.priority == "high").desc())
    q = (
        select(Notification, read.label("read"))
        .where(
            Notification.company_id == company_id,
            _addressed_to(user_id),
        )
        .order_by(*order)
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


async def mark_done(
    session: AsyncSession,
    company_id: uuid.UUID,
    action_url: str,
) -> None:
    """The action a notice asks for has been taken: every notice of the company linking
    to it is removed when the session commits, so the bell never asks for it again."""
    _defer(session, company_id, lambda sync: sync.execute(
        delete(Notification).where(Notification.company_id == company_id, Notification.action_url == action_url)))


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
