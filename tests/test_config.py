# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for celerp/config.py — config file read/write and boot helpers.

Critical coverage: first-boot scenarios where config.toml does not yet exist.
These guard against the infinite-restart bug (write_config writing api_port=0)
and the silent-drop bug (set_enabled_modules no-oping when file is missing).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_config_module():
    """Restore celerp.config after each test to undo any module-level mutations.

    Tests in this file call importlib.reload(celerp.config), which rebinds the
    module-level `settings` singleton to a NEW instance. Other test modules capture
    the ORIGINAL instance at import time via `from celerp.config import settings`
    (e.g. test_quota.py), and monkeypatch it; if celerp.config.settings is left
    pointing at a different instance, those patches no longer affect what the code
    reads (relay_http_url() reads celerp.config.settings live). So we restore the
    exact original instance, not merely a freshly-reloaded one.
    """
    import celerp.config as mod
    original_settings = mod.settings
    original_env = os.environ.get("CELERP_CONFIG")
    yield
    # Restore the env var and reload so module-level functions/paths are fresh.
    if original_env is None:
        os.environ.pop("CELERP_CONFIG", None)
    else:
        os.environ["CELERP_CONFIG"] = original_env
    importlib.reload(mod)
    # Restore the ORIGINAL settings instance so every module that captured it at
    # import time (and any monkeypatch on it) stays consistent with what the code
    # reads via celerp.config.settings. We must NOT reload celerp.main itself
    # because that recreates `app`, breaking conftest fixtures that captured it.
    mod.settings = original_settings
    import celerp.main as _main_mod
    _main_mod.settings = original_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_config(tmp_path: Path, monkeypatch):
    """Point CELERP_CONFIG at a fresh tmp dir and reload config module."""
    cfg_file = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg_file))
    import celerp.config as mod
    importlib.reload(mod)
    return mod, cfg_file


# ---------------------------------------------------------------------------
# write_config — partial section hygiene
# ---------------------------------------------------------------------------

