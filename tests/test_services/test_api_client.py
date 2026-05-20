# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for ui/api_client.py infrastructure.

Covers:
  - _api_client: TimeoutException → APIError(504)
  - _api_client: ConnectError → APIError(503)
  - _anon_api_client: TimeoutException → APIError(504)
  - _anon_api_client: ConnectError → APIError(503)
  - batch_import: uses _api_client (timeout propagated)
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from ui.api_client import (
    _api_client,
    _anon_api_client,
    APIError,
    batch_import,
)


# ---------------------------------------------------------------------------
# _api_client error mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_client_timeout_raises_504():
    """httpx.TimeoutException inside _api_client → APIError(504)."""
    with patch("ui.api_client._client") as mock_client_factory:
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_client_factory.return_value = mock_cm

        with pytest.raises(APIError) as exc_info:
            async with _api_client("tok"):
                pass  # pragma: no cover

    assert exc_info.value.status == 504
    assert "timed out" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_api_client_connect_error_raises_503():
    """httpx.ConnectError inside _api_client → APIError(503)."""
    with patch("ui.api_client._client") as mock_client_factory:
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_client_factory.return_value = mock_cm

        with pytest.raises(APIError) as exc_info:
            async with _api_client("tok"):
                pass  # pragma: no cover

    assert exc_info.value.status == 503
    assert "running" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# _anon_api_client error mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anon_api_client_timeout_raises_504():
    with patch("ui.api_client._anon_client") as mock_factory:
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_cm

        with pytest.raises(APIError) as exc_info:
            async with _anon_api_client():
                pass  # pragma: no cover

    assert exc_info.value.status == 504


@pytest.mark.asyncio
async def test_anon_api_client_connect_error_raises_503():
    with patch("ui.api_client._anon_client") as mock_factory:
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_cm

        with pytest.raises(APIError) as exc_info:
            async with _anon_api_client():
                pass  # pragma: no cover

    assert exc_info.value.status == 503


# ---------------------------------------------------------------------------
# batch_import uses a 300s timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_import_uses_300s_timeout():
    """batch_import must request a 300s timeout so large payloads don't time out."""
    captured_timeout = None

    def fake_client(token: str, timeout: float = 10.0) -> httpx.AsyncClient:
        nonlocal captured_timeout
        captured_timeout = timeout
        # Return a client that immediately raises TimeoutException so we can
        # verify the timeout was passed without needing a real server.
        raise httpx.ReadTimeout("injected")

    with patch("ui.api_client._client", side_effect=fake_client):
        with pytest.raises(APIError) as exc_info:
            await batch_import("tok", "/items/import/batch", [{"x": 1}])

    assert captured_timeout == 300.0
    assert exc_info.value.status == 504
