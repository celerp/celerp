# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""POST /companies/me/modules/marketplace-install - the one-click vault install.

The relay is faked at the httpx boundary; the importer, the premium marker, the
enable step, and every error path run for real. The never-stuck property under
test: each failure returns a clear message and leaves NOTHING half-installed,
so clicking Install again always works.

Credentials: _relay_creds() exchanges settings.gateway_token (the permanent
API key set by a successful /auth/activate) for a short-lived JWT via
POST /auth/token - the SAME pattern celerp.routers.health already uses for
connectors. The fake relay below serves that exchange too.
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
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
    def __init__(self, status_code=200, json_data=None, content=b"", bad_json=False):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("bad", "doc", 0)
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _fake_relay(*, meta=None, install=None, download=None, token=None):
    """An httpx.AsyncClient stand-in serving /auth/token + the three
    marketplace calls (metadata, install, download)."""
    token = token or _FakeResp(200, {"access_token": "relay-jwt-1"})
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
            if url.endswith("/auth/token"):
                return token
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
    from celerp.config import settings as _s
    monkeypatch.setattr(_s, "gateway_token", "api-key-1")
    monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
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
async def test_string_price_does_not_misclassify_free_module_as_paid(client, relay_env):
    """A relay response with price fields as strings (or any non-numeric truthy
    value) must NOT be treated as paid - bare Python truthiness would make
    bool("0") == True and wrongly license-gate a free module forever."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, {"is_official": True, "price_monthly": "0",
                                            "price_once": None}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 200, r.text
    from celerp.modules.importer import PREMIUM_MARKER
    assert not (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


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
async def test_malformed_relay_json_gives_friendly_error(client, relay_env):
    """A 200 with a non-JSON body must not surface as a raw 500 - the endpoint
    should recognize it can't trust the response and say so plainly."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, bad_json=True))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502
    assert "invalid response" in r.json()["detail"].lower()
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_null_token_gives_502_and_never_requests_a_download(client, relay_env):
    """{"token": null} must not slip through: it yields a clean 502, and no
    download is attempted (in particular never a literal 'None' in the URL)."""
    headers = await _register(client)
    requested_urls = []

    token_resp = _FakeResp(200, {"access_token": "relay-jwt-1"})
    meta_resp = _FakeResp(200, {"is_official": True, "price_monthly": 15.0, "price_once": None})
    install_resp = _FakeResp(200, {"token": None})

    class _Fake:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, **kw):
            requested_urls.append(url)
            if "/marketplace/modules/" in url:
                return meta_resp
            return _FakeResp(404, {"detail": "Token not found or already used"})
        async def post(self, url, **kw):
            return token_resp if url.endswith("/auth/token") else install_resp

    with patch("httpx.AsyncClient", _Fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502
    assert not (relay_env / "celerp-budgeting").exists()
    # A null token is caught before any download is fired - no pointless GET,
    # and certainly no "/None" in a URL.
    assert not any("/marketplace/download/" in u for u in requested_urls)


@pytest.mark.asyncio
async def test_non_dict_relay_body_gives_502_not_500(client, relay_env):
    """The relay is a separate service that can drift; a JSON array/string body
    (valid JSON, wrong shape) must not AttributeError into a raw 500."""
    headers = await _register(client)
    token_resp = _FakeResp(200, {"access_token": "relay-jwt-1"})
    # module metadata comes back as a JSON list, not an object
    meta_resp = _FakeResp(200, ["unexpected", "shape"])

    class _Fake:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, **kw):
            return meta_resp
        async def post(self, url, **kw):
            return token_resp

    with patch("httpx.AsyncClient", _Fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502
    assert not (relay_env / "celerp-budgeting").exists()


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
    from celerp.config import settings as _s
    monkeypatch.setattr(_s, "gateway_token", "")
    r = await client.post("/companies/me/modules/marketplace-install",
                          json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 503
    assert "connect an account" in r.json()["detail"]


@pytest.mark.asyncio
async def test_relay_token_exchange_failure_gives_clear_502(client, relay_env):
    """gateway_token is set but the relay rejects the exchange (e.g. it was
    rotated) - a clear message, not a raw 500."""
    headers = await _register(client)
    fake = _fake_relay(token=_FakeResp(401, {"detail": "Invalid API key"}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_token_exchange_200_without_access_token_gives_clear_502(client, relay_env):
    """The exchange returns 200 but the body carries no access_token (unexpected
    shape). It must map to a clean 502, never a KeyError/500 from indexing a
    missing key."""
    headers = await _register(client)
    fake = _fake_relay(token=_FakeResp(200, {"unexpected": "shape"}))
    with patch("httpx.AsyncClient", fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    assert r.status_code == 502
    assert "unexpected" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_retry_after_partial_failure_skips_redownload_and_enables(client, relay_env):
    """Simulates a prior attempt that landed the module on disk but never
    enabled it (e.g. a transient DB error right after install). The endpoint
    must recognize the already-installed package and enable it directly,
    rather than hitting the importer's collision guard with no way out."""
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        r1 = await client.post("/companies/me/modules/marketplace-install",
                               json={"slug": "celerp-budgeting"}, headers=headers)
    assert r1.status_code == 200

    # Retry with a completely broken relay (would 502 on a fresh install) -
    # must still succeed because the module is already on disk correctly.
    class _BrokenFake:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **kw):
            raise AssertionError("must not re-download an already-installed module")
        async def post(self, *a, **kw):
            raise AssertionError("must not re-download an already-installed module")

    with patch("httpx.AsyncClient", _BrokenFake):
        r2 = await client.post("/companies/me/modules/marketplace-install",
                               json={"slug": "celerp-budgeting"}, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["restart_required"] is True


@pytest.mark.asyncio
async def test_foreign_dir_of_same_name_is_not_trusted_as_installed(client, relay_env):
    """A directory that merely shares the slug's NAME but has no valid manifest
    (a crash leftover, or an unrelated folder) must NOT be treated as an existing
    install: the endpoint must fall through to a real download, not silently
    enable whatever is on disk."""
    headers = await _register(client)
    # A same-named dir whose __init__.py has no parseable PLUGIN_MANIFEST.
    broken = relay_env / "celerp-budgeting"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("x = 1  # no PLUGIN_MANIFEST here\n")

    downloaded = {"hit": False}

    class _Fake:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, **kw):
            downloaded["hit"] = True
            if "/marketplace/modules/" in url:
                return _FakeResp(200, {"is_official": True, "price_monthly": None, "price_once": None})
            return _FakeResp(200, content=_zip_bytes())
        async def post(self, url, **kw):
            if url.endswith("/auth/token"):
                return _FakeResp(200, {"access_token": "relay-jwt-1"})
            return _FakeResp(200, {"token": "dl-token"})

    with patch("httpx.AsyncClient", _Fake):
        r = await client.post("/companies/me/modules/marketplace-install",
                              json={"slug": "celerp-budgeting"}, headers=headers)
    # It went to the real download path (didn't trust the broken dir)...
    assert downloaded["hit"] is True
    # ...where the importer's collision guard gives a clear, actionable error
    # rather than silently enabling the foreign directory.
    assert r.status_code in (409, 422, 502)
