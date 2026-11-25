# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""One default location per company

Revision ID: c4d1e7a2f9b3
Revises: y8d9e0f1a2b3
Create Date: 2026-11-14

An earlier version let a company mark a second location default without clearing the first.
Where a company holds more than one, the oldest (created_at, then id) stays the default and the
others are cleared; then a partial unique index keeps it to one. Which location each such company
kept and which it lost are recorded (runtime state ``location_default_cleared``), so the next start
tells the company (services/location_default_notice.py). Both steps can run again: the clearing
finds nothing left to clear and the index is created only when missing. The index is written out
so the restamp walker proves the revision by it. Forward-only data step.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.json_text import dump_json
from celerp.migrations._locks import lock_tables

revision = "c4d1e7a2f9b3"
down_revision = "y8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    lock_tables(conn, "locations", "system_runtime_state")
    # The CTE reads the rows as they were before the update: the oldest default is the one kept.
    rows = conn.execute(sa.text("""
        WITH kept AS (
            SELECT DISTINCT ON (company_id) company_id, id FROM locations
            WHERE is_default ORDER BY company_id, created_at, id),
        cleared AS (
            UPDATE locations l SET is_default = false
            WHERE l.is_default AND EXISTS (
                SELECT 1 FROM locations k
                WHERE k.company_id = l.company_id AND k.is_default
                  AND (k.created_at, k.id) < (l.created_at, l.id))
            RETURNING l.company_id, l.id, l.created_at)
        SELECT c.company_id, kept.id, array_agg(c.id ORDER BY c.created_at, c.id)
        FROM cleared c JOIN kept ON kept.company_id = c.company_id
        GROUP BY c.company_id, kept.id
    """)).all()
    if rows:
        _record(conn, {str(cid): {"kept": str(kept), "cleared": [str(i) for i in cleared]}
                       for cid, kept, cleared in rows})
    op.create_index("uq_location_one_default", "locations", ["company_id"], unique=True,
                    postgresql_where=sa.text("is_default"), if_not_exists=True)


def _record(conn, found: dict) -> None:
    """Add *found* to the runtime state's record of cleared defaults, by company."""
    value = conn.execute(sa.text("SELECT value FROM system_runtime_state WHERE id = 1")).scalar()
    value = dict(value or {})
    value["location_default_cleared"] = {**value.get("location_default_cleared", {}), **found}
    conn.execute(sa.text("""
        INSERT INTO system_runtime_state (id, value) VALUES (1, CAST(:v AS json))
        ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value
    """), {"v": dump_json(value)})


def downgrade() -> None:
    op.drop_index("uq_location_one_default", table_name="locations", if_exists=True)
