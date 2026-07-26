# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""rename account 6960 to Foreign Exchange Difference

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-26

Nothing posts to 6960 automatically any more: a manual entry that converts to an
uneven base amount is refused, and the person posting it writes the difference
line themselves. The account is still the natural home for that line, so it
stays, but "Rounding" no longer describes what lands there.

Only companies still carrying the seeded name are renamed. A company that named
the account something of its own chose that name, and this is not the place to
overrule it.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

_CODE = "6960"
_OLD_NAME = "Foreign Exchange Rounding"
_NEW_NAME = "Foreign Exchange Difference"


def _rename(old: str, new: str) -> None:
    conn = op.get_bind()
    # to_regclass resolves against the connection's search_path rather than
    # assuming public, so the guard is correct for any schema this runs in.
    if not conn.execute(sa.text("SELECT to_regclass('accounts')")).scalar():
        return  # Nothing to rename on a fresh install
    conn.execute(
        sa.text("UPDATE accounts SET name = :new WHERE code = :code AND name = :old"),
        {"new": new, "old": old, "code": _CODE},
    )


def upgrade() -> None:
    _rename(_OLD_NAME, _NEW_NAME)


def downgrade() -> None:
    _rename(_NEW_NAME, _OLD_NAME)
