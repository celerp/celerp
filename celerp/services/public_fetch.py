# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bounded fetching of URLs supplied by systems outside the company."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

_MAX_REDIRECTS = 5


class PublicFetchError(ValueError):
    """The URL is not safe to fetch, or its response broke a resource limit."""


def blocked_ip(addr: str) -> bool:
    """Anything except an Internet-routable address is blocked."""
    try:
        return not ipaddress.ip_address(addr).is_global
    except ValueError:
        return True


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def _validated_target(url: str, schemes: tuple[str, ...]) -> tuple[str, list[str]]:
    parsed = urlparse(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise PublicFetchError(f"must be {' or '.join(schemes)}")
    if parsed.username is not None or parsed.password is not None:
        raise PublicFetchError("must not include credentials")
    try:
        addrs = await _resolve(parsed.hostname)
    except OSError as exc:
        raise PublicFetchError("could not be resolved") from exc
    if not addrs or any(blocked_ip(addr) for addr in addrs):
        raise PublicFetchError("is not a public address")
    return parsed.hostname, addrs


async def check_public_url(url: str, schemes: tuple[str, ...] = ("https",)) -> str:
    await _validated_target(url, schemes)
    return url


def _host_header(url: httpx.URL) -> str:
    host = url.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if url.scheme == "https" else 80
    return f"{host}:{url.port}" if url.port and url.port != default_port else host


async def fetch_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    schemes: tuple[str, ...] = ("https",),
) -> tuple[bytes, str]:
    """Fetch a public URL using the exact address that was validated for each hop."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            host, addrs = await _validated_target(url, schemes)
            logical = httpx.URL(url)
            response = None
            last_connect_error = None
            for addr in addrs:
                request = client.build_request(
                    "GET",
                    logical.copy_with(host=addr),
                    headers={"Accept-Encoding": "identity", "Host": _host_header(logical)},
                    extensions={"sni_hostname": host},
                )
                try:
                    response = await client.send(request, stream=True)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last_connect_error = exc
                    continue
                break
            if response is None:
                if last_connect_error is not None:
                    raise last_connect_error
                raise PublicFetchError("could not be connected")
            try:
                if response.has_redirect_location:
                    location = response.headers.get("location")
                    if location:
                        url = urljoin(url, location)
                        continue
                response.raise_for_status()
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise PublicFetchError("response is too large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PublicFetchError("response is too large")
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                return bytes(body), content_type
            finally:
                await response.aclose()
    raise PublicFetchError("too many redirects")
