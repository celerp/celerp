# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Company-wide write serialization - one primitive for read-check-write sections."""

from __future__ import annotations

import hashlib

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from celerp.models.company import Company
from celerp.models.projections import Projection

# Ids of the companies loaded with locked_company() in the session's current transaction.
_LOCKED = "celerp_locked_companies"


async def lock_company(session: AsyncSession, company_id) -> None:
    """Serialize a read-check-write section with every other holder for the company.

    Two concurrent writers that each check a condition and then write would both
    pass the check. Taking a row lock on the company makes the second writer wait
    for the first to commit, so it reads the committed state instead. The lock is
    held until the caller's transaction commits or rolls back.

    The mode is FOR NO KEY UPDATE, not FOR UPDATE. Every ledger insert takes an
    implicit foreign-key KEY SHARE lock on its company row and holds it to commit,
    so a plain FOR UPDATE here would have to upgrade past that share lock: two
    transactions that have each already emitted an event for the company both hold
    KEY SHARE and then block on each other's row lock, which PostgreSQL breaks by
    aborting one with a deadlock (40P01). FOR NO KEY UPDATE does not conflict with
    KEY SHARE, so the upgrade never happens, while it still conflicts with another
    FOR NO KEY UPDATE.

    Canonical lock order: take this lock first, then any Projection row locks.
    Never acquire it after SELECT ... FOR UPDATE on a Projection.
    """
    await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update(key_share=True)
    )


async def lock_chart(session: AsyncSession, company_id) -> None:
    """Serialize changes to the shape of the company's chart of accounts.

    Adding an account, moving one, changing its type, switching it on or off, and
    pointing a posting role at one each read other accounts (a parent, the children)
    before writing. Taking this lock first makes those changes happen one at a time
    for a company, so each one checks the chart the previous one left. Postings
    never take it. Lock order: the company lock, when the caller holds it, then this
    lock, then account rows. Held until the transaction ends; SQLite writes one
    transaction at a time already.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(b"chart:" + str(company_id).encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def lock_projections(session: AsyncSession, company_id, entity_ids) -> dict[str, Projection]:
    """Lock the company, then the given Projection rows in entity-id order.

    The one way to take row locks for a write: company first, then rows sorted by
    entity id, so two writers sharing rows always meet in the same order. A caller
    locking documents and items takes the documents in one call and the items in a
    later one. populate_existing replaces any stale copy already in the session.
    Ids with no row are absent from the result.
    """
    await lock_company(session, company_id)
    want = sorted({str(e) for e in entity_ids if e})
    if not want:
        return {}
    rows = (await session.execute(
        select(Projection)
        .where(Projection.company_id == company_id, Projection.entity_id.in_(want))
        .order_by(Projection.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    return {r.entity_id: r for r in rows}


async def locked_company(session: AsyncSession, company_id) -> Company | None:
    """Take the company lock, then load the company as the previous holder committed it.

    For a read-modify-write of the company row itself (its settings in particular):
    reading after the lock means a concurrent writer's change is built on, never
    overwritten by a stale copy.

    Company settings are one JSON value, so this is the only way to change them:
    a flush that changes the settings of a company not loaded here is refused.
    Document numbering keeps its counters in the settings and follows the same rule.
    """
    await lock_company(session, company_id)
    company = await session.get(Company, company_id, populate_existing=True)
    if company is not None:
        session.info.setdefault(_LOCKED, set()).add(company.id)
    return company


def holds_company_lock(session: AsyncSession, company_id) -> bool:
    """True when this transaction loaded the company with locked_company()."""
    return company_id in session.info.get(_LOCKED, ())


@event.listens_for(Session, "before_flush")
def _settings_change_needs_the_lock(session: Session, flush_context, instances) -> None:
    locked = session.info.get(_LOCKED, ())
    for obj in session.dirty:
        if isinstance(obj, Company) and obj.id not in locked and get_history(obj, "settings").has_changes():
            raise RuntimeError(
                f"Company {obj.id} settings changed without locked_company(); load the company "
                "with it before reading the settings to change"
            )


@event.listens_for(Session, "after_transaction_end")
def _lock_released(session: Session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_LOCKED, None)
