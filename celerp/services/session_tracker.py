# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""In-process activity tracker used by the concurrent connection policy.

Records the most recent authenticated request timestamp per (company_id, user_id).
Intentionally simple: no persistence, resets on process restart.
"""
from __future__ import annotations

import time

_WINDOW_SECONDS: int = 15 * 60  # match access token TTL
_CONCURRENT_WINDOW_SECONDS: int = 2 * 60  # window for "actively concurrent" gate check

# (company_id, user_id) -> last_seen monotonic timestamp
_activity: dict[tuple[str, str], float] = {}


def record(user_id: str, company_id: str = "") -> None:
    """Call on every authenticated API request."""
    _activity[(company_id, user_id)] = time.monotonic()


def active_user_ids(*, company_id: str | None = None, exclude: str | None = None) -> set[str]:
    """Return user_ids seen within the activity window.

    If company_id is given, returns only users for that company.
    If omitted, returns all active user_ids regardless of company.
    """
    cutoff = time.monotonic() - _WINDOW_SECONDS
    return {
        uid for (cid, uid), ts in _activity.items()
        if ts >= cutoff
        and (company_id is None or cid == company_id)
        and uid != exclude
    }


def evict(user_id: str) -> None:
    """Remove a user from the tracker (used by force-login)."""
    keys = [k for k in _activity if k[1] == user_id]
    for k in keys:
        del _activity[k]


def clear() -> None:
    """Wipe all activity (used by force-login to evict all sessions)."""
    _activity.clear()
