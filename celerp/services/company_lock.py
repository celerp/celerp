# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Company-wide write serialization - one primitive for read-check-write sections."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company


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
