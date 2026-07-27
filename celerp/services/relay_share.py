# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Core seam for the free-tier share round-trip and the lazy tunnel trigger.

The docs module is MIT-licensed and must not import the protected `celerp.gateway`
package. It reaches the relay mint and the on-demand tunnel only through this core
(BSL) service, mirroring how `celerp/services/email.py` fronts the relay email
round-trip. The relay auth handshake itself lives once in `celerp.gateway.state`.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def mint_free_share_url(doc_token: str) -> str | None:
    """Ask the relay to sign a `share.celerp.com` envelope that routes back to
    this instance's own `/share/<doc_token>` render, and return the URL.

    Returns None when the instance is not relay-bound (self-hosted, no
    `gateway_token`) or on any relay failure, so a free user with no reachable
    relay degrades honestly to no link rather than a broken one.
    """
    from celerp.config import settings

    if not settings.gateway_token:
        return None

    from celerp.gateway.state import fetch_relay_bearer, relay_http_url

    base = relay_http_url()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            bearer = await fetch_relay_bearer(client)
            resp = await client.post(
                f"{base}/share/free/mint",
                headers={"Authorization": f"Bearer {bearer}"},
                json={"doc_token": doc_token},
            )
        if resp.status_code == 200:
            return resp.json().get("url")
        log.warning("Relay share mint failed: %s", resp.status_code)
    except Exception as exc:
        log.warning("Relay share mint failed: %s", exc)
    return None


def ensure_running() -> None:
    """Bring the lazy relay tunnel up on demand for a free instance.

    Thin delegate to `celerp.gateway.ensure_running()` so the MIT docs module
    triggers the tunnel through this core seam and never imports the protected
    gateway package directly. Idempotent and a no-op without a `gateway_token`.
    """
    from celerp.gateway import ensure_running as _ensure

    _ensure()
