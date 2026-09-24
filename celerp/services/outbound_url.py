# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Validation and bounded fetching for user-configured public endpoints."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpcore
import httpx


def _blocked_ip(addr: str) -> bool:
    try:
        return not ipaddress.ip_address(addr).is_global
    except ValueError:
        return True


async def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise ValueError("URL host could not be resolved") from exc

    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if _blocked_ip(address):
            raise ValueError("URL host is not a public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("URL host could not be resolved")
    return addresses


async def validate_public_base_url(
    value: str,
    *,
    allow_http: bool = False,
    reject_query: bool = False,
    reject_fragment: bool = False,
) -> str:
    """Return a normalized public base URL or raise ValueError."""
    cleaned = (value or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    allowed_schemes = {"https"}
    if allow_http:
        allowed_schemes.add("http")
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL must use a supported scheme and public hostname")
    if reject_query and parsed.query:
        raise ValueError("Base URL must not contain a query")
    if reject_fragment and parsed.fragment:
        raise ValueError("Base URL must not contain a fragment")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    await _resolve_public_addresses(parsed.hostname, port)
    return cleaned


class _PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend=None) -> None:
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        addresses = await _resolve_public_addresses(host, port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        last_error = None
        for address in addresses:
            remaining = max(0.0, deadline - loop.time()) if deadline is not None else None
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("No public address was available")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not supported")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def public_async_transport() -> httpx.AsyncHTTPTransport:
    """HTTP transport whose TCP connections use validated public addresses."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool._network_backend = _PublicNetworkBackend()
    return transport


@dataclass(frozen=True)
class PublicFetchResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str


class PublicFetchTooLarge(ValueError):
    pass


async def fetch_public_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 10.0,
    allow_http: bool = False,
    max_redirects: int = 0,
    headers: dict | None = None,
    auth=None,
    params: dict | None = None,
) -> PublicFetchResponse:
    """Fetch a bounded response from a validated public URL."""
    current = url
    request_params = params
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=public_async_transport(),
    ) as client:
        for hop in range(max_redirects + 1):
            current = await validate_public_base_url(
                current,
                allow_http=allow_http,
                reject_query=False,
                reject_fragment=True,
            )
            async with client.stream(
                "GET", current, headers=headers, auth=auth, params=request_params
            ) as response:
                request_params = None
                if response.status_code in {301, 302, 303, 307, 308}:
                    if hop >= max_redirects:
                        raise ValueError("Too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect did not include a destination")
                    current = urljoin(str(response.url), location)
                    continue

                declared = response.headers.get("content-length")
                declared_size = None
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        pass
                if declared_size is not None and declared_size > max_bytes:
                    raise PublicFetchTooLarge("Remote response is too large")

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PublicFetchTooLarge("Remote response is too large")
                return PublicFetchResponse(
                    response.status_code,
                    dict(response.headers),
                    bytes(body),
                    str(response.url),
                )
    raise ValueError("Too many redirects")
