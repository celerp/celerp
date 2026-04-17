# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for security headers middleware and cookie_secure setting."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_security_headers_present_on_api_response(client):
    """Security headers must appear on every API response."""
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src" in csp
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "permissions-policy" in r.headers


@pytest.mark.asyncio
async def test_security_headers_on_404(client):
    """Security headers must be present even on 404 responses."""
    r = await client.get("/nonexistent-route-xyz")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


@pytest.mark.asyncio
async def test_csp_blocks_external_scripts(client):
    """CSP must not allow wildcard external script sources."""
    r = await client.get("/health")
    csp = r.headers.get("content-security-policy", "")
    # Must not have wildcard or http(s): in script-src
    assert "script-src *" not in csp
    assert "script-src https:" not in csp


@pytest.mark.asyncio
async def test_cookie_secure_false_allows_login(client):
    """cookie_secure=False (default for tests) must not break login flow."""
    # Register first
    await client.post(
        "/auth/register",
        json={"company_name": "SecCo", "email": "sec@example.com", "name": "Admin", "password": "pass1234"},
    )
    r = await client.post("/auth/login", json={"email": "sec@example.com", "password": "pass1234"})
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_security_headers_on_auth_endpoint(client):
    """Security headers must appear on /auth/login responses."""
    r = await client.post("/auth/login", json={"email": "nobody@x.com", "password": "bad"})
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
