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
# writing a unique owner token as the whole file body, retrying every
# _LOCK_RETRY_S for up to _LOCK_BUDGET_S; a lock whose mtime has aged past
# _LOCK_STALE_S is presumed abandoned. Both unlink sites are token-verified:
# a writer removes the lock only while the file still holds the token it wrote,
# so a stale-takeover cannot delete a faster successor's lock and a superseded
# owner's release becomes a no-op. Exactly one writer is elected. The token is
# read for control, so it must be written and re-read as the exact file body.
#
# Honest residual: a path-based read-then-unlink is not atomic, so a sub-operation
# TOCTOU window remains between the token re-read and the unlink. It is bounded by
# _LOCK_STALE_S (a takeover requires a >10s stall) and is far narrower than an
# unconditional unlink.
_LOCK_BUDGET_S = 5.0
_LOCK_RETRY_S = 0.05
_LOCK_STALE_S = 10.0


def _lock_token() -> bytes:
    """A unique owner token for one lock acquisition: pid plus a random suffix,
    so two acquisitions (even in the same process) never collide."""
    return f"{os.getpid()} {uuid.uuid4().hex}".encode()


def _read_lock_token(lock_path: str) -> bytes | None:
    """Return the current lock file body, or None when it cannot be read."""
    try:
        with open(lock_path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _acquire_lock(lock_path: str):
    """Acquire the config lock by exclusive create, returning (fd, token), or
    None when the budget expires while another writer holds it. The token is the
    exact bytes written into the lock; the caller passes it to _release_lock so
    only the electing writer ever removes this lock."""
    deadline = time.monotonic() + _LOCK_BUDGET_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            token = _lock_token()
            os.write(fd, token)
            os.fsync(fd)
            return fd, token
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_S:
                # Abandoned lock: re-read the token immediately before removing
                # and unlink only that same stale token, so a faster successor
                # that already replaced the lock is never deleted. Then loop
                # back to the exclusive create, which stays the only way to win.
                stale_token = _read_lock_token(lock_path)
                if stale_token is not None:
                    try:
                        if _read_lock_token(lock_path) == stale_token:
                            os.unlink(lock_path)
                    except OSError:
                        pass
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(_LOCK_RETRY_S)


def _release_lock(fd: int, lock_path: str, token: bytes) -> None:
    """Close the lock fd and unlink the lock file only while it still holds this
    owner's token; a lock a stale takeover already replaced is left untouched."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        if _read_lock_token(lock_path) == token:
            os.unlink(lock_path)
    except OSError:
        pass


def _fsync_dir(dir_path: str) -> None:
    """Fsync the directory so a preceding atomic rename is durable, not only the
    renamed file's contents. A crash after os.replace but before the directory
    entry reaches disk could otherwise lose the rename. No-op on platforms that
    cannot fsync a directory fd (Windows), and any OSError is swallowed because
    the rename itself already committed the write."""
    if os.name != "posix":
        return
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(dir_fd)
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
    acquired = _acquire_lock(lock_path)
    if acquired is None:
        log.warning("Config: could not acquire lock for %s within %ss; not persisted.",
                    sorted(updates.keys()), _LOCK_BUDGET_S)
        return False
    lock_fd, lock_token = acquired
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
        _fsync_dir(data_dir)
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
        _release_lock(lock_fd, lock_path, lock_token)


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
