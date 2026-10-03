# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""An older Celerp refuses a database a newer one has already upgraded, before it
changes anything: the migrate path (celerp migrate, celerp start, restore
reconcile) and the API's own startup."""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from celerp.db_url import sync_url

DATABASE_URL = os.environ.get("DATABASE_URL", "")
UNKNOWN = "ffff00c0ffee"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="needs a live Postgres database"
)


@pytest.fixture()
def newer_db(monkeypatch):
    """A scratch database with the current schema, stamped with a revision from a
    future Celerp. Yields its async URL; dropped afterwards. The migrate path sets
    DATABASE_URL to the database it migrates, so it is restored afterwards."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    from celerp.models.base import Base
    import celerp.models  # noqa: F401  (registers every table)

    name = f"newer_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(sync_url(DATABASE_URL), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    base, _, _ = DATABASE_URL.rpartition("/")
    url = f"{base}/{name}"
    engine = sa.create_engine(sync_url(url))
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": UNKNOWN})
        yield url
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _stamp(url: str) -> list[str]:
    engine = sa.create_engine(sync_url(url))
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        engine.dispose()


def test_migrate_refuses_a_database_a_newer_celerp_upgraded(newer_db):
    from celerp.cli import _apply_migrations
    from celerp.migrations._newer_schema import NewerSchemaError

    with pytest.raises(NewerSchemaError) as exc:
        _apply_migrations(newer_db)
    assert str(exc.value).startswith(
        "This database was last used by a newer version of Celerp than this one ("
    )
    assert "Nothing was changed. Install the latest version of Celerp to open it." in str(exc.value)
    assert UNKNOWN in str(exc.value)
    assert _stamp(newer_db) == [UNKNOWN]


def test_celerp_migrate_exits_with_the_plain_message(newer_db):
    from click.testing import CliRunner
    from celerp.cli import main

    result = CliRunner().invoke(main, ["migrate", "--db-url", newer_db])
    assert result.exit_code == 1
    assert "This database was last used by a newer version of Celerp" in result.output
    assert _stamp(newer_db) == [UNKNOWN]


def test_api_startup_refuses_a_database_a_newer_celerp_upgraded(newer_db, monkeypatch, capsys):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    import celerp.main as app_main

    engine = create_async_engine(newer_db, poolclass=NullPool)
    monkeypatch.setattr(app_main, "lifecycle_engine", engine)

    async def boot():
        async with app_main.lifespan(app_main.app):
            pass

    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(boot())
    finally:
        asyncio.run(engine.dispose())
    assert exc.value.code == 1
    assert "This database was last used by a newer version of Celerp" in capsys.readouterr().err
    assert _stamp(newer_db) == [UNKNOWN]


def test_a_known_stamp_is_not_refused():
    from celerp.migrations._newer_schema import _known_revisions, unknown_revisions

    head = next(iter(_known_revisions()))
    engine = sa.create_engine(sync_url(DATABASE_URL))
    try:
        with engine.connect() as conn, conn.begin() as tx:
            conn.execute(sa.text("CREATE TEMP TABLE alembic_version (version_num varchar(32))"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
            assert unknown_revisions(conn) == []
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": UNKNOWN})
            assert unknown_revisions(conn) == [UNKNOWN]
            tx.rollback()
    finally:
        engine.dispose()
