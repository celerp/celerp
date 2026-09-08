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
