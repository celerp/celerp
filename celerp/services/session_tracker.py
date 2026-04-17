# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""In-process activity tracker used by the concurrent connection policy.

Records the most recent authenticated request timestamp per user_id.
Intentionally simple: no persistence, resets on process restart.
"""
from __future__ import annotations

import time

_WINDOW_SECONDS: int = 15 * 60  # match access token TTL

# user_id -> last_seen monotonic timestamp
_activity: dict[str, float] = {}


def record(user_id: str, **_: object) -> None:
    """Call on every authenticated API request."""
    _activity[user_id] = time.monotonic()


def active_user_ids(*, exclude: str | None = None, **_: object) -> set[str]:
    """Return user_ids seen within the activity window."""
    cutoff = time.monotonic() - _WINDOW_SECONDS
    return {uid for uid, ts in _activity.items() if ts >= cutoff and uid != exclude}


def evict(user_id: str) -> None:
    """Remove a user from the tracker (used by force-login)."""
    _activity.pop(user_id, None)


def clear() -> None:
    """Wipe all activity (used by force-login to evict all sessions)."""
    _activity.clear()
