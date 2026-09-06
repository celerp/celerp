# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""PR #323 infra-form hardening: structural DB URL handling, blank-password
preservation, port validation, S3 backend/endpoint allowlisting, safe DB error
text, test-with-stored-secret fallback, and packaged restore-button visibility.

Packaged behavior is driven by CELERP_DATA_DIR + celerp-config.json; the
self-hosted path keeps config.toml. Tests that exercise the self-hosted branch
set CELERP_CONFIG to a tmp path so they never touch the real user config, and
patch subprocess.Popen so no real pkill fires.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fasthtml.common import FastHTML, to_xml

import ui.routes.settings_cloud as sc
from ui.i18n import t


# ── helpers ──────────────────────────────────────────────────────────────────

def _sandbox_toml(monkeypatch, tmp_path, body=""):
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setenv("CELERP_CONFIG", str(path))
    return path


@pytest_asyncio.fixture
async def client(monkeypatch):
    """A minimal FastHTML app carrying the settings-cloud routes, with the
    permission gate opened and a token present so handlers run their bodies."""
    monkeypatch.setattr(sc, "_check_permission", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(sc, "_token", lambda req: "tok", raising=False)
    app = FastHTML()
    sc.setup_routes(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t",
                           follow_redirects=False) as c:
        yield c


# ── structural DB URL round-trip (§49/M2, M3) ────────────────────────────────

def test_build_db_url_special_char_password_round_trips():
    """A password containing '@' and '/' must not corrupt the URL the way a
    plain f-string DSN would (the '@' would be read as the host separator)."""
    url = sc._build_db_url(host="db.example.com", port=5432, name="celerp",
                            user="celerp", password="p@ss/word")
    parsed = sc._parse_db_url(url)
    assert parsed["host"] == "db.example.com"
    assert parsed["name"] == "celerp"
    assert parsed["user"] == "celerp"
    assert parsed["has_password"] is True
    assert sc._url_password(url) == "p@ss/word"


def test_build_db_url_ipv6_host_round_trips():
    """An IPv6 literal host must survive being built and re-parsed; a naive
    string-split on ':' would shred it."""
    url = sc._build_db_url(host="::1", port=5432, name="celerp",
                            user="celerp", password="pw")
    parsed = sc._parse_db_url(url)
    assert parsed["host"] == "::1"
    assert parsed["port"] == "5432"


def test_masked_db_url_hides_special_char_password():
    """The redacted display form never leaks a password even when it contains
    the '@' character a naive string.replace-based mask could mishandle."""
    url = sc._build_db_url(host="h", port=5432, name="db", user="u", password="p@ss")
    masked = sc._masked_db_url(url)
    assert "p@ss" not in masked
    assert "***" in masked


def test_url_password_none_when_absent():
    url = sc._build_db_url(host="h", port=5432, name="db", user="u", password=None)
    assert sc._url_password(url) is None
    assert sc._parse_db_url(url)["has_password"] is False


# ── blank-password preservation (§49/M1) ─────────────────────────────────────

async def test_save_infra_packaged_blank_password_preserves_existing(client, tmp_path, monkeypatch):
    """Saving with the password field left blank keeps the previously
    configured password rather than wiping it to an empty string."""
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    old = "postgresql+asyncpg://celerp:secretpw@old.example.com:5432/celerp"
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "external", "external_db_url": old}))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "new.example.com", "db_name": "celerp", "db_user": "celerp",
        "db_pass": "",
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert sc._url_password(cfg["external_db_url"]) == "secretpw"


async def test_save_infra_packaged_clear_password_checkbox_wipes_it(client, tmp_path, monkeypatch):
    """The explicit db_clear_password checkbox is the only way to actually
    clear a saved password."""
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    old = "postgresql+asyncpg://celerp:secretpw@old.example.com:5432/celerp"
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "external", "external_db_url": old}))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "new.example.com", "db_name": "celerp", "db_user": "celerp",
        "db_pass": "", "db_clear_password": "1",
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert sc._url_password(cfg["external_db_url"]) is None


async def test_save_infra_selfhosted_blank_password_preserves_existing(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    old = "postgresql+asyncpg://celerp:secretpw@old.example.com:5432/celerp"
    _sandbox_toml(monkeypatch, tmp_path,
                  f'[database]\nurl = "{old}"\n')

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "new.example.com", "db_name": "celerp", "db_user": "celerp",
        "db_pass": "",
    })
    assert r.status_code == 200
    import tomllib
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert sc._url_password(cfg["database"]["url"]) == "secretpw"


# ── port validation (§51/M4) ──────────────────────────────────────────────────

async def test_save_infra_packaged_rejects_invalid_port(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "h", "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
        "db_port": "not-a-number",
    })
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["db_mode"] == "local"


async def test_save_infra_packaged_rejects_out_of_range_port(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "h", "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
        "db_port": "99999",
    })
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text


