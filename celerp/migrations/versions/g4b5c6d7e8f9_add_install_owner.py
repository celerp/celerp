# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Persist installation ownership on the user row.

Revision ID: g4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "g4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_install_owner", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "uq_users_install_owner_true",
        "users",
        ["is_install_owner"],
        unique=True,
        postgresql_where=sa.text("is_install_owner IS TRUE"),
        sqlite_where=sa.text("is_install_owner IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_install_owner_true", table_name="users")
    op.drop_column("users", "is_install_owner")
