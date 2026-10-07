# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The two-step marketplace install: Download stages, Install lands the package.

POST /companies/me/modules/marketplace-download fetches an archive from the
relay and stages it under a new reference together with the package's slug,
version, SHA-256 and install details. POST
/companies/me/modules/marketplace-install imports the staged archive named by
that reference through the shared importer; the module lands DISABLED, exactly
like a community import, so enabling and restarting stay the deliberate steps in
the Installed tab.

The relay is faked at the httpx boundary for the download half; the importer, the
premium marker, the official-prefix gate, and every error path run for real.
Covered: a download stages nothing unless the package matches the install answer;
each download has its own stage; Install lands only the package its stage holds;
and an expired, malformed, missing or incomplete stage asks for another download.

Credentials: _relay_creds() exchanges settings.gateway_token (the permanent
API key set by a successful /auth/activate) for a short-lived JWT via
POST /auth/token - the SAME pattern celerp.routers.health already uses for
connectors. The fake relay below serves that exchange too.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _answer(*, data: bytes | None = None, token="tok-1", is_official=True, is_paid=True,
            **extra) -> dict:
    """An install answer describing *data* (the default package unless given)."""
    data = _zip_bytes() if data is None else data
    return {"token": token, "slug": "celerp-budgeting", "version": "1.0.0",
            "is_official": is_official, "is_paid": is_paid, "sha256": _sha(data), **extra}


def _install_answer(**kw) -> "_FakeResp":
    return _FakeResp(200, _answer(**kw))


# A module-detail answer that differs from the install answer (free and official).
_DETAIL_SAYS_FREE = _FakeResp(200, {"is_official": True, "price_monthly": None, "price_once": None})


def _fake_relay(*, install=None, download=None, token=None, urls=None):
    """An httpx.AsyncClient stand-in serving /auth/token, the install request and
    the download. Every GET url is appended to *urls*. The default install answer
    describes whatever the download serves."""
    token = token or _FakeResp(200, {"access_token": "relay-jwt-1"})
    download = download or _FakeResp(200, content=_zip_bytes())
    install = install or _install_answer(data=download.content)
    urls = [] if urls is None else urls

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            urls.append(url)
            return _DETAIL_SAYS_FREE if "/marketplace/modules/" in url else download

        async def post(self, url, **kw):
            if url.endswith("/auth/token"):
                return token
            return install

    return _Fake


async def _register(client) -> dict:
    email = f"mp-install-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={
        "company_name": "MP Install Co", "email": email, "name": "Admin",
        "password": "pw123val"})
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


def _staged(relay_env) -> list:
    """Everything in the marketplace download area."""
    d = relay_env.parent / "marketplace-downloads"
    return sorted(d.iterdir()) if d.is_dir() else []


async def _download(client, headers, slug="celerp-budgeting"):
    return await client.post("/companies/me/modules/marketplace-download",
                             json={"slug": slug}, headers=headers)


async def _install(client, headers, ref):
    return await client.post("/companies/me/modules/marketplace-install",
                             json={"ref": ref}, headers=headers)


def _uninstall(name="celerp-budgeting") -> None:
    from celerp.modules.importer import remove_module_dir
    remove_module_dir(name)


@pytest.mark.asyncio
async def test_download_stages_then_install_marks_premium_and_lands_disabled(client, relay_env):
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert dl.status_code == 200, dl.text
    ref = dl.json()["ref"]
    # Staged, not yet installed: the module is not on disk until Install runs.
    assert _staged(relay_env)
    assert not (relay_env / "celerp-budgeting").exists()

    r = await _install(client, headers, ref)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "celerp-budgeting"
    # Install lands the package disabled - no enable, no restart_required.
    assert "restart_required" not in body
    from celerp.modules.importer import PREMIUM_MARKER
    assert (relay_env / "celerp-budgeting" / "__init__.py").exists()
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    # The stage is removed once installed.
    assert _staged(relay_env) == []


