# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""POST /companies/me/modules/buy and GET /companies/me/modules/licenses.

The relay is faked at the httpx boundary; the real endpoints run - _relay_creds()
token exchange, the checkout POST, the license unwrap, and every error path. The
UI-layer tests mock ui.api_client.buy_module directly and never reach this code,
so these endpoint-level tests are what actually prove a purchase wires through.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.xdist_group("modules_api")


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, is_list=False):
        self.status_code = status_code
        self._json = json_data
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _relay(*, token=None, checkout=None, licenses=None):
    token = token or _FakeResp(200, {"access_token": "relay-jwt-1"})
    checkout = checkout or _FakeResp(200, {"url": "https://checkout.stripe.com/pay/cs_1"})
    licenses = licenses or _FakeResp(200, {"items": []})

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return licenses  # /marketplace/my-licenses

        async def post(self, url, **kw):
            if url.endswith("/auth/token"):
                return token
            return checkout  # /marketplace/checkout

    return _Fake


async def _register(client) -> dict:
    email = f"mp-buy-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={
        "company_name": "MP Buy Co", "email": email, "name": "Admin", "password": "pw123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def relay_env(monkeypatch):
    from celerp.config import settings as _s
    monkeypatch.setattr(_s, "gateway_token", "api-key-1")
    monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")


@pytest.mark.asyncio
async def test_buy_passes_checkout_url_through(client, relay_env):
    headers = await _register(client)
    with patch("httpx.AsyncClient", _relay()):
        r = await client.post("/companies/me/modules/buy",
                              json={"slug": "celerp-budgeting", "kind": "monthly"},
                              headers=headers)
    assert r.status_code == 200
    assert r.json()["url"] == "https://checkout.stripe.com/pay/cs_1"


@pytest.mark.asyncio
async def test_buy_forwards_custom_text_to_relay(client, relay_env):
    """Buyer-language purchase disclosures ride to the relay checkout call as
    custom_text; when none are supplied the key is omitted, not sent as null."""
    headers = await _register(client)
    seen: dict = {}

    class _Capture:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _FakeResp(200, {"items": []})

        async def post(self, url, **kw):
            if url.endswith("/auth/token"):
                return _FakeResp(200, {"access_token": "relay-jwt-1"})
            seen["body"] = kw.get("json")
            return _FakeResp(200, {"url": "https://checkout.stripe.com/pay/cs_1"})

    with patch("httpx.AsyncClient", _Capture):
        r = await client.post("/companies/me/modules/buy",
                              json={"slug": "acme-crm", "kind": "monthly",
                                    "custom_text": "Sold by Acme Ltd."},
                              headers=headers)
        assert r.status_code == 200
        assert seen["body"]["custom_text"] == "Sold by Acme Ltd."

        r = await client.post("/companies/me/modules/buy",
                              json={"slug": "celerp-budgeting", "kind": "monthly"},
                              headers=headers)
        assert r.status_code == 200
        assert "custom_text" not in seen["body"]


@pytest.mark.asyncio
async def test_buy_relay_error_maps_status_and_detail(client, relay_env):
    """A non-200 from the relay checkout must surface its status + detail, not a
    generic 500."""
    headers = await _register(client)
    fake = _relay(checkout=_FakeResp(402, {"detail": "Author cannot accept charges yet."}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/buy",
                              json={"slug": "celerp-budgeting", "kind": "monthly"},
                              headers=headers)
    assert r.status_code == 402
    assert "cannot accept charges" in r.json()["detail"]


@pytest.mark.asyncio
async def test_buy_non_dict_body_gives_502(client, relay_env):
    """A valid-JSON but non-object checkout body (relay drift) must 502, not
    AttributeError into a 500."""
    headers = await _register(client)
    fake = _relay(checkout=_FakeResp(200, ["unexpected"]))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/buy",
                              json={"slug": "celerp-budgeting", "kind": "monthly"},
                              headers=headers)
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_licenses_returns_only_active_slugs(client, relay_env):
    headers = await _register(client)
    fake = _relay(licenses=_FakeResp(200, {"items": [
        {"module_slug": "celerp-a", "status": "active"},
        {"module_slug": "celerp-b", "status": "canceled"},
        {"module_slug": "celerp-c", "status": "active"},
    ]}))
    with patch("httpx.AsyncClient", fake):
        r = await client.get("/companies/me/modules/licenses", headers=headers)
    assert r.status_code == 200
    assert set(r.json()["licensed"]) == {"celerp-a", "celerp-c"}


@pytest.mark.asyncio
async def test_licenses_not_signed_in_returns_empty(client, monkeypatch):
    """No gateway_token: nothing licensed, and no error (the CTA just shows Buy)."""
    from celerp.config import settings as _s
    monkeypatch.setattr(_s, "gateway_token", "")
    headers = await _register(client)
    r = await client.get("/companies/me/modules/licenses", headers=headers)
    assert r.status_code == 200
    assert r.json()["licensed"] == []


@pytest.mark.asyncio
async def test_licenses_relay_error_returns_empty(client, relay_env):
    """A relay error while listing licenses degrades to 'none', never a 500."""
    headers = await _register(client)
    fake = _relay(licenses=_FakeResp(500, {"detail": "boom"}))
    with patch("httpx.AsyncClient", fake):
        r = await client.get("/companies/me/modules/licenses", headers=headers)
    assert r.status_code == 200
    assert r.json()["licensed"] == []
