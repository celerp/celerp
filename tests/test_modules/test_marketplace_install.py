# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The two-step marketplace install: Download stages, Install lands the package.

POST /companies/me/modules/marketplace-download fetches a licensed archive from
the relay and stages it on disk with a server-owned trust sidecar. POST
/companies/me/modules/marketplace-install imports the staged archive through the
shared importer; the module lands DISABLED, exactly like a community import, so
enabling and restarting stay the deliberate steps in the Installed tab.

The relay is faked at the httpx boundary for the download half; the importer, the
premium marker, the official-prefix gate, and every error path run for real. The
never-stuck property under test: a download failure stages nothing and can be
retried; the official/paid verdict is captured server-side at download time and
cannot be forged by the Install caller, which only hands back an opaque path.

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
    # Stage marketplace downloads inside the test's own data dir.
    monkeypatch.setattr(_s, "data_dir", tmp_path)
    return d


async def _download(client, headers, slug="celerp-budgeting"):
    return await client.post("/companies/me/modules/marketplace-download",
                             json={"slug": slug}, headers=headers)


async def _install(client, headers, path):
    return await client.post("/companies/me/modules/marketplace-install",
                             json={"path": path}, headers=headers)


@pytest.mark.asyncio
async def test_download_stages_then_install_marks_premium_and_lands_disabled(client, relay_env):
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert dl.status_code == 200, dl.text
    path = dl.json()["path"]
    # Staged, not yet installed: the module is not on disk until Install runs.
    from pathlib import Path
    assert Path(path).is_file()
    assert Path(path).with_suffix(".json").is_file()
    assert not (relay_env / "celerp-budgeting").exists()

    r = await _install(client, headers, path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "celerp-budgeting"
    # Install lands the package disabled - no enable, no restart_required.
    assert "restart_required" not in body
    from celerp.modules.importer import PREMIUM_MARKER
    assert (relay_env / "celerp-budgeting" / "__init__.py").exists()
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    # The staged archive and its sidecar are cleaned up once installed.
    assert not Path(path).exists()
    assert not Path(path).with_suffix(".json").exists()


@pytest.mark.asyncio
async def test_lifetime_only_module_is_marked_premium(client, relay_env):
    """A module sold ONLY one-time (price_monthly None, price_once set) is still
    paid, so it must land with the license-gate marker - the verdict is captured
    at download time from price_once."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, {"is_official": True, "price_monthly": None,
                                            "price_once": 79.0}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    r = await _install(client, headers, dl.json()["path"])
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
        dl = await _download(client, headers)
    r = await _install(client, headers, dl.json()["path"])
    assert r.status_code == 200, r.text
    from celerp.modules.importer import PREMIUM_MARKER
    assert not (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


@pytest.mark.asyncio
async def test_relay_refusal_passes_through_and_stages_nothing(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(install=_FakeResp(402, {"detail": "This module requires purchase."}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 402
    assert "requires purchase" in dl.json()["detail"]
    assert not (relay_env.parent / "marketplace-downloads" / "celerp-budgeting.zip").exists()


@pytest.mark.asyncio
async def test_download_failure_is_recoverable(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(download=_FakeResp(404, {"detail": "Token not found or already used"}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert not (relay_env.parent / "marketplace-downloads" / "celerp-budgeting.zip").exists()
    # Retry with a healthy relay stages, then installs - nothing was left behind.
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert dl.status_code == 200
    r = await _install(client, headers, dl.json()["path"])
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_malformed_relay_json_gives_friendly_error(client, relay_env):
    """A 200 with a non-JSON body must not surface as a raw 500 - the download
    should recognize it can't trust the response and say so plainly."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, bad_json=True))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert "invalid response" in dl.json()["detail"].lower()


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
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert not (relay_env.parent / "marketplace-downloads" / "celerp-budgeting.zip").exists()
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
        dl = await _download(client, headers)
    assert dl.status_code == 502


@pytest.mark.asyncio
async def test_mismatched_package_name_removed_and_refused(client, relay_env):
    headers = await _register(client)
    wrong = _MANIFEST.replace("celerp-budgeting", "celerp-imposter")
    fake = _fake_relay(download=_FakeResp(200, content=_zip_bytes(wrong)))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    r = await _install(client, headers, dl.json()["path"])
    assert r.status_code == 422
    assert "does not match" in r.json()["detail"]
    assert not (relay_env / "celerp-imposter").exists()
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_third_party_package_may_not_claim_celerp_prefix(client, relay_env):
    """Relay metadata says NOT official -> a celerp-* package must be refused at
    install, using the official verdict captured in the sidecar at download."""
    headers = await _register(client)
    fake = _fake_relay(meta=_FakeResp(200, {"is_official": False, "price_monthly": 9.0,
                                            "price_once": None}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 200
    r = await _install(client, headers, dl.json()["path"])
    assert r.status_code == 422
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_install_rejects_a_path_outside_the_staging_dir(client, relay_env, tmp_path):
    """The Install caller only ever hands back a staged path; a path pointing
    anywhere else (an attempt to read an arbitrary file) is refused outright."""
    headers = await _register(client)
    outside = tmp_path / "elsewhere.zip"
    outside.write_bytes(_zip_bytes())
    r = await _install(client, headers, str(outside))
    assert r.status_code == 400
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_install_of_a_missing_staged_archive_gives_410(client, relay_env):
    """A staged path that no longer exists (already installed, or swept) tells
    the user to download again rather than 500-ing on a missing file."""
    headers = await _register(client)
    from celerp.config import settings as _s
    from pathlib import Path
    ghost = Path(_s.data_dir) / "marketplace-downloads" / "celerp-budgeting.zip"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    r = await _install(client, headers, str(ghost))
    assert r.status_code == 410
    assert "download it again" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_not_signed_in_gives_clear_503(client, relay_env, monkeypatch):
    headers = await _register(client)
    from celerp.config import settings as _s
    monkeypatch.setattr(_s, "gateway_token", "")
    dl = await _download(client, headers)
    assert dl.status_code == 503
    assert "connect an account" in dl.json()["detail"]


@pytest.mark.asyncio
async def test_relay_token_exchange_failure_gives_clear_502(client, relay_env):
    """gateway_token is set but the relay rejects the exchange (e.g. it was
    rotated) - a clear message, not a raw 500."""
    headers = await _register(client)
    fake = _fake_relay(token=_FakeResp(401, {"detail": "Invalid API key"}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502


@pytest.mark.asyncio
async def test_token_exchange_200_without_access_token_gives_clear_502(client, relay_env):
    """The exchange returns 200 but the body carries no access_token (unexpected
    shape). It must map to a clean 502, never a KeyError/500 from indexing a
    missing key."""
    headers = await _register(client)
    fake = _fake_relay(token=_FakeResp(200, {"unexpected": "shape"}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert "unexpected" in dl.json()["detail"].lower()
