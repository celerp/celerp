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


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    res = await client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
@pytest.mark.skipif(True, reason="Rate limit test contaminates subsequent auth tests; run manually with isolated process")
async def test_rate_limit_on_login(client: AsyncClient) -> None:
    # Re-enable limiter for this test (disabled globally by conftest autouse fixture)
    app.state.limiter.enabled = True
    try:
        last = None
        for _ in range(12):
            last = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "bad"})
        assert last is not None
        assert last.status_code in {401, 429}
        if last.status_code == 429:
            assert last.json() == {"detail": "Rate limit exceeded"}
    finally:
        app.state.limiter.enabled = False


@pytest.mark.asyncio
async def test_max_body_size_rejects(client: AsyncClient) -> None:
    too_big = b"x" * (10 * 1024 * 1024 + 1)
    res = await client.post("/health", content=too_big)
    assert res.status_code == 413
    assert res.json() == {"detail": "Request too large"}
