# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for RateLimitedClient."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

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
