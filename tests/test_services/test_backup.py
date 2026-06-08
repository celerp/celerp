# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/services/backup.py.

Covers:
  - _parse_key: valid key, bad base64, wrong length
  - encrypt / decrypt round-trip
  - dump_database: success, pg_dump not found, exit code failure, timeout
  - upload_to_relay: success + failure
  - run_backup: all error paths + happy path
  - restore_database: success + failure
  - run_restore: happy path + safety backup failure
"""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import respx
import httpx

from celerp.config import settings
from celerp.gateway.state import relay_http_url
from celerp.services.backup import (
    BackupResult,
    _parse_key,
    decrypt,
    dump_database,
    encrypt,
    restore_database,
    run_backup,
    upload_to_relay,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_key() -> tuple[bytes, str]:
    raw = secrets.token_bytes(32)
    return raw, base64.b64encode(raw).decode()


# ── _parse_key ────────────────────────────────────────────────────────────────

def test_parse_key_valid():
    raw, b64 = _make_key()
    assert _parse_key(b64) == raw


def test_parse_key_bad_base64():
    with pytest.raises(ValueError, match="not valid base64"):
        _parse_key("not!!base64$$")


def test_parse_key_wrong_length():
    short = base64.b64encode(b"tooshort").decode()
    with pytest.raises(ValueError, match="32 bytes"):
        _parse_key(short)


# ── encrypt / decrypt ─────────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    raw, _ = _make_key()
    plaintext = b"hello world pg_dump output"
    blob = encrypt(plaintext, raw)
    assert blob != plaintext
    assert decrypt(blob, raw) == plaintext


def test_decrypt_blob_too_short():
    raw, _ = _make_key()
    with pytest.raises(ValueError, match="too short"):
        decrypt(b"\x00" * 5, raw)


def test_encrypt_produces_unique_nonces():
    raw, _ = _make_key()
    plaintext = b"same plaintext"
    blob1 = encrypt(plaintext, raw)
    blob2 = encrypt(plaintext, raw)
    assert blob1 != blob2


# ── dump_database ─────────────────────────────────────────────────────────────

def test_dump_database_success(monkeypatch):
    fake_dump = b"PGDUMP_CUSTOM_FORMAT_DATA"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=fake_dump, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dump_database("postgresql+asyncpg://u:p@localhost/db")
    assert result == fake_dump


def test_dump_database_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_dump not found"):
        dump_database("postgresql+asyncpg://u:p@localhost/db")


def test_dump_database_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"connection refused")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_dump failed"):
        dump_database("postgresql+asyncpg://u:p@localhost/db")


def test_dump_database_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=300)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        dump_database("postgresql+asyncpg://u:p@localhost/db")


# ── relay_http_url ────────────────────────────────────────────────────────────

def test_relay_base_url_from_gateway_url():
    import celerp.config as _cfg
    orig_http = _cfg.settings.gateway_http_url
    orig_ws = _cfg.settings.gateway_url
    _cfg.settings.gateway_http_url = ""
    _cfg.settings.gateway_url = "wss://relay.celerp.com/ws/connect"
    try:
        assert relay_http_url() == "https://relay.celerp.com"
    finally:
        _cfg.settings.gateway_http_url = orig_http
        _cfg.settings.gateway_url = orig_ws


def test_relay_base_url_from_http_url():
    import celerp.config as _cfg
    orig = _cfg.settings.gateway_http_url
    _cfg.settings.gateway_http_url = "https://custom-relay.example.com"
    try:
        assert relay_http_url() == "https://custom-relay.example.com"
    finally:
        _cfg.settings.gateway_http_url = orig


# ── upload_to_relay ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_upload_to_relay_success(monkeypatch):
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_instance_id", "test-instance")
    import celerp.gateway.state as gs
    gs.set_session_token("test-token")

    respx.post("https://relay.test.com/backup/upload").mock(
        return_value=httpx.Response(200, json={"id": "abc", "size_bytes": 100})
    )
    result = await upload_to_relay(b"data", backup_type="database", label="test")
    assert result["id"] == "abc"
    gs.set_session_token("")


@pytest.mark.asyncio
@respx.mock
async def test_upload_to_relay_failure(monkeypatch):
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_instance_id", "test-instance")
    import celerp.gateway.state as gs
    gs.set_session_token("test-token")

    respx.post("https://relay.test.com/backup/upload").mock(
        return_value=httpx.Response(413, text="Quota exceeded")
    )
    with pytest.raises(RuntimeError, match="HTTP 413"):
        await upload_to_relay(b"data")
    gs.set_session_token("")


# ── restore_database ──────────────────────────────────────────────────────────

def test_restore_database_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    restore_database(b"dump_data", "postgresql+asyncpg://u:p@localhost/db")


def test_restore_database_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("pg_restore")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_restore not found"):
        restore_database(b"dump_data", "postgresql+asyncpg://u:p@localhost/db")


def test_restore_database_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"ERROR: relation does not exist")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_restore failed"):
        restore_database(b"dump_data", "postgresql+asyncpg://u:p@localhost/db")


# ── run_backup ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_backup_settings():
    orig_key = settings.backup_encryption_key
    yield
    settings.backup_encryption_key = orig_key


@pytest.mark.asyncio
async def test_run_backup_no_key():
    settings.backup_encryption_key = ""
    result = await run_backup()
    assert not result.ok
    assert "BACKUP_ENCRYPTION_KEY" in result.error


@pytest.mark.asyncio
@respx.mock
async def test_run_backup_happy_path(monkeypatch):
    _, b64 = _make_key()
    settings.backup_encryption_key = b64
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(__import__("celerp.config", fromlist=["settings"]).settings, "gateway_instance_id", "test-instance")
    import celerp.gateway.state as gs
    gs.set_session_token("test-token")

    monkeypatch.setattr(
        "celerp.services.backup.dump_database",
        lambda url: b"FAKE_PG_DUMP",
    )
    respx.post("https://relay.test.com/backup/upload").mock(
        return_value=httpx.Response(200, json={"id": "abc", "size_bytes": 100})
    )

    result = await run_backup()
    assert result.ok
    assert result.size_bytes > 0
    assert result.error is None
    gs.set_session_token("")


@pytest.mark.asyncio
async def test_run_backup_dump_failure(monkeypatch):
    _, b64 = _make_key()
    settings.backup_encryption_key = b64

    monkeypatch.setattr(
        "celerp.services.backup.dump_database",
        lambda url: (_ for _ in ()).throw(RuntimeError("pg_dump not found in PATH")),
    )
    import celerp.gateway.state as gs
    gs.set_session_token("test-token")

    result = await run_backup()
    assert not result.ok
    assert "pg_dump" in result.error


# ── Mac PATH resolution (regression: backup 500 on Mac Electron) ─────────────

def _make_fake_pg_bin(tmp_path, *tools: str) -> Path:
    """Create fake pg tool executables in a temp dir; return the dir."""
    bin_dir = tmp_path / "fake_pg_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools or ("pg_dump",):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\necho fake\n")
        exe.chmod(0o755)
    return bin_dir


class TestFindPgTool:
    """Tests for celerp.services.backup._find_pg_tool.

    Regression: Mac GUI-launched Electron apps don't have /opt/homebrew/bin
    or /usr/local/opt/postgresql@*/bin in PATH, so pg_dump / pg_restore
    can't be found and backup fails with a 500.

    Resolution order:
      1. settings.pg_bin_dir — explicit override (Electron CELERP_PG_BIN_DIR or
         user config.toml [backup] pg_bin_dir)
      2. shutil.which() — respects current PATH (terminal, asdf, nix, …)
      3. _PG_CANDIDATE_DIRS — macOS fallback for stripped .app PATH
    """

    def test_pg_bin_dir_setting_takes_priority(self, monkeypatch, tmp_path):
        """settings.pg_bin_dir is checked before PATH and candidate dirs."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "linux")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [])

        bin_dir = _make_fake_pg_bin(tmp_path, "pg_dump")
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", str(bin_dir))

        result = backup_mod._find_pg_tool("pg_dump")
        assert result == str(bin_dir / "pg_dump")

    def test_pg_bin_dir_finds_exe_on_windows(self, monkeypatch, tmp_path):
        """On Windows the bundled tool is pg_dump.exe; resolution must find it."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "win32")
        bin_dir = tmp_path / "pgwin"
        bin_dir.mkdir()
        (bin_dir / "pg_dump.exe").write_text("")
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", str(bin_dir))

        result = backup_mod._find_pg_tool("pg_dump")
        assert result == str(bin_dir / "pg_dump.exe")

    def test_pg_bin_dir_is_authoritative_no_path_fallback(self, monkeypatch, tmp_path):
        """If pg_bin_dir is set but the tool isn't there, fail loudly — do NOT
        fall back to PATH. A packaged build pointed at a missing bundle must not
        silently use a system pg_dump of unknown version (plan §7.2)."""
        from celerp.services import backup as backup_mod

        empty_dir = tmp_path / "empty_override"
        empty_dir.mkdir()
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", str(empty_dir))

        # A valid tool on PATH must be ignored when pg_bin_dir is set.
        bin_dir = _make_fake_pg_bin(tmp_path, "pg_dump")
        monkeypatch.setenv("PATH", str(bin_dir))

        with pytest.raises(FileNotFoundError, match="bundled PostgreSQL tools are missing"):
            backup_mod._find_pg_tool("pg_dump")

    def test_finds_tool_via_which_when_in_path(self, monkeypatch, tmp_path):
        """shutil.which() is tried when pg_bin_dir is unset; full path is returned."""
        import shutil
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", "")
        bin_dir = _make_fake_pg_bin(tmp_path, "pg_dump")
        monkeypatch.setenv("PATH", str(bin_dir))
        assert shutil.which("pg_dump", path=str(bin_dir)) is not None
        result = backup_mod._find_pg_tool("pg_dump")
        assert result == str(bin_dir / "pg_dump")

    def test_falls_back_to_candidate_dirs_on_darwin_when_not_in_path(self, monkeypatch, tmp_path):
        """On macOS with empty PATH and no pg_bin_dir, candidate dirs are probed."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", "")

        bin_dir = _make_fake_pg_bin(tmp_path, "pg_restore")
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])

        result = backup_mod._find_pg_tool("pg_restore")
        assert result == str(bin_dir / "pg_restore")

    def test_raises_on_non_darwin_when_not_in_path(self, monkeypatch, tmp_path):
        """On Linux/Windows with no pg_bin_dir, raises FileNotFoundError when missing."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "linux")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [])
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", "")

        with pytest.raises(FileNotFoundError, match="pg_restore not found"):
            backup_mod._find_pg_tool("pg_restore")

    def test_skips_candidate_dir_without_tool(self, monkeypatch, tmp_path):
        """A candidate dir that exists but lacks the tool is not used."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", "")

        empty_dir = tmp_path / "no_pg_here"
        empty_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [empty_dir])

        with pytest.raises(FileNotFoundError):
            backup_mod._find_pg_tool("pg_restore")

    def test_returns_first_matching_candidate(self, monkeypatch, tmp_path):
        """First matching candidate dir wins (order preserved)."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(backup_mod.settings, "pg_bin_dir", "")

        b1 = _make_fake_pg_bin(tmp_path / "b1", "pg_dump")
        b2 = _make_fake_pg_bin(tmp_path / "b2", "pg_dump")
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [b1, b2])

        result = backup_mod._find_pg_tool("pg_dump")
        assert result == str(b1 / "pg_dump")


class TestDumpDatabaseUsesResolvedPath:
    """dump_database must resolve pg_dump via _find_pg_tool, not bare PATH lookup."""

    def test_finds_pg_dump_via_candidate_dir_on_macos(self, monkeypatch, tmp_path):
        """When pg_dump is only in a candidate dir (not in PATH), dump_database finds it."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text("#!/bin/sh\necho FAKE_DUMP\n")
        pg_dump.chmod(0o755)

        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")

        result = dump_database("postgresql+asyncpg://u:p@localhost/db")
        assert b"FAKE_DUMP" in result

    def test_finds_pg_dump_via_path_on_linux(self, monkeypatch, tmp_path):
        """On Linux, pg_dump is found via PATH (shutil.which) — no candidate probing."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "linux")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text("#!/bin/sh\necho FAKE_LINUX_DUMP\n")
        pg_dump.chmod(0o755)

        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        result = dump_database("postgresql+asyncpg://u:p@localhost/db")
        assert b"FAKE_LINUX_DUMP" in result


class TestRestoreDatabaseUsesResolvedPath:
    """restore_database must resolve pg_restore via _find_pg_tool."""

    def test_finds_pg_restore_via_candidate_dir_on_macos(self, monkeypatch, tmp_path):
        """When pg_restore is only in a candidate dir (not in PATH), restore_database finds it."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        (bin_dir / "pg_restore").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "pg_restore").chmod(0o755)

        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")

        # Should not raise — binary is found via candidate dir
        restore_database(b"dump", "postgresql+asyncpg://u:p@localhost/db")

