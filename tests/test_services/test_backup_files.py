# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/services/backup_files.py.

Covers:
  - build_manifest: correct entries for test directory
  - diff_manifests: new file, size change, mtime+hash, skip-if-unchanged
  - build_file_archive: contains manifest + changed files, returns None if empty
  - load_manifest / save_manifest roundtrip
"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest

from celerp.services.backup_files import (
    ManifestEntry,
    build_file_archive,
    build_manifest,
    diff_manifests,
    load_manifest,
    save_manifest,
)


@pytest.fixture
def tmp_dir(tmp_path):
    """Create a temp directory with some test files."""
    d = tmp_path / "attachments"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    (d / "b.txt").write_text("world")
    sub = d / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested")
    return d


# ── build_manifest ────────────────────────────────────────────────────────────

def test_build_manifest(tmp_dir):
    entries = build_manifest([tmp_dir])
    assert len(entries) == 3
    paths = {e.path for e in entries}
    assert str(tmp_dir / "a.txt") in paths
    assert str(tmp_dir / "b.txt") in paths
    assert str(tmp_dir / "sub" / "c.txt") in paths
    for e in entries:
        assert e.size > 0
        assert len(e.sha256) == 64
        assert e.mtime > 0


def test_build_manifest_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    entries = build_manifest([d])
    assert entries == []


def test_build_manifest_missing_dir(tmp_path):
    entries = build_manifest([tmp_path / "nonexistent"])
    assert entries == []


# ── diff_manifests ────────────────────────────────────────────────────────────

def test_diff_new_file():
    current = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)]
    previous: list[ManifestEntry] = []
    changed = diff_manifests(current, previous)
    assert len(changed) == 1
    assert changed[0] == Path("/a.txt")


def test_diff_size_changed():
    prev = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)]
    curr = [ManifestEntry(path="/a.txt", sha256="def", size=10, mtime=2.0)]
    changed = diff_manifests(curr, prev)
    assert len(changed) == 1


def test_diff_mtime_changed_hash_different():
    prev = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)]
    curr = [ManifestEntry(path="/a.txt", sha256="def", size=5, mtime=2.0)]
    changed = diff_manifests(curr, prev)
    assert len(changed) == 1


def test_diff_mtime_changed_hash_same():
    """mtime changed but hash same → not included (false positive from FS)."""
    prev = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)]
    curr = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=2.0)]
    changed = diff_manifests(curr, prev)
    assert len(changed) == 0


def test_diff_unchanged():
    entry = ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)
    changed = diff_manifests([entry], [entry])
    assert len(changed) == 0


def test_diff_deleted_file_not_in_changed():
    """Deleted file not in current manifest → not in changed list."""
    prev = [ManifestEntry(path="/a.txt", sha256="abc", size=5, mtime=1.0)]
    curr: list[ManifestEntry] = []
    changed = diff_manifests(curr, prev)
    assert len(changed) == 0


# ── build_file_archive ────────────────────────────────────────────────────────

def test_build_file_archive_skip_if_clean(tmp_dir):
    manifest = build_manifest([tmp_dir])
    result = build_file_archive([], manifest, [tmp_dir])
    assert result is None


def test_build_file_archive_contains_files(tmp_dir):
    manifest = build_manifest([tmp_dir])
    changed = [Path(e.path) for e in manifest[:1]]
    archive = build_file_archive(changed, manifest, [tmp_dir])
    assert archive is not None

    # Verify contents
    import io
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert len(names) >= 2  # manifest + at least one file


# ── load_manifest / save_manifest ─────────────────────────────────────────────

def test_manifest_roundtrip(tmp_path):
    entries = [
        ManifestEntry(path="/a.txt", sha256="abc123", size=100, mtime=1234567890.0),
        ManifestEntry(path="/b.txt", sha256="def456", size=200, mtime=1234567891.0),
    ]
    path = tmp_path / "manifest.json"
    save_manifest(entries, path)
    loaded = load_manifest(path)
    assert len(loaded) == 2
    assert loaded[0].path == "/a.txt"
    assert loaded[1].sha256 == "def456"


def test_load_manifest_missing(tmp_path):
    entries = load_manifest(tmp_path / "nonexistent.json")
    assert entries == []


def test_load_manifest_corrupt(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not valid json{{{")
    entries = load_manifest(path)
    assert entries == []
