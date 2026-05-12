# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Add session_registry and user_auth_state tables.

Revision ID: p1q2r3s4t5u6
Revises: o9p0q1r2s3t4
Create Date: 2026-05-13
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "p1q2r3s4t5u6"
down_revision = "o9p0q1r2s3t4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_registry",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_session_registry_user_id", "session_registry", ["user_id"])
    op.create_index("ix_session_registry_expiry", "session_registry", ["expiry"])

    op.create_table(
        "user_auth_state",
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("evicted_by_ip", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Backfill one user_auth_state row per existing user (fresh nonce, no eviction)
    conn = op.get_bind()
    users = conn.execute(sa.text("SELECT id FROM users")).fetchall()
    if users:
        conn.execute(
            sa.text(
                "INSERT INTO user_auth_state (user_id, nonce) VALUES (:uid, :nonce)"
            ),
            [{"uid": row[0], "nonce": str(uuid.uuid4())} for row in users],
        )


def downgrade() -> None:
    op.drop_table("user_auth_state")
    op.drop_index("ix_session_registry_expiry", "session_registry")
    op.drop_index("ix_session_registry_user_id", "session_registry")
    op.drop_table("session_registry")
