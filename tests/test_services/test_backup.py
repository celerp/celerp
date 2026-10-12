# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/services/backup.py.

Covers:
  - _parse_key: valid key, bad base64, wrong length
  - streamed AES-256-GCM: same wire format as one-shot AESGCM, both directions
  - dump_database: writes its file atomically; not found, exit code failure, timeout and
    disk full leave no partial file and keep the previous dump
  - restore_database_file: success + failure
  - _find_pg_tool resolution order
"""

from __future__ import annotations

import base64
import contextlib
import errno
import os
import secrets
import subprocess
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from celerp.gateway.state import relay_http_url
from celerp.services import backup as backup_mod
from celerp.services.backup import (
    _parse_key,
    dump_database,
    restore_database_file,
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
    with pytest.raises(ValueError, match="backup encryption key is set up wrong"):
        _parse_key("not!!base64$$")


def test_parse_key_wrong_length():
    short = base64.b64encode(b"tooshort").decode()
    with pytest.raises(ValueError, match="backup encryption key is set up wrong"):
        _parse_key(short)


# ── streamed encrypt / decrypt ────────────────────────────────────────────────

_SIZES = [0, 1, 15, 16, 17, 1000, 4096, 4097, 3 * 4096]


@pytest.mark.parametrize("size", _SIZES)
def test_streamed_encrypt_matches_aesgcm_oracle(tmp_path, size):
    """Red statement: encrypt_chunks and decrypt_file did not exist. The streamed blob is
    nonce + ciphertext + tag exactly as AESGCM writes it, so blobs already in the cloud
    still restore and new ones restore on older versions."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw, _ = _make_key()
    plain = secrets.token_bytes(size)
    src = tmp_path / "plain"
    src.write_bytes(plain)
    blob = b"".join(backup_mod.encrypt_chunks(src, raw, chunk=4096))
    assert len(blob) == size + 28
    assert AESGCM(raw).decrypt(blob[:12], blob[12:], None) == plain

    nonce = secrets.token_bytes(12)
    (tmp_path / "blob").write_bytes(nonce + AESGCM(raw).encrypt(nonce, plain, None))
    digest = backup_mod.decrypt_file(tmp_path / "blob", tmp_path / "out", raw, chunk=4096)
    assert (tmp_path / "out").read_bytes() == plain
    import hashlib
    assert digest == hashlib.sha256(plain).hexdigest()


def test_encrypt_produces_unique_nonces(tmp_path):
    raw, _ = _make_key()
    src = tmp_path / "plain"
    src.write_bytes(b"same plaintext")
    assert b"".join(backup_mod.encrypt_chunks(src, raw)) != b"".join(backup_mod.encrypt_chunks(src, raw))


@pytest.mark.parametrize("damage", ["tag", "short", "key"])
def test_decrypt_file_bad_blob_deletes_output(tmp_path, damage):
    """A damaged or too-short blob, or the wrong key, raises the damaged-backup message
    and leaves no half-decrypted file behind."""
    raw, _ = _make_key()
    src = tmp_path / "plain"
    src.write_bytes(b"x" * 10000)
    blob = bytearray(b"".join(backup_mod.encrypt_chunks(src, raw, chunk=4096)))
    if damage == "tag":
        blob[-1] ^= 1
    elif damage == "short":
        blob = blob[:20]
    else:
        raw = _make_key()[0]
    (tmp_path / "blob").write_bytes(bytes(blob))
    with pytest.raises(RuntimeError, match="damaged"):
        backup_mod.decrypt_file(tmp_path / "blob", tmp_path / "out", raw, chunk=4096)
    assert not (tmp_path / "out").exists()


# ── dump_database ─────────────────────────────────────────────────────────────

_URL = "postgresql+asyncpg://u:p@localhost/db"


