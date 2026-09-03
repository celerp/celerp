# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

import os
import re
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from celerp.config import settings

# Shared advisory-lock key that serialises schema migrations across every process
# that might run them (the CLI upgrade command and the API startup phase). One
# key, one source of truth, so two boots can never migrate the same database at
# once.
_MIGRATION_LOCK_KEY = 4207320001

# Redacts the password in any Postgres URL: "://user:secret@host" -> "://user:***@host".
# One authoritative masker for logs and surfaced errors so a connection string
# never leaks a password.
_DB_CREDENTIALS_RE = re.compile(r"(://[^:]+:)[^@]+(@)")


def mask_db_credentials(text: str) -> str:
    """Return text with any database URL password replaced by ``***``."""
    return _DB_CREDENTIALS_RE.sub(r"\1***\2", text)

# Pool budget: total_possible = (api_workers * (pool_size + max_overflow))
#                              + (gui_workers * (gui_pool_size + gui_max_overflow))
# Default workers 2 API + 1 GUI → 2*(10+5) + 1*(5+5) = 40 connections max.
# Postgres default max_connections=100 leaves 60 for migrations, admin tools, etc.
# If you increase worker counts, recalculate this budget before deploying.
if os.environ.get("CELERP_TEST_NULLPOOL"):
    # Test mode: a fresh connection per use that closes on return, so a failed
    # test can't leave a poisoned/locked connection lingering in a pooled
    # connection and block the next test's TRUNCATE. Only ever set by the test
    # harness; no effect in production.
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.database_url, future=True, poolclass=NullPool)
else:
    engine = create_async_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
        # Bound how long any query may wait on a lock or run, so a stuck query is
        # cancelled instead of pinning one of the few pooled connections for the
        # full request lifetime. lock_timeout 3s, statement_timeout 30s (in ms).
        # Migrations run under a separate sync runner with their own SET LOCAL
        # timeouts, so this global bound never aborts a migration.
        connect_args={
            "server_settings": {
                "lock_timeout": "3000",
                "statement_timeout": "30000",
            }
        },
    )
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def get_session_ctx():
    """Standalone async context manager for use outside FastAPI request handlers."""
    async with SessionLocal() as session:
        yield session
