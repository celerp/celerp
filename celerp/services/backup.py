# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Cloud backup service — pg_dump → AES-256-GCM encrypt → upload to relay.

Encryption:
  - Key: 32-byte random, base64-encoded, stored in config.toml [cloud] section
  - Nonce: 12-byte random, prepended to ciphertext
  - Wire format: nonce (12 bytes) + ciphertext (variable) + tag (16 bytes, appended by GCM)

Upload:
  - Encrypted blob is uploaded to the relay REST API via POST /backup/upload
  - Auth: X-Session-Token + X-Instance-ID headers (from live gateway session)
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from celerp.config import settings
from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers

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


async def upload_to_relay(
    blob: bytes,
    backup_type: str = "database",
    label: str | None = None,
) -> dict:
    """Upload encrypted blob to relay REST API. Returns response dict."""
    import httpx
    url = f"{relay_http_url()}/backup/upload"
    params: dict[str, str] = {"backup_type": backup_type}
    if label:
        params["label"] = label

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            url,
            headers=relay_session_headers(),
            params=params,
            files={"file": ("backup.bin", io.BytesIO(blob), "application/octet-stream")},
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Relay upload failed with HTTP {response.status_code}: {response.text[:200]}"
        )
    return response.json()


async def download_from_relay(backup_id: str) -> bytes:
    """Download encrypted backup blob from relay via presigned URL."""
    import httpx
    url = f"{relay_http_url()}/backup/{backup_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=relay_session_headers())
    if response.status_code != 200:
        raise RuntimeError(f"Failed to get download URL: HTTP {response.status_code}")

    presigned_url = response.json()["url"]
    async with httpx.AsyncClient(timeout=120) as client:
        dl = await client.get(presigned_url)
    if dl.status_code != 200:
        raise RuntimeError(f"Download failed: HTTP {dl.status_code}")
    return dl.content


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


async def run_backup(label: str | None = None) -> BackupResult:
    """Orchestrate a full DB backup: dump → encrypt → upload to relay.

    Returns BackupResult. Never raises — errors are captured in result.error.
    """
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")

    if not get_session_token():
        return BackupResult(ok=False, size_bytes=0, error="Relay not connected - skipping backup")

    try:
        key = _parse_key(settings.backup_encryption_key)
        dump = dump_database(settings.database_url)
        blob = encrypt(dump, key)
        await upload_to_relay(blob, backup_type="database", label=label)
        return BackupResult(ok=True, size_bytes=len(blob))
    except Exception as exc:
        return BackupResult(ok=False, size_bytes=0, error=str(exc))


async def run_restore(backup_id: str) -> BackupResult:
    """Download, decrypt, and restore a database backup.

    Creates a safety backup first. Returns BackupResult.
    """
    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")

    try:
        # Safety backup first
        safety = await run_backup(label="pre-restore-safety")
        if not safety.ok:
            return BackupResult(ok=False, size_bytes=0, error=f"Safety backup failed: {safety.error}")

        key = _parse_key(settings.backup_encryption_key)
        encrypted = await download_from_relay(backup_id)
        dump = decrypt(encrypted, key)
        restore_database(dump, settings.database_url)
        return BackupResult(ok=True, size_bytes=len(dump))
    except Exception as exc:
        return BackupResult(ok=False, size_bytes=0, error=str(exc))
