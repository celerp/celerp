# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""One default location per company

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-11-14

An earlier version let a company mark a second location default without clearing the first.
Where a company holds more than one, the oldest (created_at, then id) stays the default and the
others are cleared; then a partial unique index keeps it to one. Both steps can run again: the
clearing finds nothing left to clear and the index is created only when missing. The index is
written out so the restamp walker proves the revision by it. Forward-only data step.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.migrations._locks import lock_tables

revision = "v5w6x7y8z9a0"
down_revision = "u4v5w6x7y8z9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    lock_tables(conn, "locations")
    conn.execute(sa.text("""
        UPDATE locations l SET is_default = false
        WHERE l.is_default AND EXISTS (
            SELECT 1 FROM locations k
            WHERE k.company_id = l.company_id AND k.is_default
              AND (k.created_at, k.id) < (l.created_at, l.id))
    """))
    op.create_index("uq_location_one_default", "locations", ["company_id"], unique=True,
                    postgresql_where=sa.text("is_default"), if_not_exists=True)


def downgrade() -> None:
    op.drop_index("uq_location_one_default", table_name="locations", if_exists=True)
