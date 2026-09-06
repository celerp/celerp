# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

import os
import re
from contextlib import asynccontextmanager

from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from celerp.capacity import REQUEST_DB_MAX_OVERFLOW, REQUEST_DB_POOL_SIZE
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

# Request-connection timeouts (ms). One source of truth: the engine sets them in
# server_settings, and lifecycle_timeouts_disabled restores exactly these values
# after clearing them for startup work.
_REQUEST_LOCK_TIMEOUT_MS = "3000"
_REQUEST_STATEMENT_TIMEOUT_MS = "30000"

# Pool budget: the app runs one API worker (both `celerp start` and the Electron
# shell launch uvicorn without --workers), so this single process owns one request
# pool of REQUEST_DB_POOL_SIZE base connections plus REQUEST_DB_MAX_OVERFLOW under
# burst. The base size is also the web UI's interactive connection ceiling, kept in
# lockstep through celerp.capacity so the two never drift. Postgres default
# max_connections=100 leaves ample headroom for migrations, admin tools, and
# background jobs beyond this pool.
if os.environ.get("CELERP_TEST_NULLPOOL"):
    # Test mode: a fresh connection per use that closes on return, so a failed
    # test can't leave a poisoned/locked connection lingering in a pooled
    # connection and block the next test's TRUNCATE. Only ever set by the test
    # harness; no effect in production.
    engine = create_async_engine(settings.database_url, future=True, poolclass=NullPool)
else:
    engine = create_async_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=REQUEST_DB_POOL_SIZE,
        max_overflow=REQUEST_DB_MAX_OVERFLOW,
        # Bound how long any query may wait on a lock or run, so a stuck query is
        # cancelled instead of pinning one of the few pooled connections for the
        # full request lifetime (lock_timeout 3s, statement_timeout 30s, in ms).
        # This is a per-connection default, so it applies to every request AND
        # every pooled background job (connector syncs, the gateway, the daily
        # scheduler) - all of which must stay bounded. Startup reconciliation and
        # the projection rebuild are the only work that legitimately runs longer;
        # they use lifecycle_engine below instead of this pool, and the migration
        # advisory-lock wait self-exempts via lifecycle_timeouts_disabled.
        connect_args={
            "server_settings": {
                "lock_timeout": _REQUEST_LOCK_TIMEOUT_MS,
                "statement_timeout": _REQUEST_STATEMENT_TIMEOUT_MS,
            }
        },
    )
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Lifecycle/maintenance engine: a separate NullPool engine carrying NO statement
# or lock timeout. Startup reconciliation (create_all, module on_modules_ready
# seed hooks, the develop->release projection rebuild, the one-time status-doc and
# COGS backfills) and the on-demand projection rebuild routes run whole-table
# DELETEs and full-ledger replays that can legitimately exceed the request
# statement_timeout on a mature database. Running them on the bounded request pool
# would cancel them at 30s - permanently blocking boot's upgrade guard and the
# /ledger/rebuild recovery route. They run on this engine instead. NullPool: these
# operations are rare and brief, so no pooled slot is reserved and the request pool
# budget above is unaffected (at most one transient connection per concurrent
# lifecycle op).
lifecycle_engine = create_async_engine(settings.database_url, future=True, poolclass=NullPool)
LifecycleSessionLocal = async_sessionmaker(
    lifecycle_engine, class_=AsyncSession, expire_on_commit=False
)


@asynccontextmanager
async def lifecycle_timeouts_disabled(conn):
    """Clear the request statement/lock timeouts on the migration lock connection.

    The migration advisory-lock wait runs on a connection drawn from the bounded
    request engine (so a single engine serialises boots), but a second worker's
    wait can legitimately exceed the 30s statement_timeout while the first worker
    migrates, and a cancelled wait aborts the boot. This sets both timeouts to 0
    (unbounded) for the block and restores the request defaults on exit, so the
    connection returned to the pool still carries them. Other long-running startup
    work uses lifecycle_engine (no timeout) rather than this in-place exemption.
    Postgres only; a no-op on other dialects, which have no such settings.
    """
    if conn.dialect.name != "postgresql":
        yield
        return
    await conn.execute(_sql_text("SET statement_timeout = 0"))
    await conn.execute(_sql_text("SET lock_timeout = 0"))
    try:
        yield
    finally:
        await conn.execute(_sql_text(f"SET statement_timeout = {_REQUEST_STATEMENT_TIMEOUT_MS}"))
        await conn.execute(_sql_text(f"SET lock_timeout = {_REQUEST_LOCK_TIMEOUT_MS}"))


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def get_session_ctx():
    """Standalone async context manager for use outside FastAPI request handlers."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def get_lifecycle_session_ctx():
    """Session on the unbounded lifecycle_engine, for known long-running work.

    Use for the projection rebuild and startup backfills: a full rebuild's
    whole-table DELETE and full-ledger replay can exceed the request
    statement_timeout on a mature database, and must not be cancelled. Ordinary
    request and background work uses get_session/get_session_ctx (bounded).
    """
    async with LifecycleSessionLocal() as session:
        yield session
