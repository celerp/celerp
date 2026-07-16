# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backup primitives — pg_dump/pg_restore and AES-256-GCM encrypt/decrypt.

Encryption:
  - Key: 32-byte random, base64-encoded, stored in config.toml [cloud] section
  - Nonce: 12-byte random, prepended to ciphertext
  - Wire format: nonce (12 bytes) + ciphertext (variable) + tag (16 bytes, appended by GCM)

These primitives are shared by the local export/import path and the content-addressed
cloud snapshot client (``backup_repo``).
"""

from __future__ import annotations

import base64
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from celerp.config import settings

_NONCE_BYTES = 12

# macOS fallback dirs for .app builds whose PATH is stripped to /usr/bin:/bin:/usr/sbin:/sbin.
# shutil.which() is tried first (handles terminal launches, asdf, nix, mise, etc.).
# This list is only consulted when which() comes up empty.
_PG_CANDIDATE_DIRS: tuple[Path, ...] = (
    # Homebrew unversioned formula — symlinks land here on both architectures
    Path("/opt/homebrew/bin"),              # Apple Silicon
    Path("/usr/local/bin"),                 # Intel
    # Homebrew versioned formulae (Apple Silicon)
    Path("/opt/homebrew/opt/postgresql@17/bin"),
    Path("/opt/homebrew/opt/postgresql@16/bin"),
    Path("/opt/homebrew/opt/postgresql@15/bin"),
    Path("/opt/homebrew/opt/postgresql@14/bin"),
    Path("/opt/homebrew/opt/postgresql@13/bin"),
    # Homebrew versioned formulae (Intel)
    Path("/usr/local/opt/postgresql@17/bin"),
    Path("/usr/local/opt/postgresql@16/bin"),
    Path("/usr/local/opt/postgresql@15/bin"),
    Path("/usr/local/opt/postgresql@14/bin"),
    Path("/usr/local/opt/postgresql@13/bin"),
    # Postgres.app
    Path("/Applications/Postgres.app/Contents/Versions/latest/bin"),
    # EnterpriseDB / official installer
    Path("/Library/PostgreSQL/17/bin"),
    Path("/Library/PostgreSQL/16/bin"),
    Path("/Library/PostgreSQL/15/bin"),
    Path("/Library/PostgreSQL/14/bin"),
)


def _find_pg_tool(name: str) -> str:
    """Return the full path to a PostgreSQL tool (pg_dump, pg_restore, …).

    Resolution order:
      1. settings.pg_bin_dir — explicit override; set by the Electron app via
         CELERP_PG_BIN_DIR (points to bundled tools inside the .app bundle) or
         by the user via config.toml [backup] pg_bin_dir. When set it is
         AUTHORITATIVE: the tool must resolve here or we fail loudly — no PATH
         fallback. A packaged build pointed at a missing bundle must not silently
         dump with a system pg_dump of unknown version (incompatible/corrupt
         backups).
      2. shutil.which() — respects the current process PATH; works for any
         installation that correctly exports its bin dir (terminal, asdf, nix,
         mise, MacPorts, unversioned Homebrew formula, …). Only reached when
         pg_bin_dir is unset (dev, self-hosted without an override).
      3. macOS candidate dirs — legacy fallback for .app / Electron builds that
         predate the CELERP_PG_BIN_DIR injection (Homebrew, Postgres.app, EDB).

    Raises FileNotFoundError with a clear message if the tool cannot be found.
    """
    import shutil
    # On Windows the tools are pg_dump.exe / pg_restore.exe.
    names = [name, f"{name}.exe"] if sys.platform == "win32" else [name]
    if settings.pg_bin_dir:
        for n in names:
            candidate = Path(settings.pg_bin_dir) / n
            if candidate.is_file():
                return str(candidate)
        # Explicit bundle dir given but the tool isn't in it: packaging error or
        # a bad user override. Fail loudly rather than falling back to a system
        # pg_dump of unknown version, which risks an incompatible/corrupt backup.
        raise FileNotFoundError(
            f"{name} not found in configured pg_bin_dir ({settings.pg_bin_dir}). "
            "The bundled PostgreSQL tools are missing — reinstall the app, or set "
            "CELERP_PG_BIN_DIR to a valid client-tools directory."
        )
    if found := shutil.which(name):
        return found
    if sys.platform == "darwin":
        for d in _PG_CANDIDATE_DIRS:
            candidate = d / name
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError(
        f"{name} not found. Install PostgreSQL client tools or set CELERP_PG_BIN_DIR."
    )


@dataclass
class BackupResult:
    ok: bool
    size_bytes: int
    error: str | None = None
    # Non-fatal issues the user should be told about (e.g. modules enabled
    # on the source that aren't installed on the destination). Distinct
    # from `error` (which means the operation failed).
    warnings: list[str] = field(default_factory=list)
    # Set when the restored database could not be migrated to the current
    # schema: the data is present but unreadable until migrations run.
    schema_warning: str | None = None


def _parse_key(b64_key: str) -> bytes:
    """Decode and validate a base64-encoded 32-byte AES key."""
    try:
        key = base64.b64decode(b64_key)
    except Exception as exc:
        raise ValueError(f"BACKUP_ENCRYPTION_KEY is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise ValueError(
            f"BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes, got {len(key)}"
        )
    return key


def dump_database(database_url: str) -> bytes:
    """Run pg_dump against database_url and return raw dump bytes.

    Raises RuntimeError if pg_dump fails or is not found.
    """
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        pg_dump = _find_pg_tool("pg_dump")
        result = subprocess.run(
            [pg_dump, "--format=custom", "--no-password", pg_url],
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pg_dump not found in PATH — cannot create backup") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_dump timed out after 300 seconds") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {stderr}")
    return result.stdout


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns nonce + ciphertext+tag."""
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ciphertext


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM blob produced by encrypt(). Returns plaintext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < _NONCE_BYTES:
        raise ValueError("Blob too short to contain nonce")
    nonce = blob[:_NONCE_BYTES]
    ciphertext = blob[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def restore_database(dump_bytes: bytes, database_url: str) -> None:
    """Run pg_restore from dump bytes into database_url.

    Raises RuntimeError on failure.
    """
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        pg_restore = _find_pg_tool("pg_restore")
        result = subprocess.run(
            [pg_restore, "--clean", "--if-exists", "--no-password", "--no-privileges", "--no-owner", "-d", pg_url],
            input=dump_bytes,
            capture_output=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pg_restore not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_restore timed out after 600 seconds") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        # pg_restore returns non-zero for warnings too; only raise on real errors
        if "ERROR" in stderr.upper():
            raise RuntimeError(f"pg_restore failed (exit {result.returncode}): {stderr}")
