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
import uuid

log = logging.getLogger(__name__)


def merge_packaged_config(updates: dict) -> bool:
    """Merge every key in `updates` into Electron's celerp-config.json in one
    atomic write, forcing mode 0600 so the co-resident secrets (external_db_url,
    S3 keys) are never broadened. Returns True when the updates were persisted,
    False when they could not be (no packaged data dir, or a write error).

    A no-op returning False in dev/server mode where CELERP_DATA_DIR is unset.
    A missing or non-dict existing config degrades to an empty object before
    the merge. The write goes to a unique temp file created 0600, then
    os.replace swaps it in: os.replace adopts the temp inode, so the target's
    mode becomes 0600 regardless of the prior mode or the process umask (0600
    has no group/other bits for umask to strip). Any failure logs the keys and
    exception (never the values), removes the temp file, and leaves the prior
    config on disk untouched, so a multi-key save either lands in full or not
    at all.
    """
    if not updates:
        return True
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if not data_dir:
        return False
    config_path = os.path.join(data_dir, "celerp-config.json")
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
