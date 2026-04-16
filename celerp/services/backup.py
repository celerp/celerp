# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
import subprocess
from dataclasses import dataclass

from celerp.config import settings

_NONCE_BYTES = 12


@dataclass
class BackupResult:
    ok: bool
    size_bytes: int
    error: str | None = None


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
        result = subprocess.run(
            ["pg_dump", "--format=custom", "--no-password", pg_url],
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


def _relay_base_url() -> str:
    """Derive the relay HTTP base URL from gateway settings."""
    if settings.gateway_http_url:
        return settings.gateway_http_url.rstrip("/")
    url = settings.gateway_url
    url = url.replace("wss://", "https://").replace("ws://", "http://")
    url = url.split("/ws/")[0]
    return url.rstrip("/")


def _session_headers() -> dict[str, str]:
    """Return auth headers for relay API calls."""
    from celerp.gateway.state import get_session_token
    return {
        "X-Session-Token": get_session_token(),
        "X-Instance-ID": settings.gateway_instance_id,
    }


async def upload_to_relay(
    blob: bytes,
    backup_type: str = "database",
    label: str | None = None,
) -> dict:
    """Upload encrypted blob to relay REST API. Returns response dict."""
    import httpx
    url = f"{_relay_base_url()}/backup/upload"
    params: dict[str, str] = {"backup_type": backup_type}
    if label:
        params["label"] = label

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            url,
            headers=_session_headers(),
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
    url = f"{_relay_base_url()}/backup/{backup_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=_session_headers())
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
        result = subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-password", "-d", pg_url],
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
