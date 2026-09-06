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


# ---------------------------------------------------------------------------
# Deployment credential ([cloud] deployment_credential / deployment_associated)
# ---------------------------------------------------------------------------

class TestDeploymentCredential:
    """The reusable partner deployment credential and its association marker
    survive [cloud] writes, load into settings, and are removed only through the
    dedicated association helper."""

    def test_write_config_roundtrips_deployment_credential(self, tmp_path, monkeypatch):
        """A write that touches only unrelated [cloud] fields must preserve both
        deployment keys: the not-yet-consumed credential (when non-empty) and the
        association marker (when true). Without this every [cloud] write would
        silently erase the credential before the first hello can send it."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {
                "token": "gw-live",
                "instance_id": "iid-1",
                "public_url": "",
                "backup_encryption_key": "",
                "tos_version": "",
                "deployment_credential": "deploy-cred-xyz",
                "deployment_associated": True,
            },
        })
        content = cfg_file.read_text()
        assert 'deployment_credential = "deploy-cred-xyz"' in content
        assert "deployment_associated = true" in content
        cfg = mod.read_config()
        assert cfg["cloud"]["deployment_credential"] == "deploy-cred-xyz"
        assert cfg["cloud"]["deployment_associated"] is True

    def test_write_config_omits_empty_deployment_credential(self, tmp_path, monkeypatch):
        """An empty credential and an unset marker emit neither key (conditional,
        mirroring the disconnected idiom) so a direct install's config is unchanged."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "gw", "instance_id": "iid", "public_url": "",
                      "backup_encryption_key": "", "tos_version": ""},
        })
        content = cfg_file.read_text()
        assert "deployment_credential" not in content
        assert "deployment_associated" not in content

    def test_load_cloud_config_reads_deployment_fields(self, tmp_path, monkeypatch):
        """Startup load reads both new [cloud] fields into settings."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "", "instance_id": "", "public_url": "",
                      "backup_encryption_key": "", "tos_version": "",
                      "deployment_credential": "cred-load", "deployment_associated": True},
        })
        mod.settings.deployment_credential = ""
        mod.settings.deployment_associated = False
        mod.load_cloud_config()
        assert mod.settings.deployment_credential == "cred-load"
        assert mod.settings.deployment_associated is True

    def test_record_deployment_association_persists_identity(self, tmp_path, monkeypatch):
        """The helper persists the relay-issued gateway_token and instance_id, pops
        the credential, sets the marker, drops the consumed nonce, and updates the
        in-memory settings - all in one write."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "", "instance_id": "", "public_url": "",
                      "backup_encryption_key": "", "tos_version": "",
                      "deployment_credential": "cred-to-pop",
                      "deployment_nonce": "nonce-consumed"},
        })
        mod.settings.deployment_credential = "cred-to-pop"
        mod.settings.deployment_associated = False
        mod.settings.deployment_nonce = "nonce-consumed"

        mod.record_deployment_association(gateway_token="gw-live", instance_id="iid-relay")

        cfg = mod.read_config()
        cloud = cfg.get("cloud", {})
        assert cloud["token"] == "gw-live"
        assert cloud["instance_id"] == "iid-relay"
        assert cloud["deployment_associated"] is True
        assert "deployment_credential" not in cloud
        assert "deployment_nonce" not in cloud
        assert mod.settings.deployment_credential == ""
        assert mod.settings.deployment_associated is True
        assert mod.settings.gateway_token == "gw-live"
        assert mod.settings.gateway_instance_id == "iid-relay"
        content = cfg_file.read_text()
        assert "deployment_credential" not in content
        assert "deployment_associated = true" in content

    def test_ensure_deployment_nonce_generates_and_persists(self, tmp_path, monkeypatch):
        """With no nonce set, the helper generates a non-empty value, stores it in
        settings, and persists it to config."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.settings.deployment_nonce = ""

        nonce = mod.ensure_deployment_nonce()

        assert nonce
        assert mod.settings.deployment_nonce == nonce
        cfg = mod.read_config()
        assert cfg["cloud"]["deployment_nonce"] == nonce

    def test_ensure_deployment_nonce_reuses_existing(self, tmp_path, monkeypatch):
        """An already-set nonce is returned as-is, never regenerated."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.settings.deployment_nonce = "existing-nonce"

        nonce = mod.ensure_deployment_nonce()

        assert nonce == "existing-nonce"
        assert mod.settings.deployment_nonce == "existing-nonce"

    def test_write_config_roundtrips_deployment_nonce(self, tmp_path, monkeypatch):
        """The serializer emits deployment_nonce when set and re-reads it."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "", "instance_id": "", "public_url": "",
                      "backup_encryption_key": "", "tos_version": "",
                      "deployment_nonce": "nonce-abc123"},
        })
        content = cfg_file.read_text()
        assert 'deployment_nonce = "nonce-abc123"' in content
        cfg = mod.read_config()
        assert cfg["cloud"]["deployment_nonce"] == "nonce-abc123"

    def test_write_config_omits_empty_deployment_nonce(self, tmp_path, monkeypatch):
        """An unset nonce emits no key, matching the credential/disconnected idiom."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "gw", "instance_id": "iid", "public_url": "",
                      "backup_encryption_key": "", "tos_version": ""},
        })
        content = cfg_file.read_text()
        assert "deployment_nonce" not in content

    def test_load_cloud_config_reads_deployment_nonce(self, tmp_path, monkeypatch):
        """Startup load reads deployment_nonce from [cloud] into settings."""
        mod, cfg_file = _reload_config(tmp_path, monkeypatch)
        mod.write_config({
            "cloud": {"token": "", "instance_id": "", "public_url": "",
                      "backup_encryption_key": "", "tos_version": "",
                      "deployment_nonce": "nonce-load"},
        })
        mod.settings.deployment_nonce = ""
        mod.load_cloud_config()
        assert mod.settings.deployment_nonce == "nonce-load"


def test_tomli_declared_for_pre_311():
    """celerp/config.py falls back to `import tomli as tomllib` on 3.10; the
    fallback only works if packaging declares tomli for those interpreters."""
    # Read the manifest the same way celerp.config does, or this check for 3.10 support
    # cannot itself run on 3.10: tomllib is stdlib only from 3.11.
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    tomli_specs = [d for d in deps if d.split(";")[0].strip().startswith("tomli")]
    assert tomli_specs, "tomli missing from dependencies; 3.10 config read fails"
    assert any('python_version < "3.11"' in d for d in tomli_specs)
