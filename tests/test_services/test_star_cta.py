# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the GitHub-star CTA client (celerp.services.star_cta).

The relay is fully mocked, so no network call is made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.config import settings
from celerp.services import star_cta


@pytest.fixture(autouse=True)
def _bust_cta_cache():
    star_cta.cache_bust()
    yield
    star_cta.cache_bust()


def _resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


@pytest.mark.asyncio
async def test_returns_relay_cta(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", True)
    cta = {"mode": "founding", "cta_label": "Star on GitHub", "url": "https://celerp.com/github"}
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(return_value=_resp(200, cta))
        out = await star_cta.get_star_cta("footer")
    assert out == cta  # rendered verbatim, no install-side copy


@pytest.mark.asyncio
async def test_caches_within_ttl(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", True)
    monkeypatch.setattr(settings, "star_cta_cache_ttl_s", 3600)
    get = AsyncMock(return_value=_resp(200, {"mode": "founding"}))
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = get
        await star_cta.get_star_cta("footer")
        await star_cta.get_star_cta("footer")
    assert get.await_count == 1


@pytest.mark.asyncio
async def test_ttl_expiry_refetches(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", True)
    monkeypatch.setattr(settings, "star_cta_cache_ttl_s", -1)  # always expired
    get = AsyncMock(return_value=_resp(200, {"mode": "founding"}))
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = get
        await star_cta.get_star_cta("footer")
        await star_cta.get_star_cta("footer")
    assert get.await_count == 2


@pytest.mark.asyncio
async def test_relay_connect_error_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", True)
    import httpx
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.ConnectError("refused"))
        out = await star_cta.get_star_cta("footer")
    assert out is None  # determinism: no fabricated CTA


@pytest.mark.asyncio
async def test_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", True)
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = AsyncMock(return_value=_resp(503, {}))
        out = await star_cta.get_star_cta("footer")
    assert out is None


@pytest.mark.asyncio
async def test_disabled_skips_fetch(monkeypatch):
    monkeypatch.setattr(settings, "star_cta_enabled", False)
    get = AsyncMock(return_value=_resp(200, {"mode": "founding"}))
    with patch("httpx.AsyncClient") as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value.get = get
        out = await star_cta.get_star_cta("footer")
    assert out is None
    assert get.await_count == 0


def test_neutral_cta_is_static_link():
    cta = star_cta.neutral_cta("footer")
    assert cta["mode"] == "neutral"
    assert cta["show_count"] is False
    assert "/github" in cta["url"]
    assert "utm_medium=footer" in cta["url"]
