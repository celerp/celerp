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
def _dns(fake_dns):
    fake_dns(_ADDRESSES)


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
        respx.get("https://93.184.216.34/a").mock(return_value=httpx.Response(302, headers={"location": "https://cdn.test/b"}))
        respx.get("https://93.184.216.35/b").mock(
            return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/png; q=1"}))
        assert await fetch_public("https://shop.test/a", max_bytes=10, timeout=5) == (b"img", "image/png")


@pytest.mark.asyncio
async def test_fetch_refuses_a_redirect_to_a_non_public_host():
    with respx.mock:
        respx.get("https://93.184.216.34/a").mock(
            return_value=httpx.Response(302, headers={"location": "https://inside.test/b"}))
        inner = respx.get("https://192.168.1.20/b").mock(return_value=httpx.Response(200, content=b"x"))
        with pytest.raises(PublicFetchError, match="is not a public address"):
            await fetch_public("https://shop.test/a", max_bytes=10, timeout=5)
    assert not inner.called


@pytest.mark.asyncio
async def test_fetch_stops_after_too_many_redirects():
    with respx.mock:
        respx.get("https://93.184.216.34/a").mock(return_value=httpx.Response(302, headers={"location": "/a"}))
        with pytest.raises(PublicFetchError, match="too many redirects"):
            await fetch_public("https://shop.test/a", max_bytes=10, timeout=5)


@pytest.mark.asyncio
async def test_fetch_stops_reading_past_the_size_limit():
    async def _body():
        for _ in range(4):
            yield b"x" * 8

    with respx.mock:
        respx.get("https://93.184.216.34/a").mock(return_value=httpx.Response(200, content=_body()))
        with pytest.raises(PublicFetchError, match="too large"):
            await fetch_public("https://shop.test/a", max_bytes=20, timeout=5)


@pytest.mark.asyncio
async def test_fetch_raises_for_an_error_status():
    with respx.mock:
        respx.get("https://93.184.216.34/a").mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_public("https://shop.test/a", max_bytes=20, timeout=5)


def test_cgnat_is_not_a_public_fetch_target():
    assert public_fetch.blocked_ip("100.64.0.1") is True


@pytest.mark.asyncio
async def test_fetch_uses_the_exact_address_that_was_validated(monkeypatch):
    calls = 0
    async def resolve_once(host):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("logical host resolved more than once for one hop")
        return ["93.184.216.34"]
    monkeypatch.setattr(public_fetch, "_resolve", resolve_once)
    with respx.mock:
        route = respx.get("https://93.184.216.34/a").mock(return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"}))
        assert await fetch_public("https://shop.test/a", max_bytes=10, timeout=5) == (b"ok", "text/plain")
        request = route.calls.last.request
        assert request.headers["host"] == "shop.test"
        assert request.extensions["sni_hostname"] == "shop.test"
    assert calls == 1
