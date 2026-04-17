# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Integration test conftest — uses the populated dev.db (SQLite).

Truth anchors are defined ONCE here. Tests import them from conftest via fixtures.
The dev.db must be pre-populated by the importer before running these tests.
Run the importer first with:
  DATABASE_URL=sqlite+aiosqlite:///./dev.db .venv/bin/python -m celerp.importers.importer ...

CRITICAL: tests MUST NOT mutate dev.db.
- Truth anchor tests use `api` fixture: fresh read-only copy of dev.db per session.
- Journey tests use `journey_api` fixture: separate writable copy of dev.db per session.
"""
from __future__ import annotations

import os

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from celerp.db import get_session
from celerp.main import app

# ── Truth Anchors — single source of truth (full 2019-2026 history) ──────────
ITEM_COUNT = 4416
COST_TOTAL = 2_559_729.75
WHOLESALE_TOTAL = 9_798_289.57
RETAIL_TOTAL = 19_134_279.60
INVOICE_COUNT_NON_VOID = 1882
AR_GROSS = 17_020_113.29
AR_OUTSTANDING = 2_568_778.73
CONTACT_COUNT = 664
MEMO_TOTAL = 601_852.60
TOLERANCE = 1.00  # within $1.00

# ── Import credentials (company registered before import) ─────────────────────
IMPORT_EMAIL = "admin@demo.test"
IMPORT_PASSWORD = "demo-password"

# ── DB path ───────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent.parent  # celerp/core/
_DEV_DB = _REPO_ROOT / "dev.db"


def _require_anchors():
    if os.getenv("CELERP_RUN_INTEGRATION_ANCHORS") != "1":
        pytest.skip("Set CELERP_RUN_INTEGRATION_ANCHORS=1 to run anchored integration tests")
    if not _DEV_DB.exists():
        pytest.skip(f"dev.db not found at {_DEV_DB} — run the importer first")


async def _make_api_client(db_path: Path) -> AsyncClient:
    """Create an authenticated ASGI test client backed by the given DB path."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()

    async def _get_session():
        yield session

    app.dependency_overrides[get_session] = _get_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        login = await c.post("/auth/login", json={"email": IMPORT_EMAIL, "password": IMPORT_PASSWORD})
        assert login.status_code == 200, f"Login failed: {login.text}"
        token = login.json()["access_token"]

    return engine, session, {"Authorization": f"Bearer {token}"}


# ── Truth anchor fixtures (read-only copy of dev.db) ────────────────────────

@pytest.fixture(scope="session")
def truth_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_anchors()
    dst = tmp_path_factory.mktemp("truth") / "truth.db"
    shutil.copy(_DEV_DB, dst)
    return dst


@pytest_asyncio.fixture(scope="session")
async def dev_db_engine(truth_db: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{truth_db}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def dev_db_session(dev_db_engine):
    factory = async_sessionmaker(dev_db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture(scope="session")
async def api(dev_db_session: AsyncSession):
    """Session-scoped client for truth-anchor tests (read-only dev.db copy)."""

    async def _get_dev_session():
        yield dev_db_session

    app.dependency_overrides[get_session] = _get_dev_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/auth/login", json={"email": IMPORT_EMAIL, "password": IMPORT_PASSWORD})
        assert login.status_code == 200, f"Login failed: {login.text}"
        token = login.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        yield client

    app.dependency_overrides.pop(get_session, None)


# ── Journey fixtures (separate writable copy of dev.db) ──────────────────────

@pytest.fixture(scope="session")
def journey_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _require_anchors()
    dst = tmp_path_factory.mktemp("journey") / "journey.db"
    shutil.copy(_DEV_DB, dst)
    return dst


@pytest_asyncio.fixture(scope="session")
async def journey_db_engine(journey_db: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{journey_db}")
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def journey_db_session(journey_db_engine):
    factory = async_sessionmaker(journey_db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture(scope="session")
async def journey_api(journey_db_session: AsyncSession):
    """Session-scoped client for journey tests (writable dev.db copy)."""

    async def _get_journey_session():
        yield journey_db_session

    app.dependency_overrides[get_session] = _get_journey_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/auth/login", json={"email": IMPORT_EMAIL, "password": IMPORT_PASSWORD})
        assert login.status_code == 200, f"Login failed: {login.text}"
        token = login.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        yield client

    app.dependency_overrides.pop(get_session, None)

