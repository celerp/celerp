# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Atomic reads and writes for Electron's packaged celerp-config.json.

A single writer that merges any number of top-level keys into the packaged
config in one atomic operation, so a save that touches several related keys
(a database URL plus its backup, a storage backend plus its credentials)
never leaves the file with only some of them applied. Callers outside this
module never open or replace celerp-config.json directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

log = logging.getLogger(__name__)

# Cross-process lock protocol for celerp-config.json, identical on both writers
# (this module and electron/config-writer.js): the lock is an O_EXCL-created
# sidecar file `celerp-config.json.lock`. A writer acquires by exclusive create,
# retrying every _LOCK_RETRY_S for up to _LOCK_BUDGET_S; a lock whose mtime has
# aged past _LOCK_STALE_S is presumed abandoned and unlinked, after which the
# exclusive create stays the only way to win so exactly one writer is elected.
# The hold is well under a second, so the stale window is generous. The lock's
# contents (pid + timestamp) are diagnostics only and never parsed for control.
_LOCK_BUDGET_S = 5.0
_LOCK_RETRY_S = 0.05
_LOCK_STALE_S = 10.0


def _acquire_lock(lock_path: str):
    """Acquire the config lock by exclusive create, returning the open fd, or
    None when the budget expires while another writer holds it."""
    deadline = time.monotonic() + _LOCK_BUDGET_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
            except OSError:
                pass
            return fd
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_S:
                # Abandoned lock: remove it and loop back to the exclusive
                # create, which stays the only way to win the takeover race.
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(_LOCK_RETRY_S)


def _release_lock(fd: int, lock_path: str) -> None:
    """Close the lock fd and unlink the lock file, ignoring an already-gone
    lock (a stale takeover by another writer)."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def merge_packaged_config(updates: dict) -> bool:
    """Merge every key in `updates` into Electron's celerp-config.json in one
    atomic write, forcing mode 0600 so the co-resident secrets (external_db_url,
    S3 keys) are never broadened. Returns True when the updates were persisted,
    False when they could not be (no packaged data dir, the lock could not be
    acquired within its budget, or a write error).

    A no-op returning False in dev/server mode where CELERP_DATA_DIR is unset.
    The whole read-merge-write runs under the cross-process lock so a concurrent
    Electron writer never races: the existing config is re-read inside the lock,
    every key is merged, the result is written to a unique temp file created
    0600, fsync'd, then os.replace swaps it in (os.replace adopts the temp inode,
    so the target's mode becomes 0600 regardless of the prior mode or the process
    umask). Any failure logs the keys and exception (never the values), removes
    the temp file, and leaves the prior config on disk untouched, so a multi-key
    save either lands in full or not at all. The lock is always released.
    """
    if not updates:
        return True
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if not data_dir:
        return False
    config_path = os.path.join(data_dir, "celerp-config.json")
    lock_path = f"{config_path}.lock"
    lock_fd = _acquire_lock(lock_path)
    if lock_fd is None:
        log.warning("Config: could not acquire lock for %s within %ss; not persisted.",
                    sorted(updates.keys()), _LOCK_BUDGET_S)
        return False
    tmp_path = f"{config_path}.{uuid.uuid4().hex}.tmp"
    try:
        existing: dict = {}
        if os.path.exists(config_path):
            with open(config_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        existing.update(updates)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
        log.debug("Config: %s persisted.", sorted(updates.keys()))
        return True
    except Exception as exc:
        log.warning("Config: failed to persist %s: %s", sorted(updates.keys()), exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False
    finally:
        _release_lock(lock_fd, lock_path)


def read_packaged_config() -> dict:
    """Return the packaged celerp-config.json as a dict, or {} when it is
    absent, unreadable, or not an object."""
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if not data_dir:
        return {}
    config_path = os.path.join(data_dir, "celerp-config.json")
    try:
        with open(config_path) as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}
