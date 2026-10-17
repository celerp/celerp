# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The posting accounts a person chose finishing a migration, offered on their next
client from the same source.

Finishing a migration saves each role's account as ``{code, name, account_type}`` under
the person who started it and the run's source, over what was saved before, so the
answers follow the latest client. The finish step offers them as preselected choices
(``posting_readiness.readiness`` with ``previous``); nothing is set on a company until
that company's own finish. The sample migration is not a client and saves nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.migration import MigrationRun, MigrationTemplate


async def answers(session: AsyncSession, user_id: uuid.UUID, source_system: str) -> dict[str, dict]:
    """The saved answers of ``user_id`` for ``source_system``; empty when there are none."""
    return dict(await session.scalar(select(MigrationTemplate.answers).where(
        MigrationTemplate.user_id == user_id, MigrationTemplate.source_system == source_system)) or {})


async def remember(session: AsyncSession, run: MigrationRun, final: dict[str, dict]) -> None:
    """Save the accounts ``run``'s finish chose (``final``, role to account) over the
    person's earlier answers for its source. Safe against a concurrent finish by the
    same person: the row is created once, then locked and merged. The caller commits."""
    if not final or run.source_summary.get("sample"):
        return
    await session.execute(pg_insert(MigrationTemplate).values(
        id=uuid.uuid4(), user_id=run.created_by_user_id, source_system=run.source_system, answers={},
        updated_at=datetime.now(timezone.utc)).on_conflict_do_nothing(
        index_elements=["user_id", "source_system"]))
    row = await session.scalar(select(MigrationTemplate).where(
        MigrationTemplate.user_id == run.created_by_user_id,
        MigrationTemplate.source_system == run.source_system).with_for_update())
    row.answers = {**(row.answers or {}), **final}
    row.updated_at = datetime.now(timezone.utc)


async def forget(session: AsyncSession, user_id: uuid.UUID, source_system: str) -> None:
    """Remove the person's saved answers for ``source_system``. Forgetting answers that
    are already gone is not an error."""
    await session.execute(delete(MigrationTemplate).where(
        MigrationTemplate.user_id == user_id, MigrationTemplate.source_system == source_system))
    await session.commit()
