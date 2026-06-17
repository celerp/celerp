# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for system router — factory reset endpoint."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

import celerp.gateway.state as gw_state
from celerp.db import get_session
from celerp.main import app

# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def owner_jwt(session: AsyncSession) -> str:
    """Register a company and return an owner JWT."""
    import secrets
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "TestCo", "email": "owner@test.com",
            "name": "Owner", "password": "pw",
        })
        r = await c.post(
            "/auth/login",
            json={"email": "owner@test.com", "password": "pw"},
            headers={"X-Session-Token": token},
        )
        yield r.json()["access_token"], token

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


@pytest_asyncio.fixture
async def admin_jwt(session: AsyncSession) -> str:
    """Register a company as owner, create an admin, return admin JWT."""
    import secrets
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    app.dependency_overrides[get_session] = lambda: session
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    token = secrets.token_hex(32)
    gw_state.set_session_token(token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/auth/register", json={
            "company_name": "TestCo2", "email": "owner2@test.com",
            "name": "Owner2", "password": "pw",
        })
        r_owner = await c.post(
            "/auth/login",
            json={"email": "owner2@test.com", "password": "pw"},
            headers={"X-Session-Token": token},
        )
        owner_jwt = r_owner.json()["access_token"]
        await c.post(
            "/companies/me/users",
            json={"email": "admin@test.com", "name": "Admin", "role": "admin", "password": "pw"},
            headers={"Authorization": f"Bearer {owner_jwt}", "X-Session-Token": token},
        )
        r = await c.post(
            "/auth/login",
            json={"email": "admin@test.com", "password": "pw"},
            headers={"X-Session-Token": token},
        )
        yield r.json()["access_token"], token

    app.dependency_overrides.clear()
    gw_state.set_session_token("")


def _mock_session():
    """Return a mock AsyncSession that no-ops all SQL (avoids SQLite/TRUNCATE issues)."""
    sess = AsyncMock(spec=AsyncSession)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=None)
    sess.begin.return_value = cm
    sess.execute = AsyncMock(return_value=MagicMock())
    return sess


class TestFactoryReset:
    @pytest.mark.asyncio
    async def test_factory_reset_unauthenticated(self):
        """No token → 401/403."""
        mock_sess = _mock_session()
        app.dependency_overrides[get_session] = lambda: mock_sess
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/system/factory-reset")
            assert r.status_code in (401, 403)
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_factory_reset_non_owner_forbidden(self, admin_jwt):
        """Admin role → 403 (below owner threshold)."""
        jwt, token = admin_jwt
        mock_sess = _mock_session()
        app.dependency_overrides[get_session] = lambda: mock_sess
        gw_state.set_session_token(token)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/system/factory-reset",
                    headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()
            gw_state.set_session_token("")

    @pytest.mark.asyncio
    async def test_factory_reset_owner_succeeds(self, owner_jwt):
        """Owner → 200 {"ok": True}."""
        jwt, token = owner_jwt
        mock_sess = _mock_session()
        app.dependency_overrides[get_session] = lambda: mock_sess
        gw_state.set_session_token(token)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/system/factory-reset",
                    headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
                )
            assert r.status_code == 200
            assert r.json() == {"ok": True}
        finally:
            app.dependency_overrides.clear()
            gw_state.set_session_token("")

    @pytest.mark.asyncio
    async def test_factory_reset_wipes_data(self, owner_jwt):
        """All expected DELETE/TRUNCATE calls are executed."""
        jwt, token = owner_jwt
        mock_sess = _mock_session()
        app.dependency_overrides[get_session] = lambda: mock_sess
        gw_state.set_session_token(token)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/system/factory-reset",
                    headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
                )
            assert r.status_code == 200
            # Verify execute was called for DELETE users, companies, user_companies, locations
            calls = [str(call.args[0]) if call.args else "" for call in mock_sess.execute.call_args_list]
            assert any("DELETE FROM users" in c for c in calls)
            assert any("DELETE FROM companies" in c for c in calls)
            assert any("DELETE FROM user_companies" in c for c in calls)
            assert any("DELETE FROM locations" in c for c in calls)
        finally:
            app.dependency_overrides.clear()
            gw_state.set_session_token("")

    @pytest.mark.asyncio
    async def test_factory_reset_idempotent(self, owner_jwt):
        """Calling factory-reset twice with the same (still-valid) token returns 200 both times."""
        jwt, token = owner_jwt

        async def _make_mock():
            return _mock_session()

        for _ in range(2):
            mock_sess = _mock_session()
            app.dependency_overrides[get_session] = lambda s=mock_sess: s
            gw_state.set_session_token(token)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/system/factory-reset",
                    headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
                )
            assert r.status_code == 200
        app.dependency_overrides.clear()
        gw_state.set_session_token("")

    @pytest.mark.asyncio
    async def test_factory_reset_deletes_attachments(self, owner_jwt, tmp_path):
        """When an attachment directory exists, it is removed post-commit."""
        import celerp.routers.system as sys_mod
        jwt, token = owner_jwt

        mock_sess = _mock_session()
        app.dependency_overrides[get_session] = lambda: mock_sess
        gw_state.set_session_token(token)

        # Decode company_id from JWT payload to create matching dir
        import base64, json as _json
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        company_id = _json.loads(base64.urlsafe_b64decode(payload_b64))["company_id"]

        att_dir = tmp_path / company_id
        att_dir.mkdir()
        (att_dir / "file.txt").write_text("data")

        try:
            with patch.object(sys_mod, "_ATTACHMENT_ROOT", tmp_path):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                    r = await c.post(
                        "/system/factory-reset",
                        headers={"Authorization": f"Bearer {jwt}", "X-Session-Token": token},
                    )
            assert r.status_code == 200
            assert not att_dir.exists(), "Attachment directory should have been deleted"
        finally:
            app.dependency_overrides.clear()
            gw_state.set_session_token("")
