# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fetching a URL that came from outside the company (celerp.services.public_fetch)."""
from __future__ import annotations

import httpx
import pytest
import respx

from celerp.services import public_fetch
from celerp.services.public_fetch import PublicFetchError, check_public_url, fetch_public

_ADDRESSES = {
    "shop.test": ["93.184.216.34"],
    "cdn.test": ["93.184.216.35"],
    "inside.test": ["192.168.1.20"],
    "mixed.test": ["93.184.216.36", "127.0.0.1"],
}


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    async def _resolve(host):
        if host[0].isdigit():
            return [host]
        if host not in _ADDRESSES:
            raise OSError("unknown host")
        return _ADDRESSES[host]
    monkeypatch.setattr(public_fetch, "_resolve", _resolve)


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://shop.test/a", "must be https"),
        ("https://inside.test/a", "is not a public address"),
        ("https://mixed.test/a", "is not a public address"),
        ("https://127.0.0.1/a", "is not a public address"),
        ("https://nowhere.test/a", "could not be resolved"),
    ],
)
@pytest.mark.asyncio
async def test_check_public_url_refuses(url, message):
    with pytest.raises(PublicFetchError, match=message):
        await check_public_url(url)


@pytest.mark.asyncio
async def test_fetch_follows_a_redirect_to_a_public_host():
    with respx.mock:
        respx.get("https://shop.test/a").mock(return_value=httpx.Response(302, headers={"location": "https://cdn.test/b"}))
        respx.get("https://cdn.test/b").mock(
            return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/png; q=1"}))
        assert await fetch_public("https://shop.test/a", max_bytes=10, timeout=5) == (b"img", "image/png")


@pytest.mark.asyncio
async def test_fetch_refuses_a_redirect_to_a_non_public_host():
    with respx.mock:
        respx.get("https://shop.test/a").mock(
            return_value=httpx.Response(302, headers={"location": "https://inside.test/b"}))
        inner = respx.get("https://inside.test/b").mock(return_value=httpx.Response(200, content=b"x"))
        with pytest.raises(PublicFetchError, match="is not a public address"):
            await fetch_public("https://shop.test/a", max_bytes=10, timeout=5)
    assert not inner.called


@pytest.mark.asyncio
async def test_fetch_stops_after_too_many_redirects():
    with respx.mock:
        respx.get("https://shop.test/a").mock(return_value=httpx.Response(302, headers={"location": "/a"}))
        with pytest.raises(PublicFetchError, match="too many redirects"):
            await fetch_public("https://shop.test/a", max_bytes=10, timeout=5)


@pytest.mark.asyncio
async def test_fetch_stops_reading_past_the_size_limit():
    async def _body():
        for _ in range(4):
            yield b"x" * 8

    with respx.mock:
        respx.get("https://shop.test/a").mock(return_value=httpx.Response(200, content=_body()))
        with pytest.raises(PublicFetchError, match="too large"):
            await fetch_public("https://shop.test/a", max_bytes=20, timeout=5)


@pytest.mark.asyncio
async def test_fetch_raises_for_an_error_status():
    with respx.mock:
        respx.get("https://shop.test/a").mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_public("https://shop.test/a", max_bytes=20, timeout=5)
