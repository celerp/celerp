# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Integration tests: full bootstrap → login flow and `init --force` correctness.

Covers:
  - bootstrap_status returns false on empty DB
  - POST /auth/register creates admin, returns tokens, bootstrap_status flips to true
  - Second register is blocked (403) once bootstrapped
  - Login with correct credentials succeeds (200)
  - Login with wrong password fails (401)
  - Truncating tables resets bootstrap_status to false
  - `init --force` CLI: stops servers, regenerates jwt_secret, drops+recreates DB
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from celerp.cli import _config_path, _read_config, _write_config, main


# ── API-level bootstrap flow ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bootstrap_status_false_on_empty_db(client):
    r = await client.get("/auth/bootstrap-status")
    assert r.status_code == 200
    assert r.json() == {"bootstrapped": False}


@pytest.mark.asyncio
async def test_register_bootstraps_system(client):
    r = await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "admin@test.com", "name": "Admin", "password": "password123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["access_token"]
    assert data["refresh_token"]

    status = await client.get("/auth/bootstrap-status")
    assert status.json() == {"bootstrapped": True}


@pytest.mark.asyncio
async def test_register_locked_after_bootstrap(client):
    """Second registration attempt must be rejected once any user exists."""
    await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "admin@test.com", "name": "Admin", "password": "password123"},
    )
    r = await client.post(
        "/auth/register",
        json={"company_name": "Attacker", "email": "evil@test.com", "name": "Evil", "password": "haxorpw1"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_login_with_correct_credentials(client):
    await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "admin@test.com", "name": "Admin", "password": "password123"},
    )
    r = await client.post("/auth/login", json={"email": "admin@test.com", "password": "password123"})
    assert r.status_code == 200
    data = r.json()
    assert data["access_token"]
    assert data["refresh_token"]


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client):
    await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "admin@test.com", "name": "Admin", "password": "password123"},
    )
    r = await client.post("/auth/login", json={"email": "admin@test.com", "password": "wrongpassword"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_unknown_email(client):
    r = await client.post("/auth/login", json={"email": "nobody@test.com", "password": "anything"})
    assert r.status_code == 401


# ── Table wipe resets bootstrap ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_truncate_resets_bootstrap(client, session):
    """Wiping all rows must reset bootstrap_status to false."""
    # Bootstrap the system
    r = await client.post(
        "/auth/register",
        json={"company_name": "TestCo", "email": "admin@test.com", "name": "Admin", "password": "password123"},
    )
    assert r.status_code == 200

    status_before = await client.get("/auth/bootstrap-status")
    assert status_before.json()["bootstrapped"] is True

    # Truncate via _truncate_all_tables (SQLite in-memory: use session directly)
    # For the SQLite test DB we truncate by deleting all rows and resetting stamps.
    await session.execute(text("DELETE FROM users"))
    await session.execute(text("DELETE FROM companies"))
    await session.commit()

    status_after = await client.get("/auth/bootstrap-status")
    assert status_after.json()["bootstrapped"] is False


# ── CLI: init --force ─────────────────────────────────────────────────────────

_PATCHES = dict(
    test_db="celerp.cli._test_db",
    run_migrations="celerp.cli._run_migrations",
    post_grants="celerp.cli._post_migration_grants",
    start="celerp.cli._start",
    stop="celerp.cli._stop_servers",
)


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    config_file = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(config_file))
    return config_file


@pytest.fixture()
def written_cfg(tmp_config):
    cfg = {
        "database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"},
        "auth": {"jwt_secret": "old" * 21 + "x"},  # 64 chars
        "server": {"api_port": 8000, "ui_port": 8080},
        "cloud": {"token": ""},
    }
    _write_config(cfg)
    return cfg


def test_force_init_as_root_wipes_db_and_regenerates_secret(tmp_config, written_cfg):
    """sudo celerp init --force must: stop servers, drop+recreate DB, regenerate secret."""
    old_secret = written_cfg["auth"]["jwt_secret"]
    runner = CliRunner()
    with patch(_PATCHES["stop"]) as mock_stop, \
         patch("celerp.cli._provision_db") as mock_prov, \
         patch(_PATCHES["run_migrations"]), \
         patch(_PATCHES["post_grants"]), \
         patch("celerp.cli._needs_ownership_fix", return_value=False), \
         patch(_PATCHES["start"]):
        result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code == 0, result.output
    mock_stop.assert_called_once()
    # Must be called with drop_existing=True
    mock_prov.assert_called_once()
    _, kwargs = mock_prov.call_args
    assert kwargs.get("drop_existing") is True or mock_prov.call_args[0][1] is True

    new_cfg = _read_config()
    assert new_cfg["auth"]["jwt_secret"] != old_secret, (
        "--force must regenerate jwt_secret so all old sessions are invalidated"
    )


def test_force_init_non_root_also_drops_db(tmp_config, written_cfg):
    """celerp init --force without sudo must also drop+recreate via _provision_db."""
    old_secret = written_cfg["auth"]["jwt_secret"]
    runner = CliRunner()
    with patch(_PATCHES["stop"]), \
         patch("os.getuid", return_value=1000), \
         patch("celerp.cli._provision_db") as mock_prov, \
         patch(_PATCHES["run_migrations"]), \
         patch(_PATCHES["post_grants"]), \
         patch("celerp.cli._needs_ownership_fix", return_value=False), \
         patch(_PATCHES["start"]):
        result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code == 0, result.output
    mock_prov.assert_called_once()
    _, kwargs = mock_prov.call_args
    assert kwargs.get("drop_existing") is True or mock_prov.call_args[0][1] is True

    new_cfg = _read_config()
    assert new_cfg["auth"]["jwt_secret"] != old_secret


def test_force_init_provision_failure_exits(tmp_config, written_cfg):
    """If _provision_db raises, init --force must exit non-zero."""
    runner = CliRunner()
    with patch(_PATCHES["stop"]), \
         patch("celerp.cli._provision_db", side_effect=RuntimeError("pg down")):
        result = runner.invoke(main, ["init", "--force"])

    assert result.exit_code != 0
    assert "DB wipe failed" in result.output
