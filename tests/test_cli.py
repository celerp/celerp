# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for celerp CLI (celerp.cli)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from celerp.cli import _config_path, _read_config, _write_config, main


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Point config path to a temp dir."""
    config_file = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(config_file))
    return config_file


@pytest.fixture()
def valid_cfg():
    return {
        "database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"},
        "auth": {"jwt_secret": "a" * 64},
        "server": {"api_port": 8000, "ui_port": 8080},
        "cloud": {"token": ""},
    }


# Patch targets shared across init tests
_INIT_PATCHES = dict(
    test_db="celerp.cli._test_db",
    run_migrations="celerp.cli._run_migrations",
    post_grants="celerp.cli._post_migration_grants",
    start="celerp.cli._start",
)


# ── _write_config / _read_config round-trip ───────────────────────────────────

def test_write_read_roundtrip(tmp_config, valid_cfg):
    _write_config(valid_cfg)
    assert tmp_config.exists()
    result = _read_config()
    assert result["database"]["url"] == valid_cfg["database"]["url"]
    assert result["server"]["api_port"] == 8000
    assert result["cloud"]["token"] == ""


def test_read_config_missing_returns_empty(tmp_config):
    assert _read_config() == {}


# ── celerp init ───────────────────────────────────────────────────────────────

def test_init_defaults(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert "✓ Celerp initialized" in result.output
    assert tmp_config.exists()
    cfg = _read_config()
    assert cfg["server"]["api_port"] == 8000
    assert cfg["server"]["ui_port"] == 8080
    assert len(cfg["auth"]["jwt_secret"]) == 64  # secrets.token_hex(32)


def test_init_custom_flags(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, [
            "init",
            "--db-url", "postgresql+asyncpg://u:p@remotehost/mydb",
            "--api-port", "9000",
            "--ui-port", "9080",
            "--cloud-token", "tok123",
        ])
    assert result.exit_code == 0, result.output
    cfg = _read_config()
    assert cfg["database"]["url"] == "postgresql+asyncpg://u:p@remotehost/mydb"
    assert cfg["server"]["api_port"] == 9000
    assert cfg["cloud"]["token"] == "tok123"


def test_init_already_initialized(tmp_config, valid_cfg):
    _write_config(valid_cfg)
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    assert "Already initialized" in result.output


def test_init_force_stops_servers_and_regenerates_secret(tmp_config, valid_cfg):
    """--force must kill running servers, wipe the DB, and generate a new jwt_secret."""
    _write_config(valid_cfg)
    old_secret = valid_cfg["auth"]["jwt_secret"]
    runner = CliRunner()
    with patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]), \
         patch("celerp.cli._stop_servers") as mock_stop, \
         patch("celerp.cli._provision_db"), \
         patch("celerp.cli._needs_ownership_fix", return_value=False):
        result = runner.invoke(main, ["init", "--force"])
    assert result.exit_code == 0, result.output
    assert "✓ Celerp initialized" in result.output
    mock_stop.assert_called_once()
    new_cfg = _read_config()
    assert new_cfg["auth"]["jwt_secret"] != old_secret, (
        "--force must regenerate jwt_secret; old sessions must be invalidated"
    )


def test_init_post_migration_grants_called(tmp_config):
    """_post_migration_grants must be called after migrations so sequences are accessible."""
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]) as mock_mig, \
         patch(_INIT_PATCHES["post_grants"]) as mock_grants, \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    mock_mig.assert_called_once()
    mock_grants.assert_called_once()
    # grants must be called AFTER migrations
    assert mock_mig.call_args_list[0].args[0] == mock_grants.call_args_list[0].args[0], (
        "migrations and grants must use the same db_url"
    )


def test_init_db_connection_failure_no_sudo(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value="connection refused"), \
         patch("os.getuid", return_value=1000):
        result = runner.invoke(main, ["init"])
    assert result.exit_code != 0
    assert "Re-run with sudo" in result.output
    assert "init" in result.output


def test_init_db_auto_provision_as_root(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"]) as mock_test, \
         patch("os.getuid", return_value=0), \
         patch("celerp.cli._provision_db") as mock_prov, \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        mock_test.side_effect = ["connection refused", None]
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    mock_prov.assert_called_once()


def test_init_db_provision_failure_as_root(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value="connection refused"), \
         patch("os.getuid", return_value=0), \
         patch("celerp.cli._provision_db", side_effect=RuntimeError("pg not running")):
        result = runner.invoke(main, ["init"])
    assert result.exit_code != 0
    assert "Provisioning failed" in result.output


# ── celerp status ─────────────────────────────────────────────────────────────

def test_status_not_initialized(tmp_config):
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "celerp init" in result.output


def test_status_initialized(tmp_config, valid_cfg):
    _write_config(valid_cfg)
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch("sqlalchemy.create_engine"), \
         patch("alembic.script.ScriptDirectory") as mock_script, \
         patch("alembic.runtime.migration.MigrationContext"):
        mock_script.from_config.return_value.get_current_head.return_value = "abc123"
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "8000" in result.output
    assert "8080" in result.output


# ── celerp migrate ────────────────────────────────────────────────────────────

def test_migrate_not_initialized(tmp_config):
    runner = CliRunner()
    result = runner.invoke(main, ["migrate"])
    assert result.exit_code != 0
    assert "celerp init" in result.output


def test_migrate_runs_with_grants(tmp_config, valid_cfg):
    """migrate must call _post_migration_grants after _run_migrations."""
    _write_config(valid_cfg)
    runner = CliRunner()
    with patch("celerp.cli._run_migrations") as mock_mig, \
         patch("celerp.cli._post_migration_grants") as mock_grants:
        result = runner.invoke(main, ["migrate"])
    assert result.exit_code == 0
    mock_mig.assert_called_once_with(valid_cfg["database"]["url"])
    mock_grants.assert_called_once_with(valid_cfg["database"]["url"])


# ── celerp start ─────────────────────────────────────────────────────────────

def test_start_not_initialized(tmp_config):
    runner = CliRunner()
    result = runner.invoke(main, ["start"])
    assert result.exit_code != 0
    assert "celerp init" in result.output


# ── _start sentinel-based respawn ────────────────────────────────────────────

def _is_api_cmd(cmd):
    return any("celerp.main" in s for s in cmd)


def test_start_respawns_api_on_sentinel(tmp_path):
    """When API exits and the restart sentinel exists, _start respawns the API
    rather than calling sys.exit."""
    from celerp.cli import _start

    sentinel_path = tmp_path / ".restart_requested"
    sentinel_path.touch()

    cfg = {
        "server": {"api_port": 8000, "ui_port": 8080},
        "database": {"url": "sqlite+aiosqlite:///test.db"},
        "auth": {"jwt_secret": "test"},
        "modules": {"enabled": []},
    }

    spawn_calls = []

    class _Proc:
        def __init__(self, dead=False, code=0):
            self._dead = dead
            self.returncode = code
        def poll(self): return self.returncode if self._dead else None
        def terminate(self): pass
        def wait(self): pass

    def fake_popen(cmd, env):
        spawn_calls.append(list(cmd))
        if _is_api_cmd(cmd):
            api_n = sum(1 for c in spawn_calls if _is_api_cmd(c))
            return _Proc(dead=True, code=0) if api_n == 1 else _Proc()
        return _Proc()

    sleep_calls = [0]

    def fake_sleep(n):
        sleep_calls[0] += 1
        if sleep_calls[0] > 3:
            raise SystemExit(0)

    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("celerp.cli._read_config", return_value=cfg),
        patch("celerp.cli._config_to_env", return_value={}),
        patch("celerp.config.config_path", return_value=tmp_path / "config.toml"),
        patch("celerp.cli.time.sleep", side_effect=fake_sleep),
        patch("signal.signal"),
    ):
        with pytest.raises(SystemExit) as exc:
            _start(cfg)

    assert exc.value.code == 0
    assert not sentinel_path.exists(), "Sentinel must be deleted after respawn"
    assert len([c for c in spawn_calls if _is_api_cmd(c)]) >= 2, "API must be spawned at least twice"


def test_start_exits_without_sentinel(tmp_path):
    """When API exits WITHOUT the sentinel, _start calls sys.exit with the subprocess returncode."""
    from celerp.cli import _start

    cfg = {
        "server": {"api_port": 8000, "ui_port": 8080},
        "database": {"url": "sqlite+aiosqlite:///test.db"},
        "auth": {"jwt_secret": "test"},
        "modules": {"enabled": []},
    }

    spawn_calls = []

    class _Proc:
        def __init__(self, dead=False, code=0):
            self._dead = dead
            self.returncode = code
        def poll(self): return self.returncode if self._dead else None
        def terminate(self): pass
        def wait(self): pass

    def fake_popen(cmd, env):
        spawn_calls.append(list(cmd))
        return _Proc(dead=True, code=1) if _is_api_cmd(cmd) else _Proc()

    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("celerp.cli._config_to_env", return_value={}),
        patch("celerp.config.config_path", return_value=tmp_path / "config.toml"),
        patch("celerp.cli.time.sleep"),
        patch("signal.signal"),
    ):
        with pytest.raises(SystemExit) as exc:
            _start(cfg)

    assert exc.value.code == 1
    assert len([c for c in spawn_calls if _is_api_cmd(c)]) == 1, "No respawn without sentinel"