async def test_cloud_test_db_rejects_invalid_port(client):
    r = await client.post("/settings/cloud/test-db", data={
        "db_host": "h", "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
        "db_port": "abc",
    })
    assert r.status_code == 200
    assert t("settings_cloud.invalid_port") in r.text


# ── safe DB error text (§51/M4) ───────────────────────────────────────────────

async def test_cloud_test_db_does_not_leak_raw_driver_error(client, monkeypatch):
    """A raw driver exception (which can restate host/db/user) must never reach
    the response; only the generic translated message is shown."""
    async def _boom(*a, **k):
        raise RuntimeError("password authentication failed for user \"celerp\" at host secret-internal-host")

    monkeypatch.setattr(sc, "_try_db_connect", _boom)
    r = await client.post("/settings/cloud/test-db", data={
        "db_host": "secret-internal-host", "db_name": "celerp", "db_user": "celerp",
        "db_pass": "pw",
    })
    assert r.status_code == 200
    assert "secret-internal-host" not in r.text
    assert "password authentication failed" not in r.text
    assert t("settings_cloud.db_connection_failed") in r.text


# ── S3 backend allowlist + HTTPS-only endpoint (§52/M5) ──────────────────────

async def test_cloud_test_storage_rejects_unknown_backend(client):
    r = await client.post("/settings/cloud/test-storage", data={"storage_backend": "ftp"})
    assert r.status_code == 200
    assert t("settings_cloud.invalid_storage_backend") in r.text


async def test_cloud_test_storage_rejects_http_endpoint(client):
    r = await client.post("/settings/cloud/test-storage", data={
        "storage_backend": "s3", "s3_endpoint": "http://s3.example.com",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    })
    assert r.status_code == 200
    assert t("settings_cloud.invalid_s3_endpoint") in r.text


async def test_save_infra_packaged_rejects_unknown_backend(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(json.dumps({"storage_mode": "local"}))

    r = await client.post("/settings/cloud/save-infra", data={"storage_backend": "ftp"})
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["storage_mode"] == "local"


async def test_save_infra_packaged_rejects_http_s3_endpoint(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(json.dumps({"storage_mode": "local"}))

    r = await client.post("/settings/cloud/save-infra", data={
        "storage_backend": "s3", "s3_endpoint": "http://s3.example.com",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    })
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["storage_mode"] == "local"


async def test_save_infra_selfhosted_rejects_http_s3_endpoint(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    _sandbox_toml(monkeypatch, tmp_path, '[storage]\nbackend = "local"\n')

    r = await client.post("/settings/cloud/save-infra", data={
        "storage_backend": "s3", "s3_endpoint": "http://s3.example.com",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    })
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text
    import tomllib
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["storage"]["backend"] == "local"


# ── test-with-stored-secret fallback (review-miss) ───────────────────────────

async def test_cloud_test_db_falls_back_to_stored_password(client, monkeypatch):
    """Leaving the password field blank on a connectivity test uses the
    already-configured password instead of trying an empty one."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "database_url",
                         "postgresql+asyncpg://celerp:storedpw@h:5432/celerp", raising=False)

    seen = {}

    async def _fake_connect(host, port, name, user, password):
        seen["password"] = password

    monkeypatch.setattr(sc, "_try_db_connect", _fake_connect)
    r = await client.post("/settings/cloud/test-db", data={
        "db_host": "h", "db_name": "celerp", "db_user": "celerp", "db_pass": "",
    })
    assert r.status_code == 200
    assert seen["password"] == "storedpw"


async def test_cloud_test_storage_falls_back_to_stored_secret(client, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "storage_s3_secret_key", "storedsecret", raising=False)

    seen = {}

    async def _fake_connect(endpoint, bucket, access_key, secret_key):
        seen["secret"] = secret_key
        return "ok"

    monkeypatch.setattr(sc, "_try_s3_connect", _fake_connect)
    r = await client.post("/settings/cloud/test-storage", data={
        "storage_backend": "s3", "s3_endpoint": "https://s3.example.com",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "",
    })
    assert r.status_code == 200
    assert seen["secret"] == "storedsecret"


# ── packaged restore-button visibility (§50/M6) ──────────────────────────────

def test_infra_db_section_shows_restore_in_packaged_mode(tmp_path, monkeypatch):
    """A packaged install (CELERP_DATA_DIR set) with a backup URL in
    celerp-config.json must render the restore button; reading only the
    self-hosted config.toml path hid it for every packaged install."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://celerp:new@h:5432/celerp",
        "external_db_url_backup": "postgresql+asyncpg://celerp:old@old:5432/celerp",
    }))
    html = to_xml(sc._infra_db_section())
    assert 'id="restore-db-result"' in html


def test_infra_db_section_hides_restore_in_packaged_mode_without_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "local", "external_db_url": "",
    }))
    html = to_xml(sc._infra_db_section())
    assert 'id="restore-db-result"' not in html
