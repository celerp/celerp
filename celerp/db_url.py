"""Database URL for synchronous engines.

The app runs on asyncpg. Alembic and the CLI's maintenance engines run
synchronously on psycopg2, the driver the app ships. The driver is named
explicitly because a bare postgresql:// URL resolves to whatever driver the
installed SQLAlchemy defaults to, which is not necessarily one the app ships.
"""
from __future__ import annotations

import sqlalchemy as sa


def sync_url(db_url: str) -> str:
    """The configured Postgres URL with its driver set to psycopg2."""
    scheme, sep, rest = db_url.partition("://")
    if not sep or scheme.split("+", 1)[0] != "postgresql":
        return db_url
    return f"postgresql+psycopg2://{rest}"


def same_database(a: str | sa.engine.URL, b: str | sa.engine.URL) -> bool:
    """Whether two URLs name one database, whatever driver each names."""
    a, b = sa.engine.make_url(a), sa.engine.make_url(b)
    return ((a.host, a.port, a.database, dict(a.query))
            == (b.host, b.port, b.database, dict(b.query)))
