# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One server of a Celerp installation for the integration validation suite: its API or
its UI, each in a process of its own, as Celerp runs them, on the installation's own
database, data folder and config.

The only thing changed is name resolution. INTEGRATION_HOSTS maps a public host name
(relay.celerp.com, the object store, an installation's public address) to the stand-in
relay's local port, and requests to those names are sent there over plain HTTP, the way
a reverse proxy in front of the relay would. The original Host header is kept.
Everything else, the public address checks included, runs as shipped. Run it with
``python tests/integration_installation.py api|ui <port>``."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

import httpx

LOOPBACK = "127.0.0.1"


def route_hosts(hosts: dict[str, int]) -> Callable[[], None]:
    """Send requests for the mapped host names to their local ports; returns the undo.
    Also used by the suite itself to follow a company copy link the way the receiving
    installation's pages do."""
    from celerp.services import outbound_url

    original_send = httpx.AsyncHTTPTransport.handle_async_request
    original_resolve = outbound_url._resolve_public_addresses

    async def handle(self, request: httpx.Request):
        port = hosts.get(request.url.host)
        if port is not None:
            request.url = request.url.copy_with(scheme="http", host=LOOPBACK, port=port)
        return await original_send(self, request)

    async def resolve(host: str, port: int) -> list[str]:
        # A mapped name stands for a public host, and the loopback address is where
        # its requests were just sent; any other name is checked as shipped.
        if host in hosts or host == LOOPBACK:
            return [LOOPBACK]
        return await original_resolve(host, port)

    httpx.AsyncHTTPTransport.handle_async_request = handle
    outbound_url._resolve_public_addresses = resolve

    def undo() -> None:
        httpx.AsyncHTTPTransport.handle_async_request = original_send
        outbound_url._resolve_public_addresses = original_resolve
    return undo


if __name__ == "__main__":
    import uvicorn

    route_hosts(json.loads(os.environ["INTEGRATION_HOSTS"]))
    server, port = sys.argv[1], int(sys.argv[2])
    if server == "api":
        from celerp.main import app
    else:
        from ui.app import app
    uvicorn.run(app, host=LOOPBACK, port=port, log_level="warning")
