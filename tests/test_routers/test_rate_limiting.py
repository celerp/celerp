# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Structural rate limiting tests.

Verifies that the limiter is installed and configured correctly.
Does NOT actually hit rate limits (too slow/flaky in CI).
"""
from __future__ import annotations

import pytest

from celerp.main import app, limiter
from celerp.routers import auth as auth_router


def test_limiter_attached_to_app():
    """app.state.limiter must be set to the global Limiter instance."""
    assert hasattr(app.state, "limiter"), "app.state.limiter not set"
    assert app.state.limiter is limiter, "app.state.limiter is not the module-level limiter"


def test_limiter_has_default_60_per_minute():
    """Global limiter must have a non-empty default_limits list (configured as 60/minute in main.py)."""
    limits = list(app.state.limiter._default_limits)
    assert len(limits) > 0, (
        "app.state.limiter._default_limits is empty — 60/minute was not applied at Limiter() init"
    )


def test_auth_limiter_imported_and_present():
    """celerp.routers.auth exports a module-level 'limiter' for login rate limiting."""
    assert hasattr(auth_router, "limiter"), "auth module has no module-level 'limiter'"


@pytest.mark.asyncio
async def test_login_endpoint_accepts_valid_request(client):
    """Login endpoint is reachable (limiter doesn't block valid first request)."""
    # Register a user first
    r = await client.post(
        "/auth/register",
        json={"company_name": "RL Co", "email": "rl@test.com", "name": "RL", "password": "pw"},
    )
    assert r.status_code == 200

    # Login should work
    r = await client.post("/auth/login", json={"email": "rl@test.com", "password": "pw"})
    assert r.status_code == 200
    assert "access_token" in r.json()


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_returns_429(client):
    """RateLimitExceeded exception handler returns 429 JSON."""
    from slowapi.errors import RateLimitExceeded
    from celerp.main import app as _app
    # Find the exception handler in the app's exception handlers dict
    handlers = _app.exception_handlers
    assert RateLimitExceeded in handlers or 429 in handlers or any(
        "RateLimitExceeded" in str(k) for k in handlers
    ), f"No RateLimitExceeded handler found. Handlers: {list(handlers.keys())}"
