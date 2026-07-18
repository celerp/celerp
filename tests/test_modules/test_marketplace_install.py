# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""POST /companies/me/modules/marketplace-install - the one-click vault install.

The relay is faked at the httpx boundary; the importer, the premium marker, the
enable step, and every error path run for real. The never-stuck property under
test: each failure returns a clear message and leaves NOTHING half-installed,
so clicking Install again always works.
"""
from __future__ import annotations

import io
import os
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.xdist_group("modules_api")

_MANIFEST = '''PLUGIN_MANIFEST = {
    "name": "celerp-budgeting",
    "version": "1.0.0",
    "display_name": "Budgeting",
    "author": "Celerp",
}
'''


def _zip_bytes(manifest: str = _MANIFEST) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__init__.py", manifest)
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _fake_relay(*, meta=None, install=None, download=None):
    """An httpx.AsyncClient stand-in serving the three relay calls."""
    meta = meta or _FakeResp(200, {"is_official": True, "price_monthly": 15.0,
                                   "price_once": None})
    install = install or _FakeResp(200, {"token": "tok-1", "slug": "celerp-budgeting"})
    download = download or _FakeResp(200, content=_zip_bytes())

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return meta if "/marketplace/modules/" in url else download

        async def post(self, url, **kw):
            return install

    return _Fake


async def _register(client) -> dict:
    email = f"mp-install-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={
        "company_name": "MP Install Co", "email": email, "name": "Admin",
        "password": "pw123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def relay_env(tmp_path, monkeypatch):
    d = tmp_path / "modules"
    d.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(d))
    monkeypatch.setenv("CELERP_RELAY_URL", "https://relay.test")
    monkeypatch.setenv("CELERP_INSTANCE_JWT", "jwt-1")
    return d


@pytest.mark.asyncio
async def test_install_downloads_enables_and_marks_premium(client, relay_env):
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restart_required"] is True
    assert body["name"] == "celerp-budgeting"
    from celerp.modules.importer import PREMIUM_MARKER
    assert (relay_env / "celerp-budgeting" / "__init__.py").exists()
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


@pytest.mark.asyncio
async def test_lifetime_only_module_is_marked_premium(client, relay_env):
    """A module sold ONLY one-time (price_monthly None, price_once set) is still
    paid, so it must land with the license-gate marker - regression for the
    ModuleOut that used to omit price_once."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, {"is_official": True, "price_monthly": None,
                                            "price_once": 79.0}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 200, r.text
    from celerp.modules.importer import PREMIUM_MARKER
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


@pytest.mark.asyncio
async def test_relay_refusal_passes_through_and_installs_nothing(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(install=_FakeResp(402, {"detail": "This module requires purchase."}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 402
    assert "requires purchase" in r.json()["detail"]
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_download_failure_is_recoverable(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(download=_FakeResp(404, {"detail": "Token not found or already used"}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502
    assert not (relay_env / "celerp-budgeting").exists()
    # Retry with a healthy relay succeeds - nothing was left half-installed.
    with patch("httpx.AsyncClient", _fake_relay()):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_mismatched_package_name_removed_and_refused(client, relay_env):
    headers = await _register(client)
    wrong = _MANIFEST.replace("celerp-budgeting", "celerp-imposter")
    fake = _fake_relay(download=_FakeResp(200, content=_zip_bytes(wrong)))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 422
    assert "does not match" in r.json()["detail"]
    assert not (relay_env / "celerp-imposter").exists()
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_third_party_package_may_not_claim_celerp_prefix(client, relay_env):
    """Relay metadata says NOT official -> a celerp-* package must be refused."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, {"is_official": False, "price_monthly": 9.0,
                                            "price_once": None}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 422
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_not_signed_in_gives_clear_503(client, relay_env, monkeypatch):
    headers = await _register(client)
    monkeypatch.delenv("CELERP_INSTANCE_JWT")
    r = await client.post("/companies/me/modules/marketplace-install",
                          json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 503
    assert "connect an account" in r.json()["detail"]
