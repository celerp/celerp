# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Give every document and List line stored before line ids existed its line id.

Revision ID: n9b0c1d2e3f4
Revises: m8a9b0c1d2e3
Create Date: 2026-11-04

Each such line gets the id derived from its record id and its position, the same id a
replay of the record derives, so a rebuild, the projection integrity check and a backup
restore all agree with the stored state. A line that already has an id keeps it.
"""

from __future__ import annotations

from alembic import op

from celerp.migrations._json_compat import update_projection_state
from celerp.services.document_lines import fill_line_ids

revision = "n9b0c1d2e3f4"
down_revision = "m8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    update_projection_state(op.get_bind(), lambda state, row: fill_line_ids(state, row["entity_id"]),
                            where="entity_type IN ('doc','list')", extra_cols=("entity_id",))


def downgrade() -> None:
    pass  # Data migration - no safe downgrade
