# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from celerp.services.outbound_url import (
    PublicFetchTooLarge,
    fetch_public_bytes,
    validate_public_base_url,
)


def _loop(*addresses: str):
    return SimpleNamespace(getaddrinfo=AsyncMock(return_value=[
        (2, 1, 6, "", (addr, 443)) for addr in addresses
    ]))


@pytest.mark.asyncio
async def test_public_base_url_accepts_public_addresses():
    with patch(
        "celerp.services.outbound_url.asyncio.get_running_loop",
        return_value=_loop("93.184.216.34"),
    ):
        assert await validate_public_base_url(
            "https://example.com/shop/",
            reject_query=True,
            reject_fragment=True,
        ) == "https://example.com/shop"


@pytest.mark.asyncio
async def test_public_base_url_rejects_any_private_resolution():
    with patch(
        "celerp.services.outbound_url.asyncio.get_running_loop",
        return_value=_loop("93.184.216.34", "127.0.0.1"),
    ):
        with pytest.raises(ValueError, match="public"):
            await validate_public_base_url("https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com",
        "https://example.com/path?next=x",
        "https://example.com/path#frag",
    ],
)
async def test_public_base_url_rejects_credential_or_base_url_suffixes(url):
    with patch(
        "celerp.services.outbound_url.asyncio.get_running_loop",
        return_value=_loop("93.184.216.34"),
    ):
        with pytest.raises(ValueError):
            await validate_public_base_url(
                url, reject_query=True, reject_fragment=True
            )


@pytest.mark.asyncio
async def test_public_fetch_enforces_streaming_body_cap():
    with patch(
        "celerp.services.outbound_url.validate_public_base_url",
        new=AsyncMock(side_effect=lambda url, **_: url),
    ), respx.mock:
        respx.get("https://example.com/file").mock(
            return_value=httpx.Response(200, content=b"12345")
        )
        with pytest.raises(PublicFetchTooLarge):
            await fetch_public_bytes(
                "https://example.com/file",
                max_bytes=4,
            )


@pytest.mark.asyncio
async def test_public_fetch_ignores_environment_network_override(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with patch(
        "celerp.services.outbound_url.validate_public_base_url",
        new=AsyncMock(side_effect=lambda url, **_: url),
    ), respx.mock:
        route = respx.get("https://example.com/file").mock(
            return_value=httpx.Response(200, content=b"ok")
        )
        response = await fetch_public_bytes(
            "https://example.com/file",
            max_bytes=10,
        )
    assert route.called
    assert response.content == b"ok"


@pytest.mark.asyncio
async def test_public_fetch_rejects_invalid_redirect_destination():
    validate = AsyncMock(
        side_effect=[
            "https://example.com/start",
            ValueError("URL host is not a public address"),
        ]
    )
    with patch(
        "celerp.services.outbound_url.validate_public_base_url",
        new=validate,
    ), respx.mock:
        respx.get("https://example.com/start").mock(
            return_value=httpx.Response(
                302,
                headers={"location": "https://redirect.example/final"},
            )
        )
        with pytest.raises(ValueError, match="public"):
            await fetch_public_bytes(
                "https://example.com/start",
                max_bytes=10,
                max_redirects=1,
            )
