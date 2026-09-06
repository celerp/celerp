# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""PR #313: packaged infra-save apply, DB/storage grace symmetry, S3 signed
connectivity test, and the two app-local defects.

Packaged behavior is driven by CELERP_DATA_DIR + celerp-config.json (the
Electron-owned source of truth); the self-hosted path keeps config.toml. Tests
that must sandbox the self-hosted branch set CELERP_CONFIG to a tmp path so they
never touch the real user config, and patch subprocess.Popen so no real pkill
fires.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fasthtml.common import FastHTML, to_xml

import ui.routes.settings_cloud as sc
from celerp.services import attachments
from ui.i18n import t


# ── helpers ──────────────────────────────────────────────────────────────────

def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()


class _ACM:
    """Minimal async context manager yielding a preset client."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


def _inject_botocore(monkeypatch):
    """Inject a fake botocore.exceptions so _try_s3_connect's function-level
    `from botocore.exceptions import ...` resolves and the exception classes the
    test raises are the same objects the handler catches."""
    root = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, response, operation_name="HeadBucket"):
            self.response = response
            self.operation_name = operation_name
            super().__init__(str(response))

    class EndpointConnectionError(Exception):
        def __init__(self, **kwargs):
            super().__init__("endpoint connection error")

    class ConnectTimeoutError(Exception):
        pass

    class ReadTimeoutError(Exception):
        pass

    exc.ClientError = ClientError
    exc.EndpointConnectionError = EndpointConnectionError
    exc.ConnectTimeoutError = ConnectTimeoutError
    exc.ReadTimeoutError = ReadTimeoutError
    root.exceptions = exc
    monkeypatch.setitem(sys.modules, "botocore", root)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exc)
    return exc


def _inject_aiobotocore(monkeypatch, client):
    """Inject a fake aiobotocore whose create_client yields the given client."""
    session_obj = MagicMock()
    session_obj.create_client = MagicMock(side_effect=lambda *a, **k: _ACM(client))
    session_mod = types.ModuleType("aiobotocore.session")
    session_mod.get_session = MagicMock(return_value=session_obj)
    root = types.ModuleType("aiobotocore")
    root.session = session_mod
    monkeypatch.setitem(sys.modules, "aiobotocore", root)
    monkeypatch.setitem(sys.modules, "aiobotocore.session", session_mod)


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


def _sandbox_toml(monkeypatch, tmp_path, body=""):
    """Point the self-hosted config.toml at a tmp path so the merge-base branch
    never touches the real user config."""
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setenv("CELERP_CONFIG", str(path))
    return path


# ── packaged save / restore (DECISION 1) ─────────────────────────────────────

async def test_save_infra_packaged_writes_config_json(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))
    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["db_mode"] == "external"
    assert cfg["external_db_url"] == "postgresql+asyncpg://celerp:pw@db.example.com:5432/celerp"


async def test_save_infra_packaged_no_pkill(client, tmp_path, monkeypatch):
    import subprocess
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path,
                  '[database]\nurl = "postgresql+asyncpg://celerp:old@old:5432/celerp"\n')
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))
    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "new.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    })
    assert r.status_code == 200
    popen.assert_not_called()


async def test_save_infra_packaged_storage_keys(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    (tmp_path / "celerp-config.json").write_text(json.dumps({"storage_mode": "local"}))
    r = await client.post("/settings/cloud/save-infra", data={
        "storage_backend": "s3", "s3_endpoint": "https://s3.x",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["storage_mode"] == "s3"
    assert cfg["storage_s3_endpoint"] == "https://s3.x"
    assert cfg["storage_s3_bucket"] == "bkt"
    assert cfg["storage_s3_access_key"] == "AK"
    assert cfg["storage_s3_secret_key"] == "SK"


async def test_save_infra_packaged_reports_write_failure(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    monkeypatch.setattr(sc, "merge_packaged_config", MagicMock(return_value=False))
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))
    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "h.example.com", "db_name": "celerp",
        "db_user": "celerp", "db_pass": "pw",
    })
    assert r.status_code == 200
    assert "infra-test-result--err" in r.text
    assert t("settings.no_config_file_found") not in r.text
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["db_mode"] == "local"


async def test_save_infra_packaged_backs_up_prev_url(client, tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path)
    old = "postgresql+asyncpg://celerp:old@old:5432/celerp"
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "external", "external_db_url": old}))
    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "new.example.com", "db_name": "celerp",
        "db_user": "celerp", "db_pass": "pw",
    })
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["external_db_url_backup"] == old
    assert cfg["external_db_url"] == "postgresql+asyncpg://celerp:pw@new.example.com:5432/celerp"


async def test_restore_db_packaged_uses_config_json(client, tmp_path, monkeypatch):
    import subprocess
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _sandbox_toml(monkeypatch, tmp_path,
                  '[database]\nurl = "NEWURL"\n[database_backup]\nprevious_url = "OLDURL"\n')
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external", "external_db_url": "NEWURL",
        "external_db_url_backup": "OLDURL"}))
    r = await client.post("/settings/cloud/restore-db", data={})
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["external_db_url"] == "OLDURL"
    assert cfg["external_db_url_backup"] == "NEWURL"
    popen.assert_not_called()


# ── storage grace / visibility symmetry (DECISION 2) ─────────────────────────

def test_packaged_state_surfaces_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "local", "storage_mode": "s3",
        "storage_s3_bucket": "bkt", "storage_s3_endpoint": "https://s3.x",
        "storage_s3_access_key": "AK", "storage_s3_secret_key": "SK",
        "feature_flags": {"external_storage": False, "grace_period_ends": _future()},
    }))
    from celerp.gateway.state import get_packaged_db_state
    st = get_packaged_db_state()
    assert st["storage_mode"] == "s3"
    assert st["has_external_storage"] is True
    assert st["external_storage_entitled"] is False
    assert st["storage_in_grace"] is True


def test_packaged_state_storage_excludes_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "storage_mode": "s3", "storage_s3_bucket": "b",
        "storage_s3_secret_key": "SUPERSECRETVALUE", "feature_flags": {}}))
    from celerp.gateway.state import get_packaged_db_state
    st = get_packaged_db_state()
    assert st["has_external_storage"] is True
    assert "SUPERSECRETVALUE" not in json.dumps(st)


def test_infra_visible_storage_only_after_grace(tmp_path, monkeypatch):
    from celerp.gateway.state import set_feature_flags
    set_feature_flags({})
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "local", "external_db_url": "",
        "storage_mode": "s3", "storage_s3_bucket": "b",
        "feature_flags": {"external_storage": False, "grace_period_ends": None},
    }))
    assert sc._has_team_features({}) is True


def test_grace_notice_storage_only_in_grace(tmp_path, monkeypatch):
    state = {
        "in_grace": False, "storage_in_grace": True,
        "has_external_url": False, "external_db_entitled": True,
        "grace_period_ends": _future(),
        "has_external_storage": True, "external_storage_entitled": False,
    }
    assert sc._grace_notice(state, None, lang="en") is not None


def test_grace_notice_storage_only_after_grace(tmp_path, monkeypatch):
    state = {
        "in_grace": False, "storage_in_grace": False,
        "has_external_url": False, "external_db_entitled": True,
        "has_external_storage": True, "external_storage_entitled": False,
    }
    assert sc._grace_notice(state, None, lang="en") is not None


# ── S3 signed connectivity test (DECISION 3) ─────────────────────────────────

async def test_s3_test_success_head_bucket(monkeypatch):
    s3 = MagicMock()
    s3.head_bucket = AsyncMock(return_value={})
    monkeypatch.setattr(attachments, "_s3_client",
                        lambda *a, **k: _ACM(s3), raising=False)
    result = await sc._try_s3_connect("http://127.0.0.1:1", "b", "AK", "SK")
    assert result == t("settings_cloud.connected_to_bucket", bucket="b")
    s3.head_bucket.assert_awaited_once_with(Bucket="b")


async def test_s3_test_access_denied(monkeypatch):
    exc = _inject_botocore(monkeypatch)
    s3 = MagicMock()
    s3.head_bucket = AsyncMock(side_effect=exc.ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}))
    monkeypatch.setattr(attachments, "_s3_client",
                        lambda *a, **k: _ACM(s3), raising=False)
    with pytest.raises(RuntimeError) as ei:
        await sc._try_s3_connect("http://127.0.0.1:1", "b", "AK", "SK")
    assert str(ei.value) == t("settings_cloud.invalid_credentials_403")
    s3.head_bucket.assert_awaited_once()


async def test_s3_test_bucket_not_found(monkeypatch):
    exc = _inject_botocore(monkeypatch)
    s3 = MagicMock()
    s3.head_bucket = AsyncMock(side_effect=exc.ClientError(
        {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}}))
    monkeypatch.setattr(attachments, "_s3_client",
                        lambda *a, **k: _ACM(s3), raising=False)
    with pytest.raises(RuntimeError) as ei:
        await sc._try_s3_connect("http://127.0.0.1:1", "b", "AK", "SK")
    assert str(ei.value) == t("settings_cloud.bucket_not_found_404")
    s3.head_bucket.assert_awaited_once()


async def test_s3_test_unreachable(monkeypatch):
    exc = _inject_botocore(monkeypatch)
    s3 = MagicMock()
    s3.head_bucket = AsyncMock(
        side_effect=exc.EndpointConnectionError(endpoint_url="http://127.0.0.1:1"))
    monkeypatch.setattr(attachments, "_s3_client",
                        lambda *a, **k: _ACM(s3), raising=False)
    with pytest.raises(RuntimeError) as ei:
        await sc._try_s3_connect("http://127.0.0.1:1", "b", "AK", "SK")
    assert str(ei.value) == t("settings_cloud.cannot_reach_endpoint")
    s3.head_bucket.assert_awaited_once()


async def test_s3_test_degrades_without_aiobotocore(monkeypatch):
    def _raise_import(*a, **k):
        raise ImportError("No module named 'aiobotocore'")
    monkeypatch.setattr(attachments, "_s3_client", _raise_import, raising=False)
    with pytest.raises(RuntimeError) as ei:
        await sc._try_s3_connect("http://127.0.0.1:1", "b", "AK", "SK")
    assert str(ei.value) == t("settings_cloud.s3_support_unavailable")


async def test_s3_client_helper_shared(monkeypatch):
    assert hasattr(attachments, "_s3_client"), "shared _s3_client helper missing"
    s3 = MagicMock()
    s3.put_object = AsyncMock(return_value={})
    s3.head_bucket = AsyncMock(return_value={})
    _inject_aiobotocore(monkeypatch, s3)
    real = attachments._s3_client
    seen = {"n": 0}

    def spy(*a, **k):
        seen["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(attachments, "_s3_client", spy)
    backend = attachments.S3Backend("http://127.0.0.1:1", "bkt", "AK", "SK")
    await backend.store("co", "att", b"x", "image/png")
    _inject_botocore(monkeypatch)
    await sc._try_s3_connect("http://127.0.0.1:1", "bkt", "AK", "SK")
    assert seen["n"] >= 2


async def test_s3_test_unenumerated_exception_redacted(monkeypatch):
    _inject_botocore(monkeypatch)
    s3 = MagicMock()
    s3.head_bucket = AsyncMock(
        side_effect=ValueError("boto AKIAEXPOSED endpoint=http://secret"))
    monkeypatch.setattr(attachments, "_s3_client",
                        lambda *a, **k: _ACM(s3), raising=False)
    with pytest.raises(RuntimeError) as ei:
        await sc._try_s3_connect("http://127.0.0.1:1", "bkt", "AKIAEXPOSED", "SECRET")
    msg = str(ei.value)
    assert msg == t("settings_cloud.s3_connection_failed")
    assert "AKIAEXPOSED" not in msg
    assert "ValueError" not in msg


# ── app-local defect: duplicate DOM id (DECISION 3) ──────────────────────────

def test_infra_db_section_single_test_result_id(tmp_path, monkeypatch):
    _sandbox_toml(
        monkeypatch, tmp_path,
        '[database]\nurl = "postgresql+asyncpg://celerp:x@localhost:5432/celerp"\n'
        '[database_backup]\nprevious_url = "postgresql+asyncpg://celerp:old@old:5432/celerp"\n')
    html = to_xml(sc._infra_db_section())
    assert html.count('id="db-test-result"') == 1
    assert html.count('id="restore-db-result"') == 1


# ── manage_integrations denial gates every infra handler ─────────────────────
#
# _check_permission returns a truthy RedirectResponse when the caller lacks
# the permission and None when it holds it (ui/routes/settings.py:29-46). All
# four infra handlers check `if await _check_permission(...): return Div()`
# before touching the form, the packaged config.json, or subprocess.Popen -
# this is a pre-existing gate the change does not modify. These tests force
# a denial and assert the handler stops there: an empty Div (no leaked
# unauthorized detail, matching what the redirect path already renders) and
# no write or process side effect, proving the gate runs before any of them.

@pytest.mark.parametrize("path,form", [
    ("/settings/cloud/test-db", {
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    }),
    ("/settings/cloud/test-storage", {
        "storage_backend": "s3", "s3_endpoint": "https://s3.x",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    }),
    ("/settings/cloud/save-infra", {
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    }),
    ("/settings/cloud/restore-db", {}),
])
async def test_infra_handler_rejects_without_manage_integrations(
        client, tmp_path, monkeypatch, path, form):
    import subprocess
    from starlette.responses import RedirectResponse

    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    merge_key = MagicMock(return_value=True)
    monkeypatch.setattr(sc, "merge_packaged_config", merge_key)

    # Deny: override the client fixture's grant with a truthy redirect, the
    # same shape _check_permission itself returns on denial.
    monkeypatch.setattr(sc, "_check_permission",
                        AsyncMock(return_value=RedirectResponse("/dashboard", status_code=302)),
                        raising=False)

    # Packaged branch would run (and write config.json / relaunch) if the
    # gate did not stop the request first - CELERP_DATA_DIR is set so a
    # missing side effect proves the gate, not an untaken code path.
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external", "external_db_url": "OLD",
        "external_db_url_backup": "OLDER", "storage_mode": "local",
    }))

    r = await client.post(path, data=form, headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert r.text.strip() == "<div></div>"
    assert t("error.unauthorized") not in r.text
    merge_key.assert_not_called()
    popen.assert_not_called()
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["external_db_url"] == "OLD"
    assert cfg["external_db_url_backup"] == "OLDER"
