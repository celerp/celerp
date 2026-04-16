# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for celerp.modules.license"""
from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from celerp.modules.license import (
    _OFFLINE_GRACE_SECONDS,
    _read_cache,
    _verify_remote,
    _write_cache,
    check_license,
    is_premium_path,
)


# ── is_premium_path ───────────────────────────────────────────────────────────

def test_is_premium_path_true(tmp_path):
    pkg = tmp_path / "premium_modules" / "celerp-warehousing"
    pkg.mkdir(parents=True)
    assert is_premium_path(pkg) is True


def test_is_premium_path_false(tmp_path):
    pkg = tmp_path / "default_modules" / "celerp-inventory"
    pkg.mkdir(parents=True)
    assert is_premium_path(pkg) is False


def test_is_premium_path_nested(tmp_path):
    """Works even when premium_modules is not the direct parent."""
    pkg = tmp_path / "some" / "premium_modules" / "deep" / "celerp-test"
    pkg.mkdir(parents=True)
    assert is_premium_path(pkg) is True


# ── cache helpers ─────────────────────────────────────────────────────────────

def test_write_and_read_cache_licensed(tmp_path):
    cache_file = tmp_path / "celerp-test.json"
    _write_cache(cache_file, licensed=True, status="active")
    result = _read_cache(cache_file, "celerp-test")
    assert result is True


def test_write_and_read_cache_not_licensed(tmp_path):
    cache_file = tmp_path / "celerp-test.json"
    _write_cache(cache_file, licensed=False, status="not_licensed")
    result = _read_cache(cache_file, "celerp-test")
    assert result is False


def test_read_cache_missing_file(tmp_path):
    """Returns False when no cache file exists."""
    cache_file = tmp_path / "nonexistent.json"
    assert _read_cache(cache_file, "celerp-test") is False


def test_read_cache_expired(tmp_path):
    """Returns False when cache is older than grace period."""
    cache_file = tmp_path / "celerp-test.json"
    old_ts = time.time() - _OFFLINE_GRACE_SECONDS - 1
    cache_file.write_text(json.dumps({"licensed": True, "status": "active", "cached_at": old_ts}))
    assert _read_cache(cache_file, "celerp-test") is False


def test_read_cache_corrupt(tmp_path):
    """Returns False for corrupt cache file."""
    cache_file = tmp_path / "celerp-test.json"
    cache_file.write_text("not json {{")
    assert _read_cache(cache_file, "celerp-test") is False


# ── _verify_remote ────────────────────────────────────────────────────────────

def _mock_urlopen(response_data: dict, status: int = 200):
    """Return a context manager that yields a fake HTTP response."""
    class _FakeResponse:
        def __init__(self): pass
        def read(self): return json.dumps(response_data).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    return _FakeResponse()


def test_verify_remote_licensed(tmp_path):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen({"licensed": True, "status": "active"})):
        licensed, status = _verify_remote("celerp-test", "https://relay.example.com", "jwt-token")
    assert licensed is True
    assert status == "active"


def test_verify_remote_not_licensed(tmp_path):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen({"licensed": False, "status": "not_licensed"})):
        licensed, status = _verify_remote("celerp-test", "https://relay.example.com", "jwt-token")
    assert licensed is False
    assert status == "not_licensed"


def test_verify_remote_401_raises_permission_error():
    http_err = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(PermissionError, match="HTTP 401"):
            _verify_remote("celerp-test", "https://relay.example.com", "bad-jwt")


def test_verify_remote_network_error_raises():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            _verify_remote("celerp-test", "https://relay.example.com", "jwt-token")


# ── check_license integration ─────────────────────────────────────────────────

def test_check_license_live_success(tmp_path):
    """Live verification succeeds and writes cache."""
    with patch("urllib.request.urlopen", return_value=_mock_urlopen({"licensed": True, "status": "active"})):
        result = check_license("celerp-test", "https://relay.example.com", "jwt", tmp_path)
    assert result is True
    cache_file = tmp_path / "license_cache" / "celerp-test.json"
    assert cache_file.exists()
    cached = json.loads(cache_file.read_text())
    assert cached["licensed"] is True


def test_check_license_live_not_licensed(tmp_path):
    """Live verification returns False and writes cache."""
    with patch("urllib.request.urlopen", return_value=_mock_urlopen({"licensed": False, "status": "not_licensed"})):
        result = check_license("celerp-test", "https://relay.example.com", "jwt", tmp_path)
    assert result is False


def test_check_license_offline_grace_uses_cache(tmp_path):
    """When relay is unreachable, uses fresh cache."""
    cache_file = tmp_path / "license_cache" / "celerp-test.json"
    cache_file.parent.mkdir(parents=True)
    _write_cache(cache_file, licensed=True, status="active")

    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        result = check_license("celerp-test", "https://relay.example.com", "jwt", tmp_path)
    assert result is True


def test_check_license_offline_expired_cache_denies(tmp_path):
    """When relay unreachable and cache expired, denies."""
    cache_file = tmp_path / "license_cache" / "celerp-test.json"
    cache_file.parent.mkdir(parents=True)
    old_ts = time.time() - _OFFLINE_GRACE_SECONDS - 1
    cache_file.write_text(json.dumps({"licensed": True, "status": "active", "cached_at": old_ts}))

    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        result = check_license("celerp-test", "https://relay.example.com", "jwt", tmp_path)
    assert result is False


def test_check_license_offline_no_cache_denies(tmp_path):
    """When relay unreachable and no cache, denies."""
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        result = check_license("celerp-test", "https://relay.example.com", "jwt", tmp_path)
    assert result is False
