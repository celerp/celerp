# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Validation for server-side requests to user-configured public endpoints."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


def _blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise ValueError("URL host could not be resolved") from exc
    if not infos or any(_blocked_ip(info[4][0]) for info in infos):
        raise ValueError("URL host is not a public address")
    return cleaned
