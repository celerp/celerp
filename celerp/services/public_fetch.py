# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fetching a URL that came from outside the company.

A URL supplied by a sender, a store or any other remote system is fetched only
when its host resolves to public internet addresses. Redirects are followed one
hop at a time and each hop is checked the same way, and a response body is read
only up to a stated size.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

_MAX_REDIRECTS = 5


class PublicFetchError(ValueError):
    """The URL is not one that may be fetched, or its response broke a limit."""


def blocked_ip(addr: str) -> bool:
    """True for anything that is not a public address, including unparseable input."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def check_public_url(url: str, schemes: tuple[str, ...] = ("https",)) -> str:
    """Return *url* when its scheme is allowed and its host resolves only to public
    addresses; otherwise raise PublicFetchError saying which."""
    parsed = urlparse(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise PublicFetchError(f"must be {' or '.join(schemes)}")
    try:
        addrs = await _resolve(parsed.hostname)
    except OSError as exc:
        raise PublicFetchError("could not be resolved") from exc
    if not addrs or any(blocked_ip(addr) for addr in addrs):
        raise PublicFetchError("is not a public address")
    return url


async def _resolve(host: str) -> list[str]:
    """Every address *host* resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


async def fetch_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    schemes: tuple[str, ...] = ("https",),
) -> tuple[bytes, str]:
    """GET a public URL: (body, content type without parameters).

    Raises PublicFetchError for a URL or redirect that is not public, too many
    redirects, or a body over *max_bytes*; httpx errors (including a non-2xx
    status) propagate unchanged.
    """
    headers = {"Accept-Encoding": "identity"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await check_public_url(url, schemes)
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.has_redirect_location:
                    url = str(resp.next_request.url)
                    continue
                resp.raise_for_status()
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise PublicFetchError("response is too large")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PublicFetchError("response is too large")
                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                return bytes(body), content_type
    raise PublicFetchError("too many redirects")
