# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The browser suite's session API client renews its sign-in when the access token
lapses mid-run, instead of failing every later call."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright")
from test_browser.conftest import _RenewingAuth  # noqa: E402


def test_a_lapsed_token_is_renewed_once_and_the_request_sent_again():
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/auth/token/refresh":
            assert request.content == b'{"refresh_token":"r1"}'
            return httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2"})
        if request.headers["Authorization"] == "Bearer a2":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"detail": "Invalid token"})

    auth = _RenewingAuth("r1")
    with httpx.Client(base_url="http://api", headers={"Authorization": "Bearer a1"}, auth=auth,
                      transport=httpx.MockTransport(answer)) as client:
        auth.client = client
        assert client.get("/items").json() == {"ok": True}
        assert client.get("/items").json() == {"ok": True}
    assert seen == [("/items", "Bearer a1"), ("/auth/token/refresh", None), ("/items", "Bearer a2"),
                    ("/items", "Bearer a2")]
    assert (client.headers["Authorization"], auth.refresh_token) == ("Bearer a2", "r2")


def test_a_refused_renewal_returns_the_401():
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid token"})

    auth = _RenewingAuth("r1")
    with httpx.Client(base_url="http://api", headers={"Authorization": "Bearer a1"}, auth=auth,
                      transport=httpx.MockTransport(answer)) as client:
        auth.client = client
        assert client.get("/items").status_code == 401
    assert client.headers["Authorization"] == "Bearer a1"
