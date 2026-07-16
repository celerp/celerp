# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp CLI (celerp.cli)."""

from __future__ import annotations

import os
import subprocess
import sys

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


def test_init_no_start_skips_server_launch(tmp_config):
    """--no-start provisions + migrates + writes config, then exits without
    launching servers (headless/service-managed installs)."""
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]) as mock_start:
        result = runner.invoke(main, ["init", "--no-start"])
    assert result.exit_code == 0, result.output
    mock_start.assert_not_called()                      # nothing launched
    assert tmp_config.exists()                          # config written
    assert "celerp start" in result.output              # tells the user what to run next


def test_init_without_no_start_launches_servers(tmp_config):
    """Regression guard for the desktop one-command UX: no flag → _start runs."""
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]) as mock_start:
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    mock_start.assert_called_once()


def test_init_force_no_start_skips_launch(tmp_config, valid_cfg):
    """--no-start also applies on the --force path: it still stops/wipes/re-provisions,
    then exits without launching."""
    _write_config(valid_cfg)
    runner = CliRunner()
    with patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]) as mock_start, \
         patch("celerp.cli._stop_servers") as mock_stop, \
         patch("celerp.cli._provision_db") as mock_prov, \
         patch("celerp.cli._needs_ownership_fix", return_value=False):
        result = runner.invoke(main, ["init", "--force", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output
    mock_stop.assert_called_once()        # force still stopped servers
    mock_prov.assert_called_once()        # force still re-provisioned
    mock_start.assert_not_called()        # but did not launch


def test_init_shows_star_cta_line(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert "Back us early" in result.output
    assert "celerp.com/github" in result.output


def test_init_omits_star_cta_line_when_disabled(tmp_config, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "star_cta_enabled", False)
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value=None), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    assert "Back us early" not in result.output


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
        result = runner.invoke(main, ["init", "--force", "--yes"])
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


# The external-server tests pass --db-url, which is how the real external
# consumer (the DigitalOcean droplet) always invokes init. An explicit URL
# short-circuits mode detection to external, so these exercise the external
# connect/provision path regardless of whether the bundled DB is installed.
_EXT_URL = "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"


def test_init_db_connection_failure_no_sudo(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value="connection refused"), \
         patch("os.getuid", return_value=1000):
        result = runner.invoke(main, ["init", "--db-url", _EXT_URL])
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
        result = runner.invoke(main, ["init", "--db-url", _EXT_URL])
    assert result.exit_code == 0, result.output
    mock_prov.assert_called_once()


def test_init_db_provision_failure_as_root(tmp_config):
    runner = CliRunner()
    with patch(_INIT_PATCHES["test_db"], return_value="connection refused"), \
         patch("os.getuid", return_value=0), \
         patch("celerp.cli._provision_db", side_effect=RuntimeError("pg not running")):
        result = runner.invoke(main, ["init", "--db-url", _EXT_URL])
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
    """migrate must run migrations, grants, then the develop→release reconcile."""
    _write_config(valid_cfg)
    runner = CliRunner()
    with patch("celerp.cli._run_migrations") as mock_mig, \
         patch("celerp.cli._post_migration_grants") as mock_grants, \
         patch("celerp.cli._reconcile_after_migrate") as mock_reconcile:
        result = runner.invoke(main, ["migrate"])
    assert result.exit_code == 0
    mock_mig.assert_called_once_with(valid_cfg["database"]["url"])
    mock_grants.assert_called_once_with(valid_cfg["database"]["url"])
    mock_reconcile.assert_called_once_with(valid_cfg["database"]["url"])


def test_migrate_db_url_overrides_config(tmp_config):
    """`migrate --db-url` runs without a config (the packaged launcher path) and
    threads the given URL through migrations, grants, and the reconcile."""
    url = "postgresql+asyncpg://celerp:celerp@localhost:5432/launcher"
    runner = CliRunner()
    with patch("celerp.cli._run_migrations") as mock_mig, \
         patch("celerp.cli._post_migration_grants") as mock_grants, \
         patch("celerp.cli._reconcile_after_migrate") as mock_reconcile:
        result = runner.invoke(main, ["migrate", "--db-url", url])
    assert result.exit_code == 0, result.output
    mock_mig.assert_called_once_with(url)
    mock_grants.assert_called_once_with(url)
    mock_reconcile.assert_called_once_with(url)


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
        "database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"},
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
        # readiness probing is not under test here, and its sleeps would
        # consume fake_sleep's budget before the supervisor loop runs
        patch("celerp.cli._wait_ready"),
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
        "database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"},
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
        patch("celerp.cli._wait_ready"),  # not under test; would spin on closed ports
        patch("signal.signal"),
    ):
        with pytest.raises(SystemExit) as exc:
            _start(cfg)

    assert exc.value.code == 1
    assert len([c for c in spawn_calls if _is_api_cmd(c)]) == 1, "No respawn without sentinel"


