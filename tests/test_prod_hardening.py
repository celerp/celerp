# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

import os

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import AsyncClient

from celerp.main import app
from ui.i18n import t


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    res = await client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_health_version_prefers_app_version_env(monkeypatch) -> None:
    """C2: /health reports the desktop app version (CELERP_APP_VERSION, set by Electron)
    when present, else the Python package version - so the cloud-relay / PC-browser path
    (which reads /health) shows the same version as the desktop, not the pip version."""
    from celerp.routers import health as health_mod

    monkeypatch.setenv("CELERP_APP_VERSION", "9.9.9-desktop")
    assert (await health_mod.health())["version"] == "9.9.9-desktop"

    monkeypatch.delenv("CELERP_APP_VERSION", raising=False)
    assert (await health_mod.health())["version"] == health_mod.__version__


@pytest.mark.asyncio
async def test_rate_limit_on_login(client: AsyncClient) -> None:
    # /auth/login checks passwords through celerp.services.auth.password_attempts: past five
    # failures on one account the next try waits, told at once. The autouse
    # _disable_rate_limits fixture starts every test with no failures counted.
    codes = [(await client.post("/auth/login", json={"email": "nobody@example.com", "password": "bad"})).status_code
             for _ in range(5)]
    last = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "bad"})
    assert codes == [401] * 5
    assert last.status_code == 429
    assert last.headers["Retry-After"] == "1"
    assert last.json() == {"detail": t("auth.sign_ins_paused_one")}


@pytest.mark.asyncio
async def test_max_body_size_rejects(client: AsyncClient) -> None:
    too_big = b"x" * (10 * 1024 * 1024 + 1)
    res = await client.post("/health", content=too_big)
    assert res.status_code == 413
    assert res.json() == {"detail": t("error.upload_too_large", "en")}
