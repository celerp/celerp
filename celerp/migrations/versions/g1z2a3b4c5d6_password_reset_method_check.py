# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The password reset method check in a form a backup restores unchanged.

Postgres prints ``method IN (...)`` on a varchar column as an array cast to text[], and reads
that back as an array of text casts, so a restored database carried a check worded
differently from the one it was backed up from. Comparing as text keeps one wording.

Revision ID: g1z2a3b4c5d6
Revises: f0y1z2a3b4c5
"""
from alembic import op

revision = "g1z2a3b4c5d6"
down_revision = "f0y1z2a3b4c5"
branch_labels = None
depends_on = None


def _replace(condition: str) -> None:
    op.drop_constraint("ck_password_resets_method", "password_resets", type_="check")
    op.create_check_constraint("ck_password_resets_method", "password_resets", condition)


def upgrade() -> None:
    _replace("method::text IN ('email_code', 'terminal', 'owner')")


def downgrade() -> None:
    _replace("method IN ('email_code', 'terminal', 'owner')")
