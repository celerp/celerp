# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/cleanup.py"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest

from celerp.ai.cleanup import MAX_AGE_DAYS, cleanup_uploads
from celerp.config import settings


@pytest.fixture
def upload_dir(tmp_path):
    """Create a temporary upload directory and point settings at it."""
    ud = tmp_path / "ai_uploads"
    ud.mkdir()
    with patch.object(settings, "data_dir", tmp_path):
        yield ud


def _create_file(upload_dir: Path, file_id: str, age_days: float) -> None:
    """Create a test .bin + .meta file pair with a specific age."""
    bin_path = upload_dir / f"{file_id}.bin"
    meta_path = upload_dir / f"{file_id}.meta"
    bin_path.write_bytes(b"test data")
    meta_path.write_text(json.dumps({
        "filename": "test.jpg",
        "content_type": "image/jpeg",
        "company_id": "test-co",
    }))
    past = time.time() - (age_days * 86400)
    os.utime(bin_path, (past, past))
    os.utime(meta_path, (past, past))


def test_cleanup_old_unconditional(upload_dir):
    """Files >30 days deleted."""
    _create_file(upload_dir, "ai_up_old", 35)
    deleted = cleanup_uploads()
    assert deleted == 1
    assert not (upload_dir / "ai_up_old.bin").exists()
    assert not (upload_dir / "ai_up_old.meta").exists()


def test_cleanup_keeps_recent(upload_dir):
    """Files <30 days kept."""
    _create_file(upload_dir, "ai_up_new", 3)
    deleted = cleanup_uploads()
    assert deleted == 0
    assert (upload_dir / "ai_up_new.bin").exists()


def test_cleanup_keeps_mid_age(upload_dir):
    """Files between 7-30 days kept (no longer deleted as unreferenced)."""
    _create_file(upload_dir, "ai_up_mid", 15)
    deleted = cleanup_uploads()
    assert deleted == 0
    assert (upload_dir / "ai_up_mid.bin").exists()


def test_cleanup_handles_empty_dir(upload_dir):
    """No files, no error."""
    deleted = cleanup_uploads()
    assert deleted == 0


def test_cleanup_handles_missing_dir(tmp_path):
    """Upload dir doesn't exist yet."""
    with patch.object(settings, "data_dir", tmp_path / "nonexistent"):
        deleted = cleanup_uploads()
    assert deleted == 0


def test_cleanup_deletes_both_bin_and_meta(upload_dir):
    """Both .bin and .meta removed."""
    _create_file(upload_dir, "ai_up_pair", 35)
    cleanup_uploads()
    assert not (upload_dir / "ai_up_pair.bin").exists()
    assert not (upload_dir / "ai_up_pair.meta").exists()


def test_cleanup_orphaned_bin(upload_dir):
    """A .bin without .meta gets cleaned up."""
    orphan = upload_dir / "ai_up_orphan.bin"
    orphan.write_bytes(b"orphan")
    deleted = cleanup_uploads()
    assert deleted == 1
    assert not orphan.exists()
