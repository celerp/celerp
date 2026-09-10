# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Enforce at-most-one item per (company, rfid_epc) with a partial unique index.

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-09-10

Context
-------
An RFID / EPC identifies one physical tag, so it is company-unique in the same way a
barcode is. Application writes go through ``assert_rfid_epc_available`` under the item
code namespace lock; this migration adds the final defense behind that lock so any
writer that bypasses it cannot create a duplicate EPC.

A PostgreSQL partial expression unique index on ``(company_id, state ->> 'rfid_epc')``
for item projections with a non-empty EPC. EPC is a new field with no legacy data, so
there is nothing to remediate and no preflight; ``IF NOT EXISTS`` keeps the migration
idempotent alongside the index the model declares for ``create_all``.
"""

from __future__ import annotations

from alembic import op

from celerp.inventory_codes import RFID_EPC_UNIQUE_INDEX

revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {RFID_EPC_UNIQUE_INDEX} "
        "ON projections (company_id, (state ->> 'rfid_epc')) "
        "WHERE entity_type = 'item' AND NULLIF(state ->> 'rfid_epc', '') IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {RFID_EPC_UNIQUE_INDEX}")
