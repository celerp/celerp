# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Rate-limited HTTP client for connector API calls."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 2.0


class RateLimitedClient:
    """httpx.AsyncClient wrapper with automatic backoff and retry."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        before_request: Callable[[str], Awaitable[None]] | None = None,
        public_only: bool = False,
    ) -> None:
        transport = None
        if public_only:
            from celerp.services.outbound_url import public_async_transport
            transport = public_async_transport()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._before_request = before_request

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

    # Statuses worth retrying. A GET also retries a gateway failure (502/504),
    # which a relay in front of the platform returns for a transport error.
    _RETRY_STATUS = (429, 503)
    _GET_RETRY_STATUS = (429, 502, 503, 504)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        retryable = method.upper() != "POST"
        retry_status = self._GET_RETRY_STATUS if method.upper() == "GET" else self._RETRY_STATUS
        for attempt in range(self._max_retries + 1):
            if self._before_request is not None:
                await self._before_request(url)
            try:
                resp = await self._client.request(method, url, **kwargs)
            except self._RETRY_EXC as exc:
                if not retryable or attempt == self._max_retries:
                    raise
                delay = self._backoff_base ** attempt
                log.info("Transport error (%s), retry %d/%d in %.1fs",
                         type(exc).__name__, attempt + 1, self._max_retries, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code not in retry_status or not retryable:
                return resp
            if attempt == self._max_retries:
                return resp  # Return the last retryable status, let caller handle
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = self._backoff_base ** attempt
            else:
                delay = self._backoff_base ** attempt
            log.info("Retryable status (%d), retry %d/%d in %.1fs", resp.status_code, attempt + 1, self._max_retries, delay)
            await asyncio.sleep(delay)
        return resp  # unreachable but satisfies type checker
