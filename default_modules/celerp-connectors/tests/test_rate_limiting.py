# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for RateLimitedClient."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import respx
import httpx

from celerp.connectors.http import RateLimitedClient


@pytest.mark.asyncio
async def test_429_triggers_backoff():
    with respx.mock:
        route = respx.get("https://api.test/items")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0.01"}),
            httpx.Response(200, json={"ok": True}),
        ]
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_retry_after_header_respected():
    with respx.mock:
        route = respx.get("https://api.test/items")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0.01"}),
            httpx.Response(200, json={"ok": True}),
        ]
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_max_retries_exceeded():
    with respx.mock:
        respx.get("https://api.test/items").mock(return_value=httpx.Response(429))
        async with RateLimitedClient(max_retries=2, backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_non_429_not_retried():
    with respx.mock:
        respx.get("https://api.test/items").mock(return_value=httpx.Response(500))
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
        assert resp.status_code == 500
        assert respx.calls.call_count == 1


@pytest.mark.asyncio
async def test_503_triggers_backoff():
    with respx.mock:
        route = respx.get("https://api.test/items")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_503_is_not_retried():
    with respx.mock:
        route = respx.post("https://api.test/items").mock(
            return_value=httpx.Response(503)
        )
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.post("https://api.test/items", json={"name": "x"})
    assert resp.status_code == 503
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_post_ambiguous_transport_error_is_not_retried():
    with respx.mock:
        route = respx.post("https://api.test/items").mock(
            side_effect=httpx.ReadTimeout("ambiguous")
        )
        async with RateLimitedClient(backoff_base=0.01) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.post("https://api.test/items", json={"name": "x"})
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_get_gateway_failure_is_retried(status):
    with respx.mock:
        route = respx.get("https://api.test/items")
        route.side_effect = [httpx.Response(status), httpx.Response(200, json={"ok": True})]
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.get("https://api.test/items")
    assert resp.status_code == 200
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_put_gateway_failure_is_not_retried():
    with respx.mock:
        route = respx.put("https://api.test/items").mock(return_value=httpx.Response(502))
        async with RateLimitedClient(backoff_base=0.01) as client:
            resp = await client.put("https://api.test/items", json={"a": 1})
    assert resp.status_code == 502
    assert route.call_count == 1