def _writes_partial(body: bytes, returncode: int = 0, stderr: bytes = b"", exc=None):
    """A pg_dump runner that writes its --file (relative to cwd), then exits or raises."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        name = next(a.split("=", 1)[1] for a in cmd if a.startswith("--file="))
        (Path(kwargs["cwd"]) / name).write_bytes(body)
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=b"", stderr=stderr)
    return run, calls


def test_dump_database_writes_file_and_replaces_atomically(tmp_path):
    """Red statement: dump_database returned the whole dump as bytes from stdout. It now
    writes a .partial next to the destination (relative name, cwd set, as the restore
    tools are run) and renames it over the destination only when pg_dump succeeded."""
    dest = tmp_path / "database.dump"
    dest.write_bytes(b"OLD")
    run, calls = _writes_partial(b"NEW")
    assert dump_database(_URL, dest, runner=run) is None
    assert dest.read_bytes() == b"NEW"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["database.dump"]
    cmd, kwargs = calls[0]
    assert "--file=database.dump.partial" in cmd and kwargs["cwd"] == tmp_path
    assert cmd[-1] == "postgresql://u:p@localhost/db"


def test_dump_nonzero_exit_leaves_no_partial_and_keeps_previous_dump(tmp_path):
    dest = tmp_path / "database.dump"
    dest.write_bytes(b"OLD")
    run, _ = _writes_partial(b"HALF", returncode=1, stderr=b"connection refused")
    with pytest.raises(RuntimeError, match=r"couldn't be made.*connection refused"):
        dump_database(_URL, dest, runner=run)
    assert dest.read_bytes() == b"OLD"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["database.dump"]


@pytest.mark.parametrize("exc, message", [
    (subprocess.TimeoutExpired(["pg_dump"], timeout=300), "longer than 5 minutes"),
    (FileNotFoundError("pg_dump"), "database tool is missing"),
])
def test_dump_timeout_and_missing_tool_clean_up(tmp_path, exc, message):
    dest = tmp_path / "database.dump"
    run, _ = _writes_partial(b"HALF", exc=exc)
    with pytest.raises(RuntimeError, match=message):
        dump_database(_URL, dest, runner=run)
    assert list(tmp_path.iterdir()) == []


def test_dump_disk_full_leaves_no_partial(tmp_path):
    """Red statement: there was no file to clean up and a full disk surfaced as a raw
    OSError. It now removes the partial and says the disk is full."""
    dest = tmp_path / "database.dump"
    run, _ = _writes_partial(b"HALF", exc=OSError(errno.ENOSPC, "No space left on device"))
    with pytest.raises(RuntimeError, match="not enough free disk space"):
        dump_database(_URL, dest, runner=run)
    assert list(tmp_path.iterdir()) == []


def test_dump_database_not_found_before_running(monkeypatch, tmp_path):
    """No pg_dump on this computer: the runner is never called, nothing is written."""
    def missing(name):
        raise FileNotFoundError(name)
    monkeypatch.setattr(backup_mod, "_find_pg_tool", missing)
    with pytest.raises(RuntimeError, match="database tool is missing"):
        dump_database(_URL, tmp_path / "database.dump", runner=lambda *a, **k: pytest.fail("ran"))
    assert list(tmp_path.iterdir()) == []

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


# ── restore_database_file ──────────────────────────────────────────────────────────
# These cover how pg_restore and psql are found and invoked. The URL names no real database,
# so the mutating scope it runs in (covered in tests/test_db_fence.py) and the schema
# key it holds (covered in tests/test_schema_lock_race_pg.py) are stand-ins.

class _NoScope:
    def write_window(self):
        return contextlib.nullcontext()


@pytest.fixture
def _no_scope(monkeypatch):
    import celerp.cli
    from celerp.migrations import compatibility
    monkeypatch.setattr(compatibility, "mutating_scope",
                        lambda url, accept=None: contextlib.nullcontext(_NoScope()))
    monkeypatch.setattr(celerp.cli, "_migration_lock", lambda url: contextlib.nullcontext())


@pytest.mark.usefixtures("_no_scope")
def test_restore_database_success(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

    dump = tmp_path / "database.dump"
    dump.write_bytes(b"dump_data")
    monkeypatch.setattr(subprocess, "run", fake_run)
    restore_database_file(dump, "postgresql+asyncpg://u:p@localhost/db")
    assert [cmd[1:] for cmd, _ in calls[:2]] == [["--version"], ["--version"]]
    assert calls[2] == ([calls[2][0][0], "-l", "database.dump"], tmp_path)


@pytest.mark.usefixtures("_no_scope")
def test_restore_database_not_found(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("pg_restore")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="database tool is missing"):
        restore_database_file(Path("database.dump"), "postgresql+asyncpg://u:p@localhost/db")


@pytest.mark.usefixtures("_no_scope")
def test_restore_database_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"ERROR: relation does not exist")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=r"couldn't be restored.*relation does not exist"):
        restore_database_file(Path("database.dump"), "postgresql+asyncpg://u:p@localhost/db")


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

        with pytest.raises(FileNotFoundError, match="not found in the configured PostgreSQL tools folder"):
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


_FILE_WRITING_PG_DUMP = """#!/bin/sh
for a; do case "$a" in --file=*) echo {text} > "${{a#--file=}}";; esac; done
"""


class TestDumpDatabaseUsesResolvedPath:
    """dump_database must resolve pg_dump via _find_pg_tool, not bare PATH lookup."""

    def test_finds_pg_dump_via_candidate_dir_on_macos(self, monkeypatch, tmp_path):
        """When pg_dump is only in a candidate dir (not in PATH), dump_database finds it."""
        import sys as _sys
        monkeypatch.setattr(_sys, "platform", "darwin")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text(_FILE_WRITING_PG_DUMP.format(text="FAKE_DUMP"))
        pg_dump.chmod(0o755)

        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")

        dump_database(_URL, tmp_path / "database.dump")
        assert b"FAKE_DUMP" in (tmp_path / "database.dump").read_bytes()

    def test_finds_pg_dump_via_path_on_linux(self, monkeypatch, tmp_path):
        """On Linux, pg_dump is found via PATH (shutil.which) — no candidate probing."""
        import sys as _sys
        monkeypatch.setattr(_sys, "platform", "linux")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        pg_dump = bin_dir / "pg_dump"
        pg_dump.write_text(_FILE_WRITING_PG_DUMP.format(text="FAKE_LINUX_DUMP"))
        pg_dump.chmod(0o755)

        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        dump_database(_URL, tmp_path / "database.dump")
        assert b"FAKE_LINUX_DUMP" in (tmp_path / "database.dump").read_bytes()


@pytest.mark.usefixtures("_no_scope")
class TestRestoreDatabaseUsesResolvedPath:
    """restore_database_file must resolve pg_restore and psql via _find_pg_tool."""

    def test_finds_pg_restore_via_candidate_dir_on_macos(self, monkeypatch, tmp_path):
        """When pg_restore and psql are only in a candidate dir (not in PATH), restore_database_file finds them."""
        import sys as _sys
        from celerp.services import backup as backup_mod
        monkeypatch.setattr(_sys, "platform", "darwin")

        bin_dir = tmp_path / "fake_pg"
        bin_dir.mkdir()
        for tool in ("pg_restore", "psql"):
            (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / tool).chmod(0o755)

        monkeypatch.setattr(backup_mod, "_PG_CANDIDATE_DIRS", [bin_dir])
        monkeypatch.setenv("PATH", "")

        (tmp_path / "database.dump").write_bytes(b"PGDMP")
        # Should not raise — binary is found via candidate dir
        restore_database_file(tmp_path / "database.dump", "postgresql+asyncpg://u:p@localhost/db")

