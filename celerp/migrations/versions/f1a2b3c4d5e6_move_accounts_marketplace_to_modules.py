"""move accounts and marketplace_configs to module ownership (compensating no-op)

Revision ID: f1a2b3c4d5e6
Revises: e3f4a5b6c7d8
Create Date: 2026-03-17

True no-op: accounts and marketplace_configs are owned by their respective
modules (celerp-accounting, celerp-connectors). Schema is created by
SQLAlchemy create_all() when the module is loaded. This migration exists
only to maintain the revision chain for existing installs that had these
tables in the kernel schema before the module split.
"""
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

from alembic import op  # noqa: F401 — required by Alembic migration chain

revision: str = "f1a2b3c4d5e6"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
