# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""API error handler tests — verifies clean JSON error responses."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_api_nonexistent_route_returns_json_detail(client):
    """GET /nonexistent-route → JSON with 'detail' key, not HTML traceback."""
    r = await client.get("/this-api-route-does-not-exist-at-all")
    # FastAPI returns 404 for unknown routes
    assert r.status_code == 404
    body = r.text
    assert "Traceback" not in body, "API returns Python traceback on 404"
    # Should be JSON with detail key
    data = r.json()
    assert "detail" in data, f"Expected 'detail' key in JSON, got: {data}"


@pytest.mark.asyncio
async def test_api_404_handler_returns_json(client):
    """Exception handler for 404 → {detail: ...}. Uses authenticated client."""
    # Register + get token first
    r = await client.post(
        "/auth/register",
        json={"company_name": "ErrCo", "email": "err@test.com", "name": "Err", "password": "pw"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]

    r = await client.get(
        "/items/item:this-does-not-exist-anywhere-99999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"
    data = r.json()
    assert "detail" in data, f"No 'detail' in JSON: {data}"