@pytest.mark.asyncio
async def test_paid_install_requires_the_normal_license_path(client, relay_env, tmp_path):
    """A paid install requires the normal license path: it lands with no free
    verdict, and nothing else is asked."""
    headers = await _register(client)
    urls: list = []
    with patch("httpx.AsyncClient", _fake_relay(urls=urls)):
        dl = await _download(client, headers)
    assert dl.status_code == 200, dl.text
    assert urls == ["https://relay.test/marketplace/download/tok-1"]
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 200, r.text
    from celerp.modules.importer import PREMIUM_MARKER
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    assert not (tmp_path / "license_cache" / "celerp-budgeting.free.json").exists()


_DROP = object()


@pytest.mark.parametrize("change", [
    {"is_paid": _DROP},
    {"is_official": _DROP},
    {"is_paid": None},
    {"is_paid": 0},
    {"is_paid": "false"},
    {"is_official": 1},
    {"is_official": "true"},
    {"slug": _DROP},
    {"version": _DROP},
    {"version": ""},
    {"version": 1},
    {"sha256": _DROP},
    {"sha256": "ABC" + "0" * 61},
    {"sha256": "0" * 63},
    {"sha256": None},
    {"token": _DROP},
    {"token": ""},
    {"token": 1},
    {"slug": None},
], ids=["no_is_paid", "no_is_official", "paid_null", "paid_zero", "paid_str", "official_int",
        "official_str", "no_slug", "no_version", "empty_version", "int_version", "no_sha256",
        "upper_sha256", "short_sha256", "null_sha256", "no_token", "empty_token", "int_token",
        "null_slug"])
@pytest.mark.asyncio
async def test_install_answer_of_any_other_shape_stages_nothing(client, relay_env, change):
    """Unless every expected field of the install answer is present with a plain
    type, the download fails and nothing else is asked."""
    headers = await _register(client)
    urls: list = []
    answer = _answer()
    for key, value in change.items():
        if value is _DROP:
            answer.pop(key)
        else:
            answer[key] = value
    with patch("httpx.AsyncClient", _fake_relay(install=_FakeResp(200, answer), urls=urls)):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert "invalid response" in dl.json()["detail"].lower()
    assert urls == []
    assert _staged(relay_env) == []


@pytest.mark.asyncio
async def test_install_answer_with_an_unknown_field_stages_the_module(client, relay_env):
    """A field the install answer adds beyond the expected ones does not stop the download."""
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay(install=_install_answer(new_field="value"))):
        dl = await _download(client, headers)
    assert dl.status_code == 200, dl.text
    assert len(_staged(relay_env)) == 1


def test_install_answer_keeps_only_the_expected_fields():
    from celerp.routers.companies import _install_answer as read_install_answer

    answer = _answer(new_field="value")

    assert read_install_answer(answer, "celerp-budgeting") == {
        key: answer[key] for key in ("token", "slug", "version", "is_official", "is_paid", "sha256")}


@pytest.mark.asyncio
async def test_install_answer_for_another_module_stages_nothing(client, relay_env):
    """An install answer naming a different module than the one requested is
    refused before anything is downloaded."""
    headers = await _register(client)
    urls: list = []
    fake = _fake_relay(install=_install_answer(slug="celerp-other"), urls=urls)
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert urls == []
    assert _staged(relay_env) == []


@pytest.mark.asyncio
async def test_downloaded_bytes_must_match_the_digest(client, relay_env):
    """Bytes whose SHA-256 differs from the install answer's digest are refused and
    nothing is staged; a fresh download then works."""
    headers = await _register(client)
    other = _zip_bytes(_MANIFEST.replace("Budgeting", "Budgets"))
    fake = _fake_relay(install=_install_answer(data=_zip_bytes()),
                       download=_FakeResp(200, content=other))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert "try again" in dl.json()["detail"].lower()
    assert _staged(relay_env) == []
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert (await _install(client, headers, dl.json()["ref"])).status_code == 200


