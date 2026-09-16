# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the packaged-config atomic writer.

merge_packaged_config replaces several call sites that used to merge one key
at a time (settings_cloud.py's _save_infra_packaged and _restore_db_packaged),
which could leave the file with only some of a related group of keys applied
if a crash landed between two separate single-key writes. These tests cover
the multi-key case directly, plus the no-op and failure-preserves-prior
behavior the single-key writer already had.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import threading
import time

import pytest

from celerp import config_store


def test_merge_multiple_keys_atomically(tmp_path, monkeypatch):
    """A single call merging several keys lands all of them together."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))

    ok = config_store.merge_packaged_config({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://u:p@h:5432/db",
        "external_db_url_backup": "postgresql+asyncpg://u:old@h:5432/db",
    })

    assert ok is True
    persisted = json.loads(config_path.read_text())
    assert persisted["db_mode"] == "external"
    assert persisted["external_db_url"] == "postgresql+asyncpg://u:p@h:5432/db"
    assert persisted["external_db_url_backup"] == "postgresql+asyncpg://u:old@h:5432/db"


def test_merge_no_data_dir_is_noop():
    """With no CELERP_DATA_DIR (dev/server mode), the merge is a no-op that
    reports failure rather than writing anywhere."""
    os.environ.pop("CELERP_DATA_DIR", None)
    assert config_store.merge_packaged_config({"db_mode": "external"}) is False


def test_merge_preserves_0600(tmp_path, monkeypatch):
    """A multi-key merge leaves the config file at mode 0600."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"a": 1}))
    config_path.chmod(0o600)

    config_store.merge_packaged_config({"b": 2, "c": 3})

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"config mode broadened to {oct(mode)}"


def test_merge_failure_preserves_prior_file(tmp_path, monkeypatch):
    """A failure partway through a multi-key merge leaves the prior config
    intact rather than partially applied or truncated."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    prior = {"db_mode": "local", "external_db_url": ""}
    config_path.write_text(json.dumps(prior))

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(config_store.json, "dump", _boom)

    ok = config_store.merge_packaged_config({
        "db_mode": "external",
        "external_db_url": "postgresql+asyncpg://u:p@h:5432/db",
    })

    assert ok is False
    reread = json.loads(config_path.read_text())
    assert reread == prior


def test_merge_empty_updates_is_noop_success(tmp_path, monkeypatch):
    """Merging an empty dict is a successful no-op: nothing to apply, nothing
    to fail on."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    assert config_store.merge_packaged_config({}) is True


def test_read_packaged_config_missing_returns_empty(tmp_path, monkeypatch):
    """Reading with no config file present degrades to {} rather than raising."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    assert config_store.read_packaged_config() == {}


def test_read_packaged_config_roundtrip(tmp_path, monkeypatch):
    """A value written by merge_packaged_config is visible to
    read_packaged_config."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_store.merge_packaged_config({"db_mode": "external"})
    assert config_store.read_packaged_config()["db_mode"] == "external"


def test_locked_writer_waits_on_held_lock(tmp_path, monkeypatch):
    """A fresh lock file blocks the writer until its budget expires, and the
    prior config is left intact; a lock whose mtime has aged past the stale
    threshold is taken over and the write proceeds, releasing the lock in the
    finally path."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config_store, "_LOCK_BUDGET_S", 0.5)
    monkeypatch.setattr(config_store, "_LOCK_RETRY_S", 0.05)
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))
    lock_path = tmp_path / "celerp-config.json.lock"

    # A fresh lock held by another writer: the merge must wait out its budget,
    # fail, and leave the prior config untouched.
    lock_path.write_text("owner")
    t0 = time.monotonic()
    ok = config_store.merge_packaged_config({"a": 1})
    waited = time.monotonic() - t0
    assert ok is False
    assert waited >= 0.4, f"did not wait for the lock (waited {waited:.2f}s)"
    assert json.loads(config_path.read_text()) == {"db_mode": "local"}

    # Age the lock past the stale threshold: it is taken over via O_EXCL and the
    # write lands.
    stale = time.time() - (config_store._LOCK_STALE_S + 20)
    os.utime(lock_path, (stale, stale))
    ok2 = config_store.merge_packaged_config({"a": 1})
    assert ok2 is True
    assert json.loads(config_path.read_text())["a"] == 1
    assert not lock_path.exists(), "lock not released after a successful write"


def test_config_lock_stale_takeover_single_writer(tmp_path, monkeypatch):
    """A superseded owner's release must not delete the successor's lock.

    Owner A acquires the lock, the lock ages past the stale threshold, owner B
    takes it over (its own token now on disk). When the superseded owner A then
    releases, its unlink is token-verified and must be a no-op, so B's live lock
    survives and exactly one writer holds it. Without owner-token verification
    A's unconditional unlink would delete B's lock and admit a second writer."""
    lock_path = str(tmp_path / "celerp-config.json.lock")

    # Owner A acquires and writes its token.
    fd_a, token_a = config_store._acquire_lock(lock_path)
    assert fd_a is not None
    assert pathlib.Path(lock_path).read_bytes() == token_a

    # Age the lock so a takeover is permitted, then owner B takes it over. B's
    # acquire re-creates the lock with B's own token via the O_EXCL path.
    stale = time.time() - (config_store._LOCK_STALE_S + 20)
    os.utime(lock_path, (stale, stale))
    fd_b, token_b = config_store._acquire_lock(lock_path)
    assert fd_b is not None
    assert token_b != token_a, "takeover did not write a distinct owner token"
    assert pathlib.Path(lock_path).read_bytes() == token_b

    # The superseded owner A releases. Its unlink must be a no-op because the
    # lock no longer holds A's token; B's live lock survives.
    config_store._release_lock(fd_a, lock_path, token_a)
    assert pathlib.Path(lock_path).exists(), "superseded release deleted the successor's lock"
    assert pathlib.Path(lock_path).read_bytes() == token_b, "successor's lock was clobbered"

    # B's own release removes its lock cleanly.
    config_store._release_lock(fd_b, lock_path, token_b)
    assert not pathlib.Path(lock_path).exists(), "owner B's release did not remove its lock"


def test_config_lock_release_only_unlinks_own_token(tmp_path, monkeypatch):
    """A release must unlink only while the on-disk lock still carries this
    owner's token; a lock replaced by a different token is left alone."""
    lock_path = str(tmp_path / "celerp-config.json.lock")
    fd, token = config_store._acquire_lock(lock_path)
    assert fd is not None

    # Simulate another writer having replaced the lock file after this owner
    # acquired it: the on-disk token no longer matches ours.
    pathlib.Path(lock_path).write_bytes(b"different-owner-token")
    config_store._release_lock(fd, lock_path, token)
    assert pathlib.Path(lock_path).exists(), "release unlinked a lock it no longer owned"
    assert pathlib.Path(lock_path).read_bytes() == b"different-owner-token"
    os.unlink(lock_path)


def test_config_dir_fsynced_after_rename(tmp_path, monkeypatch):
    """The containing directory is fsynced after the atomic rename so the
    rename itself is durable, not only the file contents. A directory-fsync
    OSError is swallowed and the write still reports success."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))

    real_fsync = os.fsync
    dir_fsync_fds: list[int] = []

    def _tracking_fsync(fd):
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                dir_fsync_fds.append(fd)
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(config_store.os, "fsync", _tracking_fsync)
    ok = config_store.merge_packaged_config({"db_mode": "external"})
    assert ok is True
    assert dir_fsync_fds, "the containing directory was not fsynced after the rename"


def test_config_dir_fsync_oserror_is_swallowed(tmp_path, monkeypatch):
    """A directory-fsync failure (a filesystem or platform that rejects it) does
    not fail the write: the rename already committed the update."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))

    real_fsync = os.fsync

    def _fsync_dir_raises(fd):
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory fsync not supported")
        except OSError as exc:
            if "not supported" in str(exc):
                raise
        return real_fsync(fd)

    monkeypatch.setattr(config_store.os, "fsync", _fsync_dir_raises)
    ok = config_store.merge_packaged_config({"db_mode": "external"})
    assert ok is True, "a directory-fsync OSError must not fail the write"
    assert json.loads(config_path.read_text())["db_mode"] == "external"


def test_concurrent_node_python_writers(tmp_path, monkeypatch):
    """N Python writers (merge_packaged_config) and N Node writers
    (electron/config-writer.js CLI) each merge their own key against one config
    file behind a start barrier. The shared lock serializes them, so the final
    file is valid JSON containing every worker's key and the original: no lost
    update, no partial write. Any nonzero worker exit fails the test."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime not available for the cross-process writer test")
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    cli = repo_root / "electron" / "config-writer.js"
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    config_path = tmp_path / "celerp-config.json"
    config_path.write_text(json.dumps({"db_mode": "local"}))

    n = 6
    barrier = threading.Barrier(2 * n)
    errors: list[str] = []
    env = {**os.environ, "CELERP_DATA_DIR": str(tmp_path)}

    def py_worker(i):
        barrier.wait()
        if not config_store.merge_packaged_config({f"py_{i}": i}):
            errors.append(f"py_{i} merge returned False")

    def node_worker(i):
        barrier.wait()
        proc = subprocess.run(
            [node, str(cli), str(config_path), f"node_{i}", str(i)],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            errors.append(f"node_{i} exit {proc.returncode}: {proc.stderr.strip()}")

    threads = [threading.Thread(target=py_worker, args=(i,)) for i in range(n)]
    threads += [threading.Thread(target=node_worker, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    assert not errors, errors
    final = json.loads(config_path.read_text())  # valid JSON -> no partial write
    for i in range(n):
        assert final[f"py_{i}"] == i, f"lost py_{i}"
        assert final[f"node_{i}"] == i, f"lost node_{i}"
    assert final["db_mode"] == "local", "original key clobbered"
