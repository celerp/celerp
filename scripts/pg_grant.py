#!/usr/bin/env python3
"""
Grant the required PostgreSQL privileges to the Celerp app user.

Run this ONCE after `alembic upgrade head`, as a PostgreSQL superuser:

    python scripts/pg_grant.py --db-url postgresql://superuser:pass@host/celerp --app-user celerp_user

The Celerp app user needs:
  - SELECT/INSERT/UPDATE/DELETE on all tables
  - USAGE/SELECT on all sequences (required for INSERT on tables with serial PKs)

These grants cover existing objects AND set default privileges for future
objects created by subsequent migrations.
"""

import argparse
import asyncio

import asyncpg


async def grant(db_url: str, app_user: str) -> None:
    # asyncpg uses postgres:// scheme
    conn_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    conn = await asyncpg.connect(conn_url)
    try:
        # Existing objects
        await conn.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_user};"
        )
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_user};"
        )
        # Future objects (created by migrations run as superuser)
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_user};"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_user};"
        )
        print(f"Grants applied for user '{app_user}'.")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Grant Celerp DB privileges.")
    parser.add_argument(
        "--db-url",
        required=True,
        help="Superuser DATABASE_URL (postgresql[+asyncpg]://...)",
    )
    parser.add_argument(
        "--app-user",
        required=True,
        help="PostgreSQL user that Celerp connects as",
    )
    args = parser.parse_args()
    asyncio.run(grant(args.db_url, args.app_user))


if __name__ == "__main__":
    main()
