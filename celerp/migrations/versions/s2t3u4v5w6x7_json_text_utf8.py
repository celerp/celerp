# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""On a SQL_ASCII database, every json value an earlier version stored with characters past
ASCII written as \\u escapes is rewritten to hold them as UTF-8 text, the form every engine
now writes (dump_json): such a database cannot read the escapes back, so a query or index on
that value failed. Any other encoding, or a database without such values, is left as it is.

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-11-10
"""
from alembic import op

from celerp.migrations._json_compat import unescape_json_text

revision = "s2t3u4v5w6x7"
down_revision = "r1s2t3u4v5w6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    unescape_json_text(op.get_bind())


def downgrade() -> None:
    # UTF-8 text and the escapes are one JSON value: nothing to put back.
    pass
