# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The firm name a company key carries, the name the client sees on what the firm sends.

Revision ID: f0y1z2a3b4c5
Revises: e9x0y1z2a3b4
"""
from alembic import op
import sqlalchemy as sa

revision = "f0y1z2a3b4c5"
down_revision = "e9x0y1z2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A key issued before this revision keeps the name its client was shown until now: the
    # company the firm's copy was opened from, else the copy's own name.
    op.add_column("company_peer_grants", sa.Column("firm", sa.String(160), nullable=True))
    op.execute(
        "UPDATE company_peer_grants g SET firm = LEFT(COALESCE(NULLIF(TRIM("
        "CAST(c.settings AS jsonb) -> 'restored_backup' ->> 'opened_from'), ''), c.name), 160) "
        "FROM companies c WHERE c.id = g.company_id"
    )
    with op.batch_alter_table("company_peer_grants") as batch:
        batch.alter_column("firm", existing_type=sa.String(160), nullable=False)


def downgrade() -> None:
    op.drop_column("company_peer_grants", "firm")
