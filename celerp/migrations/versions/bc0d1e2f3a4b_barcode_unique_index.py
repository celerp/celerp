# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Remove any barcode unique index (physical-code uniqueness is enforced on write).

Revision ID: bc0d1e2f3a4b
Revises: b3c4d5e6f7a8
Create Date: 2026-08-25

This revision originally created a unique index on item barcodes, which stopped a
database that already held a duplicate barcode from upgrading. Uniqueness is now
checked when a write introduces a code, and existing duplicates are left in place
for the user to resolve, so the revision only ensures no such index exists.
"""

from __future__ import annotations

from alembic import op

from celerp.inventory_codes import BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX

revision = "bc0d1e2f3a4b"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def _drop_indexes() -> None:
    op.execute(f"DROP INDEX IF EXISTS {BARCODE_UNIQUE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {LEGACY_BARCODE_UNIQUE_INDEX}")


def upgrade() -> None:
    _drop_indexes()


def downgrade() -> None:
    _drop_indexes()
