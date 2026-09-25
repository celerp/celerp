# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The UI's /system/update* routes forward to the API as the signed-in user."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from ui.routes import system_update


def _client(monkeypatch, handler, token="tok"):
    seen: list[httpx.Request] = []

    def local_client(tok, *, timeout, follow_redirects):
        assert tok == token
        def record(req):
            seen.append(req)
            return handler(req)
        return httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(record))

    monkeypatch.setattr(system_update.api, "_local_client", local_client)
    monkeypatch.setattr(system_update, "_token", lambda request: token)
    app = FastAPI()
    system_update.setup_routes(app)
    return TestClient(app), seen


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/system/update", None),
    ("POST", "/system/update", None),
    ("POST", "/system/update/check", None),
    ("PATCH", "/system/update/settings", {"auto": False}),
])
def test_forwards_method_path_body_and_answer(monkeypatch, method, path, body):
    client, seen = _client(monkeypatch, lambda req: httpx.Response(409, json={"detail": "current"}))
    r = client.request(method, path, json=body)
    assert r.status_code == 409
    assert r.json() == {"detail": "current"}
    assert (seen[0].method, seen[0].url.path) == (method, path)
    if body:
        assert seen[0].content == b'{"auto":false}'
        assert seen[0].headers["content-type"] == "application/json"


def test_signed_out_is_refused_without_calling_the_api(monkeypatch):
    client, seen = _client(monkeypatch, lambda req: httpx.Response(200), token=None)
    assert client.get("/system/update").status_code == 401
    assert seen == []


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_api_away_while_installing_reads_as_unavailable(monkeypatch, error):
    def fail(req):
        raise error
    client, _ = _client(monkeypatch, fail)
    assert client.get("/system/update").status_code == 503
