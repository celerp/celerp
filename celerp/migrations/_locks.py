# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The table lock a data-backfill migration takes before it writes a company's rows.

A migration can run inside a serving process (System Recovery reconciles the restored schema),
so its data step may meet requests that hold their company's lock and rows of the same table.
Taking the tables EXCLUSIVE first, in one statement, keeps every such request out of them until
the revision's transaction ends: plain reads go on, and nothing that would hold a row of them
can hold one the data step then waits for.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def lock_tables(*tables: str) -> None:
    """Lock ``tables`` EXCLUSIVE for the rest of the revision's transaction, named in sorted
    order, before the data step's first read of them. A no-op off PostgreSQL."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    names = ", ".join(bind.dialect.identifier_preparer.quote(t) for t in sorted(set(tables)))
    bind.execute(sa.text(f"LOCK TABLE {names} IN EXCLUSIVE MODE"))
