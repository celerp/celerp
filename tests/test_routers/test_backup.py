# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp-backup module routes.

Covers:
  - Router registration: /backup/* routes are present on the app (regression for
    ImportError-silenced path bug — routes must be registered at startup, not
    conditionally on sys.path state)
  - POST /backup/trigger: database + files success/failure + invalid type
  - GET  /backup/list: no session (empty state), relay success (HTML + JSON), relay error
  - POST /backup/restore: success + failure
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
    await _clear_tracker(session)
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
    assert r.headers.get("HX-Trigger") == "backupDone"


@pytest.mark.asyncio
async def test_trigger_db_failure(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup.run_backup",
        lambda **kw: _async_return(BackupResult(ok=False, size_bytes=0, error="pg_dump not found")),
    )
    r = await auth_client.post("/backup/trigger?type=database")
    assert r.status_code == 422, f"Expected 422 so UI can show real error. Got {r.status_code}: {r.text[:100]}"
    assert "pg_dump" in r.json()["detail"]
    # No HX-Trigger on failure
    assert "HX-Trigger" not in r.headers


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
    assert r.headers.get("HX-Trigger") == "backupDone"


@pytest.mark.asyncio
async def test_trigger_invalid_type(auth_client):
    r = await auth_client.post("/backup/trigger?type=invalid")
    assert r.status_code == 400


# ── GET /backup/list ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_no_session_token_htmx(auth_client, monkeypatch):
    """When relay not connected, HTMX request gets empty-state HTML."""
    monkeypatch.setattr(gw_state, "get_session_token", lambda: "")
    r = await auth_client.get("/backup/list", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "empty-state-msg" in r.text


@pytest.mark.asyncio
async def test_list_no_session_token_json(auth_client, monkeypatch):
    """When relay not connected, JSON request gets empty items list."""
    monkeypatch.setattr(gw_state, "get_session_token", lambda: "")
    r = await auth_client.get("/backup/list")
    assert r.status_code == 200
    assert r.json() == {"items": []}


@pytest.mark.asyncio
async def test_list_htmx_with_items(auth_client, monkeypatch):
    """HTMX request returns rendered table when relay returns items."""
    import httpx as _httpx
    import respx

    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")

    items = [
        {"id": "bkp-1", "created_at": "2026-05-01T10:00:00Z", "size_bytes": 1048576, "label": "daily"},
        {"id": "bkp-2", "created_at": "2026-05-02T12:00:00Z", "size_bytes": 512000, "label": None},
    ]

    with respx.mock:
        respx.get("https://relay.test.com/backup/").mock(
            return_value=_httpx.Response(200, json={"items": items})
        )
        r = await auth_client.get("/backup/list", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "data-table" in r.text
    assert "bkp-1" in r.text or "May 01" in r.text
    assert "1.0 MB" in r.text


@pytest.mark.asyncio
async def test_list_htmx_empty_items(auth_client, monkeypatch):
    """HTMX request returns empty-state when relay returns no items."""
    import httpx as _httpx
    import respx

    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")

    with respx.mock:
        respx.get("https://relay.test.com/backup/").mock(
            return_value=_httpx.Response(200, json={"items": []})
        )
        r = await auth_client.get("/backup/list", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert "empty-state-msg" in r.text


@pytest.mark.asyncio
async def test_list_json(auth_client, monkeypatch):
    """Non-HTMX request returns raw JSON from relay."""
    import httpx as _httpx
    import respx

    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")

    items = [{"id": "bkp-1", "created_at": "2026-05-01T10:00:00Z", "size_bytes": 100, "label": "x"}]
    with respx.mock:
        respx.get("https://relay.test.com/backup/").mock(
            return_value=_httpx.Response(200, json={"items": items})
        )
        r = await auth_client.get("/backup/list")

    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == "bkp-1"


@pytest.mark.asyncio
async def test_list_relay_error(auth_client, monkeypatch):
    """Relay non-200 response raises 502."""
    import httpx as _httpx
    import respx

    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")

    with respx.mock:
        respx.get("https://relay.test.com/backup/").mock(
            return_value=_httpx.Response(503, text="Service Unavailable")
        )
        r = await auth_client.get("/backup/list")

    assert r.status_code == 503


# ── POST /backup/restore ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_success(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup.run_restore",
        lambda bid: _async_return(BackupResult(ok=True, size_bytes=5000)),
    )
    r = await auth_client.post("/backup/restore/bkp-123")
    assert r.status_code == 200
    assert "flash--success" in r.text
    assert "flash--error" not in r.text


@pytest.mark.asyncio
async def test_restore_failure(auth_client, monkeypatch):
    from celerp.services.backup import BackupResult
    monkeypatch.setattr(
        "celerp.services.backup.run_restore",
        lambda bid: _async_return(BackupResult(ok=False, size_bytes=0, error="Decrypt failed")),
    )
    r = await auth_client.post("/backup/restore/bkp-bad")
    assert r.status_code == 200
    assert "flash--error" in r.text
    assert "Decrypt failed" in r.text


# ── Router registration (regression: ImportError must not silence registration) ─

def test_backup_routes_are_registered():
    """Regression: /backup/* routes must be present on the app.

    Previously a module-level try/except ImportError in main.py silently ate
    the ModuleNotFoundError for celerp_backup (which is only importable after
    the module loader patches sys.path during lifespan startup). This caused
    all /backup/* requests to return 404 in production.
    """
    registered = {route.path for route in app.routes}
    assert "/backup/trigger" in registered, "/backup/trigger not registered"
    assert "/backup/list" in registered, "/backup/list not registered"
    assert "/backup/restore/{backup_id}" in registered, "/backup/restore/{backup_id} not registered"
    assert "/backup/export" in registered, "/backup/export not registered"
    assert "/backup/export/{backup_id}" in registered, "/backup/export/{backup_id} not registered"
    assert "/backup/import" in registered, "/backup/import not registered"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _async_return(value):
    return value
