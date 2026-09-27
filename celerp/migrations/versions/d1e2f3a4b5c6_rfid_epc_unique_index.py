# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Remove any RFID / EPC unique index (physical-code uniqueness is enforced on write).

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-09-10

This revision originally created a unique index on item RFID / EPC values. Like the
barcode index, it could block an upgrade on existing duplicate data, so it now only
ensures no such index exists.
"""

from __future__ import annotations

from alembic import op

from celerp.inventory_codes import RFID_EPC_UNIQUE_INDEX

revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {RFID_EPC_UNIQUE_INDEX}")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {RFID_EPC_UNIQUE_INDEX}")
