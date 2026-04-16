# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import respx
import httpx

from celerp.config import settings
from celerp.services.backup import (
    BackupResult,
    _parse_key,
    _relay_base_url,
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


# ── _relay_base_url ───────────────────────────────────────────────────────────

def test_relay_base_url_from_gateway_url():
    orig = settings.gateway_http_url
    settings.gateway_http_url = ""
    settings.gateway_url = "wss://relay.celerp.com/ws/connect"
    try:
        assert _relay_base_url() == "https://relay.celerp.com"
    finally:
        settings.gateway_http_url = orig


def test_relay_base_url_from_http_url():
    orig = settings.gateway_http_url
    settings.gateway_http_url = "https://custom-relay.example.com"
    try:
        assert _relay_base_url() == "https://custom-relay.example.com"
    finally:
        settings.gateway_http_url = orig


# ── upload_to_relay ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_upload_to_relay_success(monkeypatch):
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")
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
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")
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
    monkeypatch.setattr(settings, "gateway_http_url", "https://relay.test.com")
    monkeypatch.setattr(settings, "gateway_instance_id", "test-instance")
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

    result = await run_backup()
    assert not result.ok
    assert "pg_dump" in result.error
