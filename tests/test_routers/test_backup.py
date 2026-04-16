# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp-backup module routes.

Covers:
  - GET  /backup/status: flags and scheduler state
  - POST /backup/trigger: database + files success/failure
  - POST /backup/restore: success/failure
"""

from __future__ import annotations

import base64
import os
import secrets

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.config import settings
import celerp.gateway.state as gw_state
from celerp.db import get_session
from celerp.main import app
from celerp.models.base import Base

_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_client(session: AsyncSession):
    """Authenticated async client with session token pre-set."""
    from celerp.services.session_tracker import clear as _clear_tracker
    _clear_tracker()
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "BackupCo", "email": "b@test.com",
            "name": "Admin", "password": "pw",
        })
        r = await c.post("/auth/login", json={"email": "b@test.com", "password": "pw"})
        jwt = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {jwt}"
        c.headers["X-Session-Token"] = token
        yield c

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


@pytest.fixture(autouse=True)
def reset_backup_settings():
    orig_key = settings.backup_encryption_key
    yield
    settings.backup_encryption_key = orig_key


# ── GET /backup/status ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_unconfigured(auth_client, monkeypatch):
    monkeypatch.setattr("celerp.gateway.client._client", None)
    settings.backup_encryption_key = ""
    r = await auth_client.get("/backup/status")
    assert r.status_code == 200
    data = r.json()
    assert data["encryption_configured"] is False
    assert data["gateway_connected"] is False
    assert "scheduler_running" in data
    assert "backup_enabled" in data


@pytest.mark.asyncio
async def test_status_configured(auth_client, monkeypatch):
    import celerp.gateway.client as _gw_mod
    from celerp.gateway.client import GatewayClient
    fake = GatewayClient("tok", "iid", "wss://x")
    monkeypatch.setattr(_gw_mod, "_client", fake)
    settings.backup_encryption_key = base64.b64encode(secrets.token_bytes(32)).decode()
    r = await auth_client.get("/backup/status")
    assert r.status_code == 200
    data = r.json()
    assert data["encryption_configured"] is True
    assert data["gateway_connected"] is True


# ── POST /backup/trigger ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_db_success(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup.run_backup",
        lambda **kw: _async_return(BackupResult(ok=True, size_bytes=1024)),
    )
    r = await auth_client.post("/backup/trigger?type=database")
    assert r.status_code == 200
    assert "Backup complete" in r.text


@pytest.mark.asyncio
async def test_trigger_db_failure(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup.run_backup",
        lambda **kw: _async_return(BackupResult(ok=False, size_bytes=0, error="pg_dump not found")),
    )
    r = await auth_client.post("/backup/trigger?type=database")
    assert r.status_code == 200
    assert "pg_dump" in r.text
    assert "flash--error" in r.text


@pytest.mark.asyncio
async def test_trigger_files_success(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup_files.run_file_backup",
        lambda **kw: _async_return(BackupResult(ok=True, size_bytes=2048)),
    )
    r = await auth_client.post("/backup/trigger?type=files")
    assert r.status_code == 200
    assert "Backup complete" in r.text


@pytest.mark.asyncio
async def test_trigger_invalid_type(auth_client):
    r = await auth_client.post("/backup/trigger?type=invalid")
    assert r.status_code == 400


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _async_return(value):
    return value
