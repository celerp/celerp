# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Country packs: each country's currency and tax rates, shipped inside the release.

The pack root holds ``packs.lock.json`` (country code to version, schema version and sha256)
and ``packs/<CC>.json``. A pack is read only when its bytes match the lock's digest; an absent
or changed pack is skipped and logged, never fetched. The root is ``CELERP_L10N_DIR`` when set,
else this package's directory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


def _root() -> Path:
    return Path(os.environ.get("CELERP_L10N_DIR") or Path(__file__).resolve().parent)


def _lock() -> dict | None:
    path = _root() / "packs.lock.json"
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(lock, dict):
            raise ValueError("not an object")
        return lock
    except (OSError, ValueError) as exc:
        log.error("Country pack lock %s is unreadable: %s", path, exc)
        return None


@lru_cache(maxsize=1)
def lock_codes() -> tuple[str, ...] | None:
    """The lock's country codes in order, or None when the lock is missing or unreadable."""
    lock = _lock()
    return None if lock is None else tuple(lock)


@lru_cache(maxsize=1)
def packs() -> dict[str, dict]:
    """``{code: {"currency", "taxes"}}`` for every lock entry whose pack matches its digest."""
    loaded: dict[str, dict] = {}
    for code, entry in (_lock() or {}).items():
        path = _root() / "packs" / f"{code}.json"
        try:
            data = path.read_bytes()
        except OSError:
            log.error("Country pack %s is missing", path.name)
            continue
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            log.error("Country pack %s does not match its digest in the lock", path.name)
            continue
        pack = json.loads(data)
        loaded[code] = {"currency": pack["company_defaults"]["currency"], "taxes": pack["taxes"]}
    return loaded


def missing() -> tuple[str, ...]:
    """Lock codes with no loaded pack, in lock order; () when the lock is unreadable."""
    loaded = packs()
    return tuple(code for code in lock_codes() or () if code not in loaded)
