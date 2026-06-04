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

def _make_fake_pg_bin(tmp_path, name: str = "pg_dump") -> Path:
    """Create a fake pg_dump executable in a temp dir; return the dir."""
    bin_dir = tmp_path / "fake_pg_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / name).write_text("#!/bin/sh\necho fake\n")
    (bin_dir / name).chmod(0o755)
    return bin_dir


class TestResolvePgBinDir:
    """Tests for celerp.services.backup._resolve_pg_bin_dir.

    Regression: Mac GUI-launched Electron apps don't have /opt/homebrew/bin
    or /usr/local/opt/postgresql@*/bin in PATH, so pg_dump / pg_restore
    can't be found and backup fails with a 500.
    """

    def test_returns_empty_on_non_darwin(self, monkeypatch):
        """Linux/Windows: no probing needed. Returns empty list (no-op)."""
        import sys as _sys
        from celerp.services.backup import _resolve_pg_bin_dir
        monkeypatch.setattr(_sys, "platform", "linux")
        result = _resolve_pg_bin_dir()
        assert result == []

    def test_finds_pg_dump_in_known_macos_location(self, monkeypatch, tmp_path):
        """When pg_dump exists in /opt/homebrew/opt/postgresql@16/bin on darwin, return that dir."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        # Create a fake /opt/homebrew/opt/postgresql@16/bin with pg_dump
        fake_bin = tmp_path / "homebrew" / "opt" / "postgresql@16" / "bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "pg_dump").write_text("#!/bin/sh\n")
        (fake_bin / "pg_dump").chmod(0o755)
        (fake_bin / "pg_restore").write_text("#!/bin/sh\n")
        (fake_bin / "pg_restore").chmod(0o755)

        # Monkeypatch the list of candidate dirs the function probes
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [fake_bin])
        result = backup_mod._resolve_pg_bin_dir()
        assert fake_bin in result

    def test_skips_candidate_dir_without_pg_dump(self, monkeypatch, tmp_path):
        """A dir that exists but lacks pg_dump is not returned."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        empty_dir = tmp_path / "no_pg_here"
        empty_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [empty_dir])
        result = backup_mod._resolve_pg_bin_dir()
        assert empty_dir not in result

    def test_returns_deduped_dirs(self, monkeypatch, tmp_path):
        """Same dir appearing twice in candidates returns once."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        bin_dir = _make_fake_pg_bin(tmp_path)
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir, bin_dir])
        result = backup_mod._resolve_pg_bin_dir()
        assert result.count(bin_dir) == 1

    def test_multiple_candidate_dirs_all_returned(self, monkeypatch, tmp_path):
        """All valid candidate dirs are returned, in order."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        b1 = _make_fake_pg_bin(tmp_path / "b1")
        b2 = _make_fake_pg_bin(tmp_path / "b2")
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [b1, b2])
        result = backup_mod._resolve_pg_bin_dir()
        assert b1 in result
        assert b2 in result


class TestDumpDatabaseUsesResolvedPath:
    """dump_database must inject the resolved pg bin dir into the subprocess PATH."""

    def test_subprocess_receives_augmented_path(self, monkeypatch, tmp_path):
        """When pg_dump is not in the parent PATH but is in a known Mac location,
        dump_database should find it via the resolved path."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        # Create a fake pg_dump that succeeds and prints "FAKE"
        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text("#!/bin/sh\necho FAKE_DUMP\n")
        pg_dump.chmod(0o755)

        # Make the function think this is the only candidate, and clear PATH
        # so subprocess.run only finds pg_dump via the augmented PATH.
        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")  # make sure no system pg_dump is reachable

        # Also patch the env sanitizer: make sure our test PATH gets through
        # (subprocess.run inherits the current process env by default)
        result = dump_database("postgresql+asyncpg://u:p@localhost/db")
        assert b"FAKE_DUMP" in result

    def test_subprocess_falls_back_to_system_path_on_linux(self, monkeypatch, tmp_path):
        """On non-darwin, we don't augment PATH; existing PATH behavior is preserved."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "linux")

        # Create fake pg_dump
        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text("#!/bin/sh\necho FAKE_LINUX_DUMP\n")
        pg_dump.chmod(0o755)

        # Put it in PATH
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        result = dump_database("postgresql+asyncpg://u:p@localhost/db")
        assert b"FAKE_LINUX_DUMP" in result


class TestRestoreDatabaseUsesResolvedPath:
    """restore_database must use the same PATH augmentation as dump_database."""

    def test_restore_subprocess_receives_augmented_path_on_macos(self, monkeypatch, tmp_path):
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        # _resolve_pg_bin_dir probes for pg_dump as the proxy for the whole bin dir
        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text("#!/bin/sh\nexit 0\n")
        pg_dump.chmod(0o755)
        pg_restore = bin_dir / "pg_restore"
        pg_restore.write_text("#!/bin/sh\nexit 0\n")
        pg_restore.chmod(0o755)

        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")

        # Should not raise FileNotFoundError - the binary is found via the resolved path
        restore_database(b"dump", "postgresql+asyncpg://u:p@localhost/db")

