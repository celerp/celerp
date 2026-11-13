# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Lookup indexes a migration builds without blocking writes (CREATE INDEX CONCURRENTLY).

A lookup index never stops Celerp starting. Each index is made valid on its own: a valid index
of its name is kept (a start of the new version may have built it from the models already), an
INVALID one, what a cancelled or failed concurrent build leaves, is dropped and built again, and
a build that fails or leaves the index invalid drops only that index. An index is dropped only
once it is known to be invalid or its build failed: when its validity cannot be read, it is left
as it is. Lookups read without a missing index (slower, same rows) until the next start finds it
missing and builds it.
"""

import logging

from alembic import op
import sqlalchemy as sa

log = logging.getLogger(__name__)

_VALID = sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")


def _valid(name: str) -> bool | None:
    """True for a valid index *name*, False for an invalid one, None when there is none."""
    return op.get_bind().execute(_VALID, {"name": name}).scalar()


def build_lookup_index(name: str, table: str, build) -> bool:
    """Make *name* a valid index on *table*, inside an autocommit block, and answer whether it is.

    ``build`` runs the migration's ``op.create_index`` for *name* (written out in the migration,
    so the restamp walker proves the revision by it) when no valid index of that name exists.
    Never raises: a build that fails or leaves the index invalid is logged and the index dropped;
    a validity read that fails is logged and the index, whatever it is, left in place.
    """
    try:
        valid = _valid(name)
    except Exception as exc:
        return _unread(name, exc)
    try:
        if valid is True:
            return True
        if valid is False:
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
        build()
    except Exception as exc:
        return _drop_unfinished(name, table, repr(exc))
    try:
        valid = _valid(name)
    except Exception as exc:
        return _unread(name, exc)
    return True if valid is True else _drop_unfinished(name, table, "the build left it invalid")


def _unread(name: str, exc: Exception) -> bool:
    log.warning("Could not read whether index %s is valid (%r); it is left as it is", name, exc)
    return False


def _drop_unfinished(name: str, table: str, reason: str) -> bool:
    log.warning("Index %s was not built (%s); it is built again at the next start", name, reason)
    try:
        op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
    except Exception as exc:
        log.warning("Could not drop the unfinished index %s (%s)", name, exc)
    return False
