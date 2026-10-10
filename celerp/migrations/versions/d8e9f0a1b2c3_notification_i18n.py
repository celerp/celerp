# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Let a notification carry the message keys it was written from.

Revision ID: d8e9f0a1b2c3
Revises: c7f1a2b3d4e5
Create Date: 2026-10-05

A notice is stored as English text. One that also stores its title and body keys
and their params (``i18n``) is shown in each reader's language; one without them,
every notice written before, is shown as stored.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d8e9f0a1b2c3"
down_revision = "c7f1a2b3d4e5"
branch_labels = None
depends_on = None


def _columns() -> list[str]:
    """The notifications table's columns; none when the table is not there yet (a new
    install creates it from the model, i18n included)."""
    inspector = inspect(op.get_bind())
    return [c["name"] for c in inspector.get_columns("notifications")] if inspector.has_table("notifications") else []


def upgrade() -> None:
    columns = _columns()
    if columns and "i18n" not in columns:
        op.add_column("notifications", sa.Column("i18n", sa.JSON, nullable=True))


def downgrade() -> None:
    if "i18n" in _columns():
        op.drop_column("notifications", "i18n")
