# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Clear the active-JTI registry once, after the token v2 version cutover.

Revision ID: f3a4b5c6d7e8
Revises: d1e2f3a4b5c6
Create Date: 2026-09-15

Context
-------
The ``auth_ver=2`` boundary already rejects every pre-cutover access and refresh
token by format version, so their JTI rows in ``session_registry`` name sessions
that can never authenticate again. Left in place they only hold false occupancy
against ``direct_connection_limit`` until the old access TTL elapses. This one-time
data migration deletes those dead rows; live post-cutover tokens re-register their
own JTIs on the next request.

It changes no schema. Deleted rows cannot be recovered, so downgrade is a no-op
rather than a false restore.
"""

from __future__ import annotations

from alembic import op


revision = "f3a4b5c6d7e8"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM session_registry")


def downgrade() -> None:
    # The deleted rows named already-invalid pre-cutover sessions; there is
    # nothing to restore.
    pass