class TestWriteConfigPartialSections:
    """write_config must only emit sections present in cfg, never zero-fill missing ones."""

    def test_modules_only_writes_no_server_section(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        content = cfg_file.read_text()
        assert "[modules]" in content
        assert "[server]" not in content
        assert "api_port" not in content

    def test_modules_only_writes_no_database_section(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        content = cfg_file.read_text()
        assert "[database]" not in content
        assert 'url = ""' not in content

    def test_modules_only_writes_no_auth_section(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        content = cfg_file.read_text()
        assert "[auth]" not in content
        assert 'jwt_secret = ""' not in content

    def test_modules_only_writes_no_cloud_section(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        content = cfg_file.read_text()
        assert "[cloud]" not in content

    def test_full_cfg_writes_all_sections(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "database": {"url": "postgresql+asyncpg://x:y@localhost/db"},
            "auth": {"jwt_secret": "s3cr3t"},
            "server": {"api_port": 8000, "ui_port": 8080},
            "cloud": {"token": "", "instance_id": "", "public_url": "", "backup_encryption_key": "", "tos_version": ""},
            "modules": {"enabled": ["inventory", "crm"]},
        })
        content = cfg_file.read_text()
        assert "[database]" in content
        assert "[auth]" in content
        assert "[server]" in content
        assert "api_port = 8000" in content
        assert "[modules]" in content

    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        nested = tmp_path / "deeply" / "nested" / "dir"
        cfg_file = nested / "config.toml"
        monkeypatch.setenv("CELERP_CONFIG", str(cfg_file))
        import celerp.config as mod
        importlib.reload(mod)
        mod.write_config({"modules": {"enabled": []}})
        assert cfg_file.exists()


# ---------------------------------------------------------------------------
# write_config / read_config round-trip
# ---------------------------------------------------------------------------

class TestWriteReadRoundTrip:
    """Data written by write_config must be faithfully recovered by read_config."""

    def test_modules_round_trip(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory", "crm"]}})
        cfg = mod.read_config()
        assert cfg["modules"]["enabled"] == ["inventory", "crm"]

    def test_server_ports_round_trip(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "server": {"api_port": 54321, "ui_port": 54322},
            "modules": {"enabled": []},
        })
        cfg = mod.read_config()
        assert cfg["server"]["api_port"] == 54321
        assert cfg["server"]["ui_port"] == 54322

    def test_jwt_secret_round_trip(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        secret = "supersecretvalue123"
        mod.write_config({
            "auth": {"jwt_secret": secret},
            "modules": {"enabled": []},
        })
        cfg = mod.read_config()
        assert cfg["auth"]["jwt_secret"] == secret

    def test_read_config_returns_empty_dict_when_file_missing(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        # Don't call write_config — file should not exist
        cfg = mod.read_config()
        assert cfg == {}


# ---------------------------------------------------------------------------
# set_enabled_modules — first-boot (no config.toml)
# ---------------------------------------------------------------------------

class TestSetEnabledModulesFirstBoot:
    """On first boot, config.toml does not exist. set_enabled_modules must write it."""

    def test_writes_config_file_when_missing(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        assert not cfg_file.exists()
        mod.set_enabled_modules(["inventory"])
        assert cfg_file.exists()

    def test_written_file_contains_module(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        assert "inventory" in cfg["modules"]["enabled"]

    def test_written_file_does_not_contain_zero_port(self, tmp_path, monkeypatch):
        """First-boot write must NOT produce api_port = 0 (the infinite-restart bug)."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory"])
        content = cfg_file.read_text()
        assert "api_port = 0" not in content
        assert "ui_port = 0" not in content

    def test_written_file_does_not_contain_empty_jwt_secret(self, tmp_path, monkeypatch):
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory"])
        content = cfg_file.read_text()
        assert 'jwt_secret = ""' not in content

    def test_multiple_modules_on_first_boot(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory", "crm"])
        cfg = mod.read_config()
        enabled = cfg["modules"]["enabled"]
        assert "inventory" in enabled
        assert "crm" in enabled


# ---------------------------------------------------------------------------
# set_enabled_modules — idempotency
# ---------------------------------------------------------------------------

class TestSetEnabledModulesIdempotency:
    """Calling set_enabled_modules twice with the same modules must not duplicate them."""

    def test_no_duplicate_on_repeat_call(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory"])
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        assert cfg["modules"]["enabled"].count("inventory") == 1

    def test_noop_when_all_already_enabled(self, tmp_path, monkeypatch):
        """If all requested modules are already enabled, file mtime must not change."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.set_enabled_modules(["inventory"])
        mtime_before = cfg_file.stat().st_mtime
        mod.set_enabled_modules(["inventory"])
        mtime_after = cfg_file.stat().st_mtime
        assert mtime_before == mtime_after, "File should not be rewritten when nothing changes"


# ---------------------------------------------------------------------------
# set_enabled_modules — additive, preserves existing config
# ---------------------------------------------------------------------------

class TestSetEnabledModulesPreservesConfig:
    """set_enabled_modules must not wipe or overwrite unrelated config sections."""

    def test_preserves_server_ports(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        # Setup wizard writes full config first
        mod.write_config({
            "server": {"api_port": 12345, "ui_port": 12346},
            "auth": {"jwt_secret": "mysecret"},
            "modules": {"enabled": []},
        })
        # Then set_enabled_modules adds a module
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        assert cfg["server"]["api_port"] == 12345
        assert cfg["server"]["ui_port"] == 12346

    def test_preserves_jwt_secret(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "auth": {"jwt_secret": "dontloseme"},
            "modules": {"enabled": []},
        })
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        assert cfg["auth"]["jwt_secret"] == "dontloseme"

    def test_additive_to_existing_modules(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)
        mod.write_config({"modules": {"enabled": ["inventory"]}})
        mod.set_enabled_modules(["crm"])
        cfg = mod.read_config()
        enabled = cfg["modules"]["enabled"]
        assert "inventory" in enabled
        assert "crm" in enabled


# ---------------------------------------------------------------------------
# Full first-boot → setup wizard sequence
# ---------------------------------------------------------------------------

class TestFirstBootSequence:
    """End-to-end: simulate the exact sequence the Electron app runs on first boot.

    Step 1: seedDefaultModules() calls set_enabled_modules (no config.toml exists)
    Step 2: Setup wizard completes, writes full config via write_config
    Step 3: App restarts, read_config must have both modules AND server config
    """

    def test_modules_survive_full_config_write(self, tmp_path, monkeypatch):
        mod, _ = _reload_config(tmp_path, monkeypatch)

        # Step 1: Electron seeds modules before config.toml exists
        mod.set_enabled_modules(["inventory", "crm"])

        # Step 2: Setup wizard writes full config (must read existing modules first)
        existing = mod.read_config()
        existing.setdefault("database", {})["url"] = "postgresql+asyncpg://x:y@localhost/db"
        existing.setdefault("auth", {})["jwt_secret"] = "strongsecret"
        existing.setdefault("server", {})["api_port"] = 54321
        existing.setdefault("server", {})["ui_port"] = 54322
        mod.write_config(existing)

        # Step 3: App restarts — read full config
        cfg = mod.read_config()
        assert "inventory" in cfg["modules"]["enabled"]
        assert "crm" in cfg["modules"]["enabled"]
        assert cfg["server"]["api_port"] == 54321
        assert cfg["auth"]["jwt_secret"] == "strongsecret"

    def test_subsequent_boot_loads_correct_modules(self, tmp_path, monkeypatch):
        """After first boot completes, subsequent reads must return persisted modules."""
        mod, _ = _reload_config(tmp_path, monkeypatch)

        # First boot full sequence
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        cfg.setdefault("server", {})["api_port"] = 9000
        cfg.setdefault("auth", {})["jwt_secret"] = "secret99"
        mod.write_config(cfg)

        # Simulate app restart: reload module, read config fresh
        importlib.reload(mod)
        cfg2 = mod.read_config()
        assert "inventory" in cfg2["modules"]["enabled"]
        assert cfg2["server"]["api_port"] == 9000

    def test_no_modules_on_subsequent_boot_means_setup_not_run(self, tmp_path, monkeypatch):
        """If config.toml exists but [modules] is missing, set_enabled_modules must still write it."""
        mod, _ = _reload_config(tmp_path, monkeypatch)
        # Simulate a partial config written by something else (no [modules])
        mod.write_config({
            "server": {"api_port": 8000, "ui_port": 8080},
            "auth": {"jwt_secret": "s3cr3t"},
        })
        # set_enabled_modules must add [modules] without destroying [server]
        mod.set_enabled_modules(["inventory"])
        cfg = mod.read_config()
        assert "inventory" in cfg["modules"]["enabled"]
        assert cfg["server"]["api_port"] == 8000
