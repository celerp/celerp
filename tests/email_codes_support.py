# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A stand-in for the Celerp relay's /email-codes endpoints, for the app's password
recovery tests. It keeps one code per email and attempt secret, good for 5 guesses as the
relay's is, refuses a send or verify without a well-formed secret as the relay does, records
every call, and can be switched to each way the relay fails."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

RELAY = "https://relay.test"
CODE = "24681012"

# Wrong guesses an attempt's code takes before it is used up.
GUESSES = 5

# Each way the relay can fail to answer, as the app sees it.
FAILURES = ("down", "timeout", "404", "405", "500", "bad-json")

# The relay's rule for an attempt secret.
ATTEMPT = re.compile(r"[A-Za-z0-9_-]{32,128}")


class FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # (email, attempt) -> wrong guesses its code has left.
        self.issued: dict[tuple[str, str], int] = {}
        self.failure: str | None = None
        # The answer to a send other than a plain yes, meaning no code is coming.
        self.send_status: int | None = None

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def attempts(self, path: str, email: str) -> list[str]:
        """The attempt secret of each call to *path* for *email*, in order."""
        return [body["attempt"] for p, body in self.calls if p == path and body["email"] == email]

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
        if path in ("/email-codes/send", "/email-codes/verify") and not (
                isinstance(body.get("attempt"), str) and ATTEMPT.fullmatch(body["attempt"])):
            return httpx.Response(422, json={"detail": "attempt"})
        if path == "/email-codes/send":
            if self.send_status is not None:
                return httpx.Response(self.send_status, json={"detail": "no"})
            self.issued.setdefault((body["email"], body["attempt"]), GUESSES)
            return httpx.Response(200, json={"ok": True})
        if path == "/email-codes/verify":
            key = (body["email"], body["attempt"])
            if key not in self.issued:
                return httpx.Response(400, json={"detail": "code_refused"})
            if body.get("code") == CODE:
                del self.issued[key]
                return httpx.Response(200, json={"ok": True})
            self.issued[key] -= 1
            if not self.issued[key]:
                del self.issued[key]
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
