# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Enforce at-most-one item per (company, rfid_epc) with a partial unique index.

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-09-10

Legacy SQL_ASCII databases omit this PostgreSQL JSON expression index because
unrelated escaped Unicode in the same JSON value can make evaluation fail.
"""

from __future__ import annotations

from alembic import op

from celerp.inventory_codes import RFID_EPC_UNIQUE_INDEX
from celerp.migrations._json_compat import is_sql_ascii

revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if is_sql_ascii(conn):
        op.execute(f"DROP INDEX IF EXISTS {RFID_EPC_UNIQUE_INDEX}")
        return
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {RFID_EPC_UNIQUE_INDEX} "
        "ON projections (company_id, (state ->> 'rfid_epc')) "
        "WHERE entity_type = 'item' AND NULLIF(state ->> 'rfid_epc', '') IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {RFID_EPC_UNIQUE_INDEX}")
