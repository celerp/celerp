# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""WS5 G3: server-side ACTIVE Team entitlement at the infra write endpoints.

The manage_integrations RBAC gate is pure role checking; it does not confirm the
install still holds an ACTIVE external-infra entitlement. A lapsed-but-configured
install (external infra on disk, but the current feature flags no longer grant
it) must be rejected server-side at save-infra, test-db, and test-storage, so a
direct POST cannot establish or re-probe external infra without a live
entitlement. cloud/restore-db keeps the lenient gate: it is the undo/recovery
path a lapsed admin must still reach.

The active-entitlement predicate is _active_team_entitlement(state): the ACTIVE
feature-flag clause (external_db or external_storage), never the lenient
_has_team_features which also passes lapsed-but-configured installs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fasthtml.common import FastHTML

import ui.routes.settings_cloud as sc
from ui.i18n import t


# A lapsed-but-configured caller: external infra is on disk, but the live flags
# no longer grant it. _has_team_features passes this (undo path), so it is the
# exact caller the ACTIVE guard must reject at save/test.
_LAPSED_STATE = {"feature_flags": {"external_db": False, "external_storage": False}}
_ACTIVE_DB_STATE = {"feature_flags": {"external_db": True, "external_storage": False}}
_ACTIVE_STORAGE_STATE = {"feature_flags": {"external_db": False, "external_storage": True}}


@pytest_asyncio.fixture
async def client(monkeypatch):
    """Settings-cloud routes with manage_integrations GRANTED and a token
    present, so the request reaches the entitlement guard rather than stopping
    at the RBAC gate."""
    monkeypatch.setattr(sc, "_check_permission", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(sc, "_token", lambda req: "tok", raising=False)
    app = FastHTML()
    sc.setup_routes(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t",
                           follow_redirects=False) as c:
        yield c


def _unit_state():
    return {"feature_flags": {"external_db": True, "external_storage": False}}


def test_active_team_entitlement_true_when_flag_set():
    """The ACTIVE predicate grants when either external flag is live."""
    assert sc._active_team_entitlement(_ACTIVE_DB_STATE) is True
    assert sc._active_team_entitlement(_ACTIVE_STORAGE_STATE) is True


def test_active_team_entitlement_false_when_lapsed():
    """A lapsed-but-configured install (no live flags) is not ACTIVE-entitled,
    even though the lenient _has_team_features would still pass it."""
    assert sc._active_team_entitlement(_LAPSED_STATE) is False
    assert sc._active_team_entitlement({}) is False


async def test_infra_save_requires_team_entitlement(client, tmp_path, monkeypatch):
    """A lapsed-but-configured caller with manage_integrations is rejected
    server-side at save-infra: no config write, no relaunch."""
    import subprocess
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    merge = MagicMock(return_value=True)
    monkeypatch.setattr(sc, "merge_packaged_config", merge)
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(return_value=_LAPSED_STATE), raising=False)

    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external", "external_db_url": "OLD",
        "external_db_url_backup": "OLDER", "storage_mode": "local",
    }))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    }, headers={"HX-Request": "true"})

    assert r.status_code == 200
    merge.assert_not_called()
    popen.assert_not_called()
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["external_db_url"] == "OLD", "save proceeded despite a lapsed entitlement"


async def test_infra_save_allowed_with_active_entitlement(client, tmp_path, monkeypatch):
    """An ACTIVE-entitled caller with manage_integrations saves normally."""
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(return_value=_ACTIVE_DB_STATE), raising=False)
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(
        json.dumps({"db_mode": "local", "external_db_url": ""}))

    r = await client.post("/settings/cloud/save-infra", data={
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    }, headers={"HX-Request": "true"})

    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    assert cfg["db_mode"] == "external", "an active-entitled save did not persist"


async def test_infra_test_db_requires_team_entitlement(client, monkeypatch):
    """test-db is rejected for a lapsed-but-configured caller before it probes."""
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(return_value=_LAPSED_STATE), raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(sc, "_try_db_connect", connect, raising=False)
    r = await client.post("/settings/cloud/test-db", data={
        "db_host": "db.example.com", "db_port": "5432",
        "db_name": "celerp", "db_user": "celerp", "db_pass": "pw",
    }, headers={"HX-Request": "true"})
    assert r.status_code == 200
    connect.assert_not_called()


async def test_infra_test_storage_requires_team_entitlement(client, monkeypatch):
    """test-storage is rejected for a lapsed-but-configured caller before it
    probes S3."""
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(return_value=_LAPSED_STATE), raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(sc, "_try_s3_connect", connect, raising=False)
    r = await client.post("/settings/cloud/test-storage", data={
        "storage_backend": "s3", "s3_endpoint": "https://s3.example.com",
        "s3_bucket": "bkt", "s3_access_key": "AK", "s3_secret_key": "SK",
    }, headers={"HX-Request": "true"})
    assert r.status_code == 200
    connect.assert_not_called()


async def test_infra_restore_db_keeps_lenient_gate(client, tmp_path, monkeypatch):
    """restore-db is the undo path: a lapsed-but-configured admin must still be
    able to restore a backup, so it keeps the lenient gate and does NOT require
    ACTIVE entitlement."""
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    # A lapsed state must NOT block restore.
    monkeypatch.setattr(sc, "_commercial_state", AsyncMock(return_value=_LAPSED_STATE), raising=False)
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    (tmp_path / "celerp-config.json").write_text(json.dumps({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://u:p@current:5432/db",
        "external_db_url_backup": "postgresql+asyncpg://u:p@backup:5432/db",
    }))

    r = await client.post("/settings/cloud/restore-db", data={},
                          headers={"HX-Request": "true"})
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "celerp-config.json").read_text())
    # The restore swapped current <-> backup, so the lapsed caller was allowed.
    assert cfg["external_db_url"] == "postgresql+asyncpg://u:p@backup:5432/db", \
        "restore was blocked for a lapsed admin (undo path must stay open)"
