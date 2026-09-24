# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backfill one usable installation owner on existing installations.

Revision ID: h5c6d7e8f9a0
Revises: g4b5c6d7e8f9
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "h5c6d7e8f9a0"
down_revision = "g4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE users
        SET is_install_owner = TRUE
        WHERE id = (
            SELECT u.id
            FROM users AS u
            LEFT JOIN user_companies AS uc ON uc.user_id = u.id
            GROUP BY u.id, u.is_active, u.created_at
            ORDER BY
                CASE
                    WHEN u.is_active IS TRUE AND MAX(
                        CASE WHEN uc.is_active IS TRUE AND uc.role = 'owner' THEN 1 ELSE 0 END
                    ) = 1 THEN 0
                    WHEN u.is_active IS TRUE AND MAX(
                        CASE WHEN uc.is_active IS TRUE AND uc.role = 'admin' THEN 1 ELSE 0 END
                    ) = 1 THEN 1
                    WHEN u.is_active IS TRUE AND MAX(
                        CASE WHEN uc.is_active IS TRUE THEN 1 ELSE 0 END
                    ) = 1 THEN 2
                    WHEN u.is_active IS TRUE THEN 3
                    ELSE 4
                END,
                u.created_at ASC,
                u.id ASC
            LIMIT 1
        )
        AND NOT EXISTS (
            SELECT 1 FROM users WHERE is_install_owner IS TRUE
        )
    """))


def downgrade() -> None:
    pass
