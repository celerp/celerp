# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The links a store fetches product images from are kept as token hashes.

Re-running the upgrade changes nothing that is already there.

Revision ID: l7f8a9b0c1d2
Revises: k6e7f8a9b0c1
"""
import sqlalchemy as sa
from alembic import op

revision = "l7f8a9b0c1d2"
down_revision = "k6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "connector_media_tokens" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "connector_media_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connector_media_tokens_expires_at", "connector_media_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_table("connector_media_tokens")
