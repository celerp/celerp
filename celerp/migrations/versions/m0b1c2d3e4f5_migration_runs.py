# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add migration_runs and migration_entity_maps for durable company migrations, and the
server-controlled companies.is_migration_staged flag.

Revision ID: m0b1c2d3e4f5
Revises: l9a0b1c2d3e4
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa

revision = "m0b1c2d3e4f5"
down_revision = "l9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("is_migration_staged", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))
    op.create_table(
        "migration_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("source_artifact_name", sa.Text(), nullable=True),
        sa.Column("prepared_by", sa.String(200), nullable=True),
        sa.Column("source_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("source_schema_version", sa.String(64), nullable=True),
        sa.Column("adapter_version", sa.String(64), nullable=False),
        sa.Column("cif_version", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_phase", sa.String(64), nullable=True),
        sa.Column("phase_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("coverage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("mapping_decisions", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("reconciliation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_migration_runs_company_id", "migration_runs", ["company_id"])

    op.create_table(
        "migration_entity_maps",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "migration_run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("migration_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(128), nullable=False),
        sa.Column("source_external_id", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column("target_entity_type", sa.String(64), nullable=False),
        sa.Column("target_entity_id", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint(
            "migration_run_id", "source_type", "source_external_id", name="uq_migration_entity_map_source"
        ),
    )
    op.create_index("ix_migration_entity_maps_migration_run_id", "migration_entity_maps", ["migration_run_id"])
    op.create_index("idx_migration_entity_map_run_status", "migration_entity_maps", ["migration_run_id", "status"])


def downgrade() -> None:
    op.drop_table("migration_entity_maps")
    op.drop_table("migration_runs")
    op.drop_column("companies", "is_migration_staged")
