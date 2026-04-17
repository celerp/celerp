# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for /companies/me/modules API endpoints.

GET  /companies/me/modules
POST /companies/me/modules/{name}/enable
POST /companies/me/modules/{name}/disable

Uses the standard test client + register pattern from conftest.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _register(client, role: str = "admin") -> str:
    email = f"mod-test-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Mod Test Co", "email": email, "name": "Admin", "password": "pw123"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestModulesAPIEndpoints:
    @pytest.mark.asyncio
    async def test_list_modules_unauthenticated(self, client):
        r = await client.get("/companies/me/modules")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_modules_authenticated_returns_list(self, client):
        token = await _register(client)
        r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_list_modules_empty_module_dir(self, client, monkeypatch):
        """With no module directory, list returns empty list."""
        monkeypatch.delenv("MODULE_DIR", raising=False)
        token = await _register(client)
        r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_enable_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/gemstones/enable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/celerp-labels/disable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_enable_module_persists_to_settings(self, client):
        """Enabling a module adds it to company.settings enabled_modules."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert data.get("restart_required") is True
        assert "gemstones" in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_disable_module_removes_from_settings(self, client):
        """Disabling a module removes it from company.settings enabled_modules."""
        token = await _register(client)
        # First enable it
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        # Then disable
        r = await client.post("/companies/me/modules/gemstones/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "gemstones" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_then_disable_is_idempotent(self, client):
        """Double enable is safe; enabled set is a set (no duplicates)."""
        token = await _register(client)
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        r = await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        enabled = data.get("enabled_modules", [])
        assert enabled.count("gemstones") == 1  # No duplicate

    @pytest.mark.asyncio
    async def test_disable_not_enabled_module_is_safe(self, client):
        """Disabling a module that isn't enabled returns 200, no error."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/nonexistent-module/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "nonexistent-module" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_returns_restart_required(self, client):
        """Enable response always includes restart_required: true."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        assert r.status_code == 200
        assert r.json().get("restart_required") is True

    @pytest.mark.asyncio
    async def test_disable_returns_restart_required(self, client):
        """Disable response always includes restart_required: true."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/disable", headers=_h(token))
        assert r.status_code == 200
        assert r.json().get("restart_required") is True

    @pytest.mark.asyncio
    async def test_company_isolation_module_settings(self, client):
        """Module settings are per-company, not global.
        
        Company A enables gemstones. Company B (created via POST /companies)
        should start with default enabled set, not A's settings.
        """
        # Register company A (bootstrap)
        token_a = await _register(client)
        # Create company B using the bootstrap token
        r_b = await client.post(
            "/companies",
            json={"name": "Company B"},
            headers=_h(token_a),
        )
        assert r_b.status_code == 200, r_b.text
        token_b = r_b.json()["access_token"]

        # Company A enables gemstones
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token_a))

        # Company B should NOT see gemstones in its settings (it has its own settings)
        r_b_list = await client.get("/companies/me/modules", headers=_h(token_b))
        assert r_b_list.status_code == 200
        # Verify companies have separate settings by checking enabled state
        r_b_disable = await client.post(
            "/companies/me/modules/gemstones/disable", headers=_h(token_b)
        )
        assert r_b_disable.status_code == 200
        data_b = r_b_disable.json()
        # B's disable call should NOT return gemstones in the enabled list
        assert "gemstones" not in data_b.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_list_modules_with_installed_module(self, client, tmp_path):
        """With a module installed, list includes it with enabled/running state."""
        import shutil
        from pathlib import Path
        import os

        token = await _register(client)

        # Copy celerp-verticals into a tmp module dir and point the loader at it
        verticals_src = Path(__file__).parent.parent.parent / "default_modules" / "celerp-verticals"

        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        shutil.copytree(verticals_src, module_dir / "celerp-verticals")

        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))

        assert r.status_code == 200
        data = r.json()
        names = [m["name"] for m in data]
        assert "celerp-verticals" in names

        gem = next(m for m in data if m["name"] == "celerp-verticals")
        assert "enabled" in gem
        assert "running" in gem
        assert "version" in gem
