# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""On every encoding, every json value an earlier version stored with characters past ASCII
written as \\u escapes is rewritten to hold them as UTF-8 text (unescape_json_text). s2t3u4v5w6x7
did so on SQL_ASCII only, so a UTF8 install kept the escapes and its backup could not be restored
into a SQL_ASCII install. Escapes with no text form (NUL, a lone surrogate) stay as they are, so
such a row reads as before.

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-11-11
"""
from alembic import op

from celerp.migrations._json_compat import unescape_json_text

revision = "u4v5w6x7y8z9"
down_revision = "t3u4v5w6x7y8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    unescape_json_text(op.get_bind())


def downgrade() -> None:
    # UTF-8 text and the escapes are one JSON value: nothing to put back.
    pass
