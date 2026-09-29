# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Company-wide write serialization - one primitive for read-check-write sections."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.models.projections import Projection


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
