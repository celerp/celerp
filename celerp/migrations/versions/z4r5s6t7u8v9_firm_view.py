# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The firm view: when a connected company last reached this installation with its key,
and the records it sent that were not applied.

Revision ID: z4r5s6t7u8v9
Revises: y3q4r5s6t7u8
"""
from alembic import op
import sqlalchemy as sa

revision = "z4r5s6t7u8v9"
down_revision = "y3q4r5s6t7u8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company_peer_grants", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "company_peer_attention",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_company_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("source_entity_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "source_company_id", "kind", "entity_type", "source_entity_id",
                            name="uq_company_peer_attention_record"),
    )
    op.create_index("ix_company_peer_attention_company", "company_peer_attention", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_company_peer_attention_company", table_name="company_peer_attention")
    op.drop_table("company_peer_attention")
    op.drop_column("company_peer_grants", "last_used_at")
