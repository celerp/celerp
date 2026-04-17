# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for main.py: lifespan, exception handlers, lifespan gateway startup/teardown."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient


# ── Exception handlers (via the live app) ────────────────────────────────────

@pytest.mark.asyncio
async def test_unhandled_exception_handler_returns_500():
    """unhandled_exception_handler returns 500 JSON response."""
    from unittest.mock import MagicMock
    from celerp.main import unhandled_exception_handler

    mock_url = MagicMock()
    mock_url.path = "/__test_500__"
    mock_url.query = ""
    mock_client = MagicMock()
    mock_client.host = "127.0.0.1"
    mock_request = MagicMock()
    mock_request.url = mock_url
    mock_request.method = "GET"
    mock_request.client = mock_client
    exc = RuntimeError("boom")

    response = await unhandled_exception_handler(mock_request, exc)
    assert response.status_code == 500
    import json
    body = json.loads(response.body)
    assert body["detail"] == "Internal server error"


@pytest.mark.asyncio
async def test_rate_limit_handler_returns_429():
    """rate_limit_handler returns 429 JSON response."""
    from unittest.mock import MagicMock
    from celerp.main import rate_limit_handler
    from slowapi.errors import RateLimitExceeded

    mock_request = MagicMock()
    mock_exc = MagicMock(spec=RateLimitExceeded)

    response = await rate_limit_handler(mock_request, mock_exc)
    assert response.status_code == 429
    import json
    body = json.loads(response.body)
    assert body["detail"] == "Rate limit exceeded"


# ── Lifespan: gateway startup/teardown ────────────────────────────────────────
#
# All three tests mock `engine.begin()` so they don't touch the real DB.
# The lifespan only needs: (a) DB tables created and (b) gateway task logic.
# Mocking the engine makes these pure unit tests, immune to DB state / pool teardown.

def _mock_db():
    """Return a patch context that makes engine.begin() a no-op."""
    mock_conn = AsyncMock()
    mock_conn.run_sync = AsyncMock()
    mock_begin = MagicMock()
    mock_begin.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_begin.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.begin = MagicMock(return_value=mock_begin)
    return patch("celerp.main.engine", mock_engine)


@pytest.mark.asyncio
async def test_lifespan_starts_gateway_when_token_set():
    """lifespan starts gateway task when settings.gateway_token is set."""
    from celerp.config import settings
    import celerp.gateway.client as gw_module

    original_token = settings.gateway_token
    original_instance = settings.gateway_instance_id

    fake_run_called = []

    async def fake_run(self):
        fake_run_called.append(True)

    with _mock_db(), patch.object(gw_module.GatewayClient, "run", fake_run):
        settings.gateway_token = "test-gw-token"
        settings.gateway_instance_id = "test-inst-id"

        from celerp.main import lifespan

        mock_app = MagicMock()

        try:
            async with lifespan(mock_app):
                client_inst = gw_module.get_client()
                assert client_inst is not None
        finally:
            settings.gateway_token = original_token
            settings.gateway_instance_id = original_instance
            gw_module.set_client(None)


@pytest.mark.asyncio
async def test_lifespan_no_gateway_when_token_empty():
    """lifespan does NOT start gateway task when settings.gateway_token is empty."""
    from celerp.config import settings
    import celerp.gateway.client as gw_module

    original_token = settings.gateway_token
    settings.gateway_token = ""

    with _mock_db():
        from celerp.main import lifespan

        mock_app = MagicMock()

        try:
            async with lifespan(mock_app):
                assert gw_module.get_client() is None
        finally:
            settings.gateway_token = original_token


@pytest.mark.asyncio
async def test_lifespan_gateway_teardown():
    """lifespan stops gateway client on shutdown."""
    from celerp.config import settings
    import celerp.gateway.client as gw_module

    original_token = settings.gateway_token
    stopped = []

    async def fake_run(self):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            pass

    with _mock_db(), patch.object(gw_module.GatewayClient, "run", fake_run):
        settings.gateway_token = "test-gw-token"

        from celerp.main import lifespan

        mock_app = MagicMock()

        async with lifespan(mock_app):
            client_inst = gw_module.get_client()
            assert client_inst is not None
            original_close = client_inst.close
            async def tracked_close():
                stopped.append(True)
                await original_close()
            client_inst.close = tracked_close

        assert stopped == [True]
        assert gw_module.get_client() is None

    settings.gateway_token = original_token
