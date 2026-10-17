# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A stand-in for the Celerp relay's /email-codes endpoints, for the app's password
recovery and setup email tests. It issues one known code per email and purpose,
records every call, and can be switched to each way the relay fails."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

RELAY = "https://relay.test"
CODE = "246810"

# Each way the relay can fail to answer, as the app sees it.
FAILURES = ("down", "timeout", "404", "405", "500", "bad-json")


class FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.issued: dict[tuple[str, str], str] = {}
        self.failure: str | None = None
        self.send_gate: asyncio.Event | None = None

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def sent_to(self) -> list[str]:
        return [body["email"] for path, body in self.calls if path == "/email-codes/send"]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        if self.failure == "down":
            raise httpx.ConnectError("refused", request=request)
        if self.failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.failure in ("404", "405", "500"):
            return httpx.Response(int(self.failure), json={"detail": "no"})
        if self.failure == "bad-json":
            return httpx.Response(200, content=b"<html>")
        if path == "/email-codes/status":
            return httpx.Response(200, json={"ok": True})
        if path == "/email-codes/send":
            if self.send_gate is not None:
                await self.send_gate.wait()
            self.issued[(body["email"], body["purpose"])] = CODE
            return httpx.Response(200, json={"ok": True})
        if path == "/email-codes/verify":
            key = (body["email"], body["purpose"])
            if self.issued.get(key) == body.get("code"):
                del self.issued[key]
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(400, json={"detail": "code_refused"})
        return httpx.Response(404, json={"detail": "Not Found"})


@contextmanager
def relay_stand_in():
    """Every relay call the app makes goes to one FakeRelay."""
    relay = FakeRelay()

    async def _run(total_s, op):
        async with httpx.AsyncClient(transport=httpx.MockTransport(relay.handle)) as c:
            return await op(c)

    with patch("celerp.gateway.state.relay_http_url", return_value=RELAY), \
         patch("celerp.gateway.state.with_relay_client", new=_run):
        yield relay


@pytest.fixture
def relay():
    with relay_stand_in() as r:
        yield r


@pytest.fixture
def short_floor(monkeypatch):
    """The reset answer's minimum time, shortened so the suite does not wait 4 s a call."""
    import celerp.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "RECOVERY_FLOOR_S", 0.2, raising=False)
    return 0.2


async def settle() -> None:
    """Let the background sends the app started finish."""
    from celerp.services import background
    await asyncio.gather(*list(background._BG_TASKS), return_exceptions=True)
