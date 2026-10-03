# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Refuse a database a newer Celerp has already upgraded.

A database stamped with an alembic revision this copy's migration scripts do not
contain was last used by a newer Celerp. Opening it would be a silent downgrade:
the stamp repair walker would restamp it back to this copy's head and the old
code would then read and write a schema it does not know. "Unknown" is decided
by this copy's alembic script directory, never by version strings, so it holds
for develop builds and for revisions that arrive out of order.
"""
from __future__ import annotations

import functools

import sqlalchemy as sa


class NewerSchemaError(RuntimeError):
    """The database was last used by a newer Celerp; nothing was changed."""


@functools.lru_cache(maxsize=1)
def _known_revisions() -> frozenset[str]:
    from alembic.script import ScriptDirectory
    from celerp.alembic_config import build_alembic_config
    script = ScriptDirectory.from_config(build_alembic_config())
    return frozenset(rev.revision for rev in script.walk_revisions())


def unknown_revisions(conn: sa.Connection) -> list[str]:
    """The revisions *conn*'s database is stamped with that this copy does not know."""
    if not sa.inspect(conn).has_table("alembic_version"):
        return []
    stamped = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    known = _known_revisions()
    return sorted(rev for rev in stamped if rev not in known)


def refuse_newer_schema(conn: sa.Connection) -> None:
    """Raise NewerSchemaError when *conn*'s database was last used by a newer Celerp."""
    unknown = unknown_revisions(conn)
    if not unknown:
        return
    from celerp import __version__
    raise NewerSchemaError(
        f"This database was last used by a newer version of Celerp than this one "
        f"({__version__}). Nothing was changed. Install the latest version of Celerp "
        f"to open it. (Database revision: {', '.join(unknown)})"
    )
