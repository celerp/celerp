# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connector work runs as executions that hold a lease, and a connection's first sync
has a record of its own.

Each connection gets its first-sync record: a store connection whose first sync was
started counts it as completed, as do accounting connections, which have no reviewed
first sync. The store's webhooks are marked current. Re-running the upgrade changes
nothing that is already there.

Revision ID: k6e7f8a9b0c1
Revises: j5d6e7f8a9b0
"""
import sqlalchemy as sa
from alembic import op

revision = "k6e7f8a9b0c1"
down_revision = "j5d6e7f8a9b0"
branch_labels = None
depends_on = None

# Connectors whose first sync a person reviews and starts, as they stood at this revision.
_REVIEWED = ("woocommerce", "shopify")


def _key_columns() -> list[sa.Column]:
    return [
        sa.Column("company_id", sa.String(64), primary_key=True),
        sa.Column("connector", sa.String(32), primary_key=True),
        sa.Column("resource", sa.String(32), primary_key=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "connector_execution_leases" not in tables:
        op.create_table(
            "connector_execution_leases", *_key_columns(),
            sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("holder", sa.Uuid(), nullable=True),
        )
    if "connector_execution_heartbeats" not in tables:
        op.create_table(
            "connector_execution_heartbeats", *_key_columns(),
            sa.Column("holder", sa.Uuid(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rerun_requested", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "connector_first_syncs" not in tables:
        op.create_table(
            "connector_first_syncs",
            sa.Column("company_id", sa.String(64), primary_key=True),
            sa.Column("connector", sa.String(32), primary_key=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("started_by", sa.String(64), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
            sa.Column("directions", sa.JSON(), nullable=True),
            sa.Column("match", sa.String(16), nullable=True),
            sa.Column("preview_version", sa.String(64), nullable=True),
            sa.Column("batch_id", sa.String(64), nullable=True),
            sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("undo", sa.JSON(), nullable=True),
            sa.Column("error", sa.JSON(), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "state IN ('pending', 'starting', 'running', 'completed', 'failed')",
                name="ck_connector_first_sync_state",
            ),
        )
    columns = {c["name"] for c in inspector.get_columns("connector_configs")}
    if "webhooks_dirty" not in columns:
        op.add_column("connector_configs", sa.Column(
            "webhooks_dirty", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "webhooks_version" not in columns:
        op.add_column("connector_configs", sa.Column(
            "webhooks_version", sa.BigInteger(), nullable=False, server_default="0"))
    # A connection made before reviewed first syncs existed has been syncing since its
    # first run after its last reset (runs before that belong to an earlier connection):
    # that run is its completed first sync. Connectors without a reviewed first sync have
    # nothing to review.
    bind.execute(sa.text("""
        INSERT INTO connector_first_syncs (company_id, connector, state, started_at)
        SELECT c.company_id, c.connector,
               CASE WHEN c.connector NOT IN :reviewed OR s.first_run IS NOT NULL
                    THEN 'completed' ELSE 'pending' END,
               s.first_run
        FROM connector_configs c
        LEFT JOIN LATERAL (
            SELECT min(r.started_at) AS first_run FROM sync_runs r
            WHERE r.company_id = c.company_id AND r.connector = c.connector
              AND r.entity <> '__connector_reset__'
              AND r.started_at > coalesce((
                  SELECT max(x.started_at) FROM sync_runs x
                  WHERE x.company_id = c.company_id AND x.connector = c.connector
                    AND x.entity = '__connector_reset__'), '-infinity'::timestamptz)
        ) s ON true
        WHERE NOT EXISTS (SELECT 1 FROM connector_first_syncs f
                          WHERE f.company_id = c.company_id AND f.connector = c.connector)
    """).bindparams(sa.bindparam("reviewed", expanding=True)), {"reviewed": list(_REVIEWED)})


def downgrade() -> None:
    op.drop_column("connector_configs", "webhooks_version")
    op.drop_column("connector_configs", "webhooks_dirty")
    op.drop_table("connector_first_syncs")
    op.drop_table("connector_execution_heartbeats")
    op.drop_table("connector_execution_leases")
