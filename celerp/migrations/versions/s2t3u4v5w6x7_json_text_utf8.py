# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every json value an earlier version stored with characters past ASCII written as \\u
escapes is rewritten to hold them as UTF-8 text, the form every engine now writes (dump_json):
a SQL_ASCII database cannot read the escapes back, so a query or index on that value failed. It
is the first revision after the release, so it runs before any later one reads a json value.
When it was released it rewrote SQL_ASCII databases only; u4v5w6x7y8z9 runs the same rewrite
again for the UTF8 installs that passed it.

Revision ID: s2t3u4v5w6x7
Revises: n9b0c1d2e3f4
Create Date: 2026-11-10
"""
from alembic import op

from celerp.migrations._json_compat import unescape_json_text

revision = "s2t3u4v5w6x7"
down_revision = "n9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    unescape_json_text(op.get_bind())


def downgrade() -> None:
    # UTF-8 text and the escapes are one JSON value: nothing to put back.
    pass
