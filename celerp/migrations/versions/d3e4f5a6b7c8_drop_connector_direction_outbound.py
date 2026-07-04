# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""drop connector direction columns + outbound_queue (connectors are inbound-only)

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-03

Connectors pull from the platform into Celerp only; there is no outbound push, so
the per-run/per-config `direction` and the never-used `outbound_queue` are removed.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: a develop build creates its schema from the current models (which
    # already lack these) via create_all, then the dev→release path replays migrations.
    # Guard each drop so re-applying against an already-current schema is a no-op.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "direction" in {c["name"] for c in insp.get_columns("sync_runs")}:
        op.drop_column("sync_runs", "direction")
    if "direction" in {c["name"] for c in insp.get_columns("connector_configs")}:
        op.drop_column("connector_configs", "direction")
    if insp.has_table("outbound_queue"):
        op.drop_table("outbound_queue")


def downgrade() -> None:
    op.add_column("sync_runs", sa.Column("direction", sa.String(16), nullable=False, server_default="inbound"))
    op.add_column("connector_configs", sa.Column("direction", sa.String(16), nullable=False, server_default="both"))
    op.create_table(
        "outbound_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(64), nullable=False, index=True),
        sa.Column("connector", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("error_message", sa.Text, nullable=True),
    )
