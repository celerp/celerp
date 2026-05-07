# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Persistent session tracker used by the concurrent connection policy.

Records the most recent authenticated request timestamp per (company_id, user_id).
State is written to a JSON file next to config.toml so it survives process reloads
(e.g. uvicorn --reload in dev mode) and process restarts.

File format: {"<company_id>|<user_id>": <unix_timestamp_float>, ...}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_WINDOW_SECONDS: int = 15 * 60  # match access token TTL

# In-memory mirror - kept in sync with the file on every write.
_activity: dict[tuple[str, str], float] = {}
_loaded: bool = False


def _sessions_path() -> Path | None:
    """Return path to sessions file next to config.toml, or None if unavailable."""
    try:
        from celerp.config import config_path
        return config_path().parent / ".active_sessions.json"
    except Exception:
        return None


def _load() -> None:
    """Load from file into _activity (called once at first use)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _sessions_path()
    if path is None or not path.exists():
        return
    try:
        data: dict[str, float] = json.loads(path.read_text())
        now = time.time()
        for key, ts in data.items():
            if now - ts < _WINDOW_SECONDS:
                parts = key.split("|", 1)
                if len(parts) == 2:
                    _activity[(parts[0], parts[1])] = ts
    except Exception:
        pass


def _save() -> None:
    """Persist current _activity to file (best-effort)."""
    path = _sessions_path()
    if path is None:
        return
    try:
        data = {f"{cid}|{uid}": ts for (cid, uid), ts in _activity.items()}
        path.write_text(json.dumps(data))
    except Exception:
        pass


_SAVE_INTERVAL: float = 30.0  # write to disk at most once per 30s per session
_last_save: float = 0.0


def record(user_id: str, company_id: str = "") -> None:
    """Call on every authenticated API request."""
    global _last_save
    _load()
    _activity[(company_id, user_id)] = time.time()
    now = time.time()
    if now - _last_save >= _SAVE_INTERVAL:
        _last_save = now
        _save()


def active_user_ids(*, company_id: str | None = None) -> set[str]:
    """Return user_ids seen within the activity window.

    If company_id is given, returns only users for that company.
    If omitted, returns all active user_ids regardless of company.
    """
    _load()
    cutoff = time.time() - _WINDOW_SECONDS
    return {
        uid for (cid, uid), ts in _activity.items()
        if ts >= cutoff
        and (company_id is None or cid == company_id)
    }


def evict(user_id: str) -> None:
    """Remove a user from the tracker (used by force-login)."""
    _load()
    keys = [k for k in _activity if k[1] == user_id]
    for k in keys:
        del _activity[k]
    _save()


def clear() -> None:
    """Wipe all activity (used by force-login to evict all sessions)."""
    global _loaded, _last_save
    _activity.clear()
    _loaded = True  # no need to re-load after explicit clear
    _last_save = 0.0  # force save on next record() so cleared state is persisted
    _save()
