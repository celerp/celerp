# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from celerp.config import settings

# Pool budget: total_possible = (api_workers * (pool_size + max_overflow))
#                              + (gui_workers * (gui_pool_size + gui_max_overflow))
# Default workers 2 API + 1 GUI → 2*(10+5) + 1*(5+5) = 40 connections max.
# Postgres default max_connections=100 leaves 60 for migrations, admin tools, etc.
# If you increase worker counts, recalculate this budget before deploying.
#
# SQLite (used in tests and Electron) does not support QueuePool args - it uses
# StaticPool. Pool kwargs are only passed for Postgres/asyncpg.
_is_postgres = settings.database_url.startswith("postgresql")
_pool_kwargs: dict = {"pool_size": 10, "max_overflow": 5} if _is_postgres else {}

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
        **_pool_kwargs,
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
