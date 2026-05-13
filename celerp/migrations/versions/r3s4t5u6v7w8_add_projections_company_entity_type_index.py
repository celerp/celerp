"""Add compound index on projections(company_id, entity_type).

Without this index every doc/inventory/contact list query does a full
company partition scan. With it the DB can seek directly to the entity
type slice before applying any JSON path filters.

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-05-13 00:00:00.000000
"""
from alembic import op

revision = "r3s4t5u6v7w8"
down_revision = "q2r3s4t5u6v7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_projections_company_entity_type",
        "projections",
        ["company_id", "entity_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_projections_company_entity_type", table_name="projections")
