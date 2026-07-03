# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Rate-limited HTTP client for connector API calls."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 2.0


class RateLimitedClient:
    """httpx.AsyncClient wrapper with automatic 429/503 backoff and retry."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
    ) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def __aenter__(self) -> "RateLimitedClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self._client.aclose()

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        return await self._request("DELETE", url, **kwargs)

    # Transient transport failures (flaky network, mid-pagination drops) — retried
    # with the same backoff as 429/503 rather than aborting the whole sync page.
    _RETRY_EXC = (
        httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
        httpx.PoolTimeout, httpx.RemoteProtocolError,
    )

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except self._RETRY_EXC as exc:
                if attempt == self._max_retries:
                    raise
                delay = self._backoff_base ** attempt
                log.info("Transport error (%s), retry %d/%d in %.1fs",
                         type(exc).__name__, attempt + 1, self._max_retries, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code not in (429, 503):
                return resp
            if attempt == self._max_retries:
                return resp  # Return the 429/503 on final attempt, let caller handle
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = self._backoff_base ** attempt
            else:
                delay = self._backoff_base ** attempt
            log.info("Rate limited (%d), retry %d/%d in %.1fs", resp.status_code, attempt + 1, self._max_retries, delay)
            await asyncio.sleep(delay)
        return resp  # unreachable but satisfies type checker