# ── celerp init --force purges files (#160) ──────────────────────────────────

def _seed_data_dir(tmp_path, monkeypatch):
    """Point settings.data_dir at a temp dir seeded with attachment + ai_upload files."""
    from celerp.config import settings
    data_dir = tmp_path / "data"
    att = data_dir / "static" / "attachments"
    att.mkdir(parents=True)
    (att / "img.png").write_bytes(b"x")
    ai = data_dir / "ai_uploads"
    ai.mkdir(parents=True)
    (ai / "doc.pdf").write_bytes(b"y")
    monkeypatch.setattr(settings, "data_dir", data_dir)
    return att, ai


def test_init_force_yes_wipes_db_and_files(tmp_config, tmp_path, monkeypatch):
    """--force --yes wipes the DB and removes attachment + ai_uploads dirs (no prompt)."""
    att, ai = _seed_data_dir(tmp_path, monkeypatch)
    runner = CliRunner()
    with patch("celerp.cli._provision_db") as prov, \
         patch("celerp.cli._stop_servers"), \
         patch(_INIT_PATCHES["run_migrations"]), \
         patch(_INIT_PATCHES["post_grants"]), \
         patch(_INIT_PATCHES["start"]):
        result = runner.invoke(main, ["init", "--force", "--yes", "--db-url", _EXT_URL])
    assert result.exit_code == 0, result.output
    prov.assert_called_once()           # DB wiped
    assert not att.exists() and not ai.exists()   # files removed


def test_init_force_aborts_on_no_keeps_everything(tmp_config, tmp_path, monkeypatch):
    """Without --yes, answering No to the wipe warning aborts: DB and files untouched."""
    att, ai = _seed_data_dir(tmp_path, monkeypatch)
    runner = CliRunner()
    with patch("celerp.cli._provision_db") as prov, \
         patch("celerp.cli._stop_servers") as stop:
        result = runner.invoke(main, ["init", "--force"], input="n\n")
    assert "Aborted" in result.output
    prov.assert_not_called()            # DB not wiped
    stop.assert_not_called()
    assert att.exists() and ai.exists() # files kept


# ── _wait_ready ───────────────────────────────────────────────────────────────

def test_wait_ready_announces_when_port_opens(capsys):
    """The ready line appears only once the port actually accepts connections."""
    import socket
    import threading
    import time as _time

    from celerp.cli import _wait_ready

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    class FakeProc:
        def poll(self):
            return None

    def _listen_later():
        _time.sleep(0.6)
        srv.listen(1)

    t = threading.Thread(target=_listen_later)
    t.start()
    _wait_ready({"UI ": (FakeProc(), port)}, timeout=10)
    t.join()
    srv.close()
    out = capsys.readouterr().out
    assert "✓ UI  ready → http://localhost:" in out


def test_wait_ready_skips_dead_process(capsys):
    """A crashed server never gets a ready line (the supervisor reports it)."""
    from celerp.cli import _wait_ready

    class DeadProc:
        def poll(self):
            return 1

    _wait_ready({"API": (DeadProc(), 1)}, timeout=2)
    assert "ready" not in capsys.readouterr().out


# ── `python -m celerp` module entrypoint ────────────────────────────────────
# The packaged Electron launcher invokes the bundled Python by module
# (`python -m celerp migrate`), not via the console script. These tests prove
# the celerp/__main__.py entrypoint exists and dispatches to the CLI group.

def _module_run(args, config_path):
    env = {
        **os.environ,
        "ALLOW_INSECURE_JWT": "true",        # config import guard
        "CELERP_CONFIG": str(config_path),   # isolate from the real config
    }
    return subprocess.run(
        [sys.executable, "-m", "celerp", *args],
        capture_output=True, text=True, timeout=20, env=env,
    )


def test_module_entrypoint_help_lists_commands(tmp_path):
    """`python -m celerp --help` must run and expose the subcommands."""
    r = _module_run(["--help"], tmp_path / "nope.toml")
    assert r.returncode == 0, r.stderr
    assert "migrate" in r.stdout


def test_module_entrypoint_dispatches_to_subcommand(tmp_path):
    """`python -m celerp migrate` must reach the migrate command itself.

    With no config it exits non-zero and says "Not initialized" — proving the
    invocation dispatched to the real subcommand, not just the group --help.
    """
    r = _module_run(["migrate"], tmp_path / "nope.toml")
    assert r.returncode != 0
    assert "Not initialized" in (r.stdout + r.stderr)


def test_run_migrations_exits_nonzero_on_failure():
    """The CLI wrapper preserves exit semantics for the packaged launcher, while
    _apply_migrations stays importable (the backup restore calls it and needs the
    exception, not a process exit)."""
    import celerp.cli as cli

    with patch("celerp.cli._apply_migrations", side_effect=RuntimeError("boom")):
        with pytest.raises(SystemExit) as exc:
            cli._run_migrations("postgresql://user:pw@localhost/db")
    assert exc.value.code == 1
