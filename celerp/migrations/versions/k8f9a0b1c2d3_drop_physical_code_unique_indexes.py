# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Drop the physical-code unique indexes from databases that already created them.

Revision ID: k8f9a0b1c2d3
Revises: j7e8f9a0b1c2
Create Date: 2026-09-27

Databases upgraded while the barcode and RFID / EPC revisions still created unique
indexes carry them past those revisions. Uniqueness is now checked when a write
introduces a code, so the indexes are removed here and every upgrade path converges
on the same schema.
"""

from __future__ import annotations

from alembic import op

from celerp.inventory_codes import (
    BARCODE_UNIQUE_INDEX,
    LEGACY_BARCODE_UNIQUE_INDEX,
    RFID_EPC_UNIQUE_INDEX,
)

revision = "k8f9a0b1c2d3"
down_revision = "j7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX, RFID_EPC_UNIQUE_INDEX):
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    # Recreating the indexes would fail on the duplicate data this revision exists to
    # allow, and no earlier revision needs them; nothing to restore.
    pass