@pytest.mark.asyncio
async def test_relay_refusal_passes_through_and_stages_nothing(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(install=_FakeResp(402, {"detail": "This module requires purchase."}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 402
    assert "requires purchase" in dl.json()["detail"]
    assert _staged(relay_env) == []


@pytest.mark.asyncio
async def test_download_failure_is_recoverable(client, relay_env):
    headers = await _register(client)
    fake = _fake_relay(download=_FakeResp(404, {"detail": "Token not found or already used"}))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert _staged(relay_env) == []
    # Retry with a healthy relay stages, then installs - nothing was left behind.
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert dl.status_code == 200
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_malformed_relay_json_gives_friendly_error(client, relay_env):
    """A 200 with a non-JSON body must not surface as a raw 500 - the download
    should report an invalid response in plain words."""
    headers = await _register(client)
    fake = _fake_relay(install=_FakeResp(200, bad_json=True))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert "invalid response" in dl.json()["detail"].lower()


@pytest.mark.asyncio
async def test_null_token_gives_502_and_never_requests_a_download(client, relay_env):
    """{"token": null} must not slip through: it yields a clean 502, and no
    download is attempted (in particular never a literal 'None' in the URL)."""
    headers = await _register(client)
    requested_urls: list = []
    _Fake = _fake_relay(install=_install_answer(token=None), urls=requested_urls)

    with patch("httpx.AsyncClient", _Fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502
    assert _staged(relay_env) == []
    # A null token is caught before any download is fired - no pointless GET,
    # and certainly no "/None" in a URL.
    assert not any("/marketplace/download/" in u for u in requested_urls)


@pytest.mark.asyncio
async def test_non_dict_relay_body_gives_502_not_500(client, relay_env):
    """The relay is a separate service that can drift; a JSON array/string body
    (valid JSON, wrong shape) must not AttributeError into a raw 500."""
    headers = await _register(client)
    # the install answer comes back as a JSON list, not an object
    _Fake = _fake_relay(install=_FakeResp(200, ["unexpected", "shape"]))

    with patch("httpx.AsyncClient", _Fake):
        dl = await _download(client, headers)
    assert dl.status_code == 502


@pytest.mark.asyncio
async def test_package_named_differently_from_its_module_is_refused(client, relay_env):
    headers = await _register(client)
    wrong = _MANIFEST.replace("celerp-budgeting", "celerp-imposter")
    fake = _fake_relay(download=_FakeResp(200, content=_zip_bytes(wrong)))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 422
    assert "does not match" in r.json()["detail"]
    assert not (relay_env / "celerp-imposter").exists()
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_package_version_must_match_the_listed_version(client, relay_env, tmp_path):
    """A package whose manifest version differs from the version it was listed
    with is refused, and nothing of it is installed."""
    headers = await _register(client)
    data = _zip_bytes(_MANIFEST.replace('"1.0.0"', '"1.1.0"'))
    fake = _fake_relay(install=_install_answer(data=data, is_paid=False),
                       download=_FakeResp(200, content=data))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 200, dl.text
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 422
    assert "does not match" in r.json()["detail"]
    assert not (relay_env / "celerp-budgeting").exists()
    assert not (tmp_path / "license_cache" / "celerp-budgeting.free.json").exists()


@pytest.mark.asyncio
async def test_celerp_prefixed_package_that_is_not_official_is_refused(client, relay_env):
    """A celerp-* package whose download is not official is refused at install."""
    headers = await _register(client)
    fake = _fake_relay(install=_install_answer(is_official=False))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    assert dl.status_code == 200
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 422
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.parametrize("ref", [
    "", "mp_", "mp_" + "0" * 31, "mp_" + "0" * 33, "mp_" + "A" * 32, "imp_" + "0" * 32,
    "../mp_" + "0" * 32, "mp_" + "0" * 32 + "/../x", "celerp-budgeting.zip",
], ids=["empty", "bare_prefix", "short", "long", "upper", "other_prefix", "parent",
        "suffix", "filename"])
@pytest.mark.asyncio
async def test_install_refuses_a_malformed_reference(client, relay_env, tmp_path, ref):
    """Install accepts only a well-formed download reference; anything else,
    including a file name or a path, is refused."""
    headers = await _register(client)
    (tmp_path / "celerp-budgeting.zip").write_bytes(_zip_bytes())
    r = await _install(client, headers, ref)
    assert r.status_code == 400
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_install_refuses_a_path_where_a_reference_belongs(client, relay_env, tmp_path):
    headers = await _register(client)
    outside = tmp_path / "elsewhere.zip"
    outside.write_bytes(_zip_bytes())
    r = await client.post("/companies/me/modules/marketplace-install",
                          json={"path": str(outside)}, headers=headers)
    assert r.status_code == 422
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_install_of_an_unknown_reference_asks_for_a_new_download(client, relay_env):
    """A well-formed reference with no stage (already installed, expired and
    swept, or never issued) tells the user to download again."""
    headers = await _register(client)
    r = await _install(client, headers, "mp_" + "0" * 32)
    assert r.status_code == 410
    assert "download it again" in r.json()["detail"].lower()


def _no_details(package: Path, details: Path) -> None:
    details.unlink()


def _no_package(package: Path, details: Path) -> None:
    package.unlink()


def _details_short_a_field(package: Path, details: Path) -> None:
    meta = json.loads(details.read_text())
    meta.pop("version")
    details.write_text(json.dumps(meta))


def _details_with_a_text_flag(package: Path, details: Path) -> None:
    meta = json.loads(details.read_text())
    meta["is_paid"] = "false"
    details.write_text(json.dumps(meta))


def _unreadable_details(package: Path, details: Path) -> None:
    details.write_text("{not json")


def _package_differs_from_its_digest(package: Path, details: Path) -> None:
    package.write_bytes(_zip_bytes(_MANIFEST.replace("Budgeting", "Budgets")))


@pytest.mark.parametrize("damage", [
    _no_details, _no_package, _details_short_a_field, _details_with_a_text_flag,
    _unreadable_details, _package_differs_from_its_digest,
], ids=lambda f: f.__name__.strip("_"))
@pytest.mark.asyncio
async def test_install_of_an_incomplete_stage_asks_for_a_new_download(client, relay_env, damage):
    """A stage missing its package or its details, or whose package no longer
    matches its recorded digest, installs nothing and asks for a new download."""
    from celerp.modules import marketplace_stage

    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    damage(*marketplace_stage.stage_paths(dl.json()["ref"]))
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 410
    assert "download it again" in r.json()["detail"].lower()
    assert not (relay_env / "celerp-budgeting").exists()


@pytest.mark.asyncio
async def test_install_of_an_expired_stage_asks_for_a_new_download(client, relay_env, monkeypatch):
    """A stage older than the download window installs nothing, is removed, and
    a fresh download installs normally."""
    from celerp.modules import marketplace_stage

    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    with monkeypatch.context() as m:
        m.setattr(marketplace_stage, "TTL_SECONDS", -1)
        r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 410
    assert "download it again" in r.json()["detail"].lower()
    assert not (relay_env / "celerp-budgeting").exists()
    assert _staged(relay_env) == []
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    assert (await _install(client, headers, dl.json()["ref"])).status_code == 200


@pytest.mark.asyncio
async def test_retry_after_a_recoverable_install_error_uses_the_same_download(client, relay_env):
    """An Install that fails for a reason the user can fix keeps the download, so
    the same Install succeeds once the cause is gone."""
    headers = await _register(client)
    with patch("httpx.AsyncClient", _fake_relay()):
        dl = await _download(client, headers)
    ref = dl.json()["ref"]
    blocker = relay_env / "celerp-budgeting"
    blocker.mkdir()
    (blocker / "notes.txt").write_text("copied in by hand")
    r = await _install(client, headers, ref)
    assert r.status_code == 422
    assert "already exists" in r.json()["detail"]
    (blocker / "notes.txt").unlink()
    blocker.rmdir()
    r = await _install(client, headers, ref)
    assert r.status_code == 200, r.text
    assert (relay_env / "celerp-budgeting" / "__init__.py").exists()
    assert _staged(relay_env) == []


def _package(version: str) -> bytes:
    return _zip_bytes(_MANIFEST.replace('"1.0.0"', f'"{version}"'))


@pytest.mark.asyncio
async def test_two_downloads_of_one_module_each_keep_their_own_package(client, relay_env):
    """Two downloads of the same module stay independent: each reference still
    installs exactly the package and details it was downloaded with."""
    from celerp.modules.importer import PREMIUM_MARKER

    headers = await _register(client)
    refs = []
    for version, paid in (("1.0.0", True), ("2.0.0", False)):
        data = _package(version)
        fake = _fake_relay(install=_install_answer(data=data, version=version, is_paid=paid),
                           download=_FakeResp(200, content=data))
        with patch("httpx.AsyncClient", fake):
            dl = await _download(client, headers)
        assert dl.status_code == 200, dl.text
        refs.append(dl.json()["ref"])
    assert refs[0] != refs[1]

    r = await _install(client, headers, refs[1])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "2.0.0"
    assert not (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    _uninstall()
    r = await _install(client, headers, refs[0])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


@pytest.mark.asyncio
async def test_overlapping_downloads_of_one_module_each_install_their_own_package(
        client, relay_env, tmp_path):
    """Download A starts first and receives its package only after download B has
    finished. Each reference installs its own package with its own details."""
    from celerp.modules.importer import PREMIUM_MARKER

    headers = await _register(client)
    pkg_a, pkg_b = _package("1.0.0"), _package("2.0.0")
    answers = [
        _install_answer(data=pkg_a, token="tok-a", version="1.0.0", is_paid=True),
        _install_answer(data=pkg_b, token="tok-b", version="2.0.0", is_paid=False),
    ]
    release_a = asyncio.Event()

    class _Fake:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if url.endswith("/tok-a"):
                await release_a.wait()
                return _FakeResp(200, content=pkg_a)
            return _FakeResp(200, content=pkg_b)

        async def post(self, url, **kw):
            if url.endswith("/auth/token"):
                return _FakeResp(200, {"access_token": "relay-jwt-1"})
            return answers.pop(0)

    with patch("httpx.AsyncClient", _Fake):
        first = asyncio.create_task(_download(client, headers))
        async with asyncio.timeout(10):
            while len(answers) == 2:
                await asyncio.sleep(0.01)
        dl_b = await _download(client, headers)
        release_a.set()
        dl_a = await first
    assert dl_a.status_code == 200 and dl_b.status_code == 200, (dl_a.text, dl_b.text)
    assert dl_a.json()["ref"] != dl_b.json()["ref"]

    r = await _install(client, headers, dl_b.json()["ref"])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "2.0.0"
    assert not (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    assert (tmp_path / "license_cache" / "celerp-budgeting.free.json").is_file()
    _uninstall()
    r = await _install(client, headers, dl_a.json()["ref"])
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"
    assert (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()


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


@pytest.mark.asyncio
async def test_install_of_a_free_official_module_keeps_it_loading_offline(
        client, relay_env, tmp_path, monkeypatch):
    """Install is online by definition, so it records the free verdict there; the
    module then loads on a later start with the relay out of reach."""
    from celerp.modules import loader

    headers = await _register(client)
    fake = _fake_relay(install=_install_answer(is_paid=False))
    with patch("httpx.AsyncClient", fake):
        dl = await _download(client, headers)
    r = await _install(client, headers, dl.json()["ref"])
    assert r.status_code == 200, r.text
    from celerp.modules.importer import PREMIUM_MARKER
    assert not (relay_env / "celerp-budgeting" / PREMIUM_MARKER).exists()
    assert (tmp_path / "license_cache" / "celerp-budgeting.free.json").is_file()

    def _offline(*a, **kw):
        raise OSError("unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _offline)
    monkeypatch.setattr(loader, "exchange_api_key_for_jwt", lambda *a, **k: None)
    admission = loader.admit_modules(str(relay_env), {"celerp-budgeting"})
    assert [m.name for m in admission.admitted] == ["celerp-budgeting"]
