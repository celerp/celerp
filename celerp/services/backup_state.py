# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""In-process 'a backup is building' flag — pause writes, keep reads flowing.

A backup represents a clean point in time, so while one is in flight we pause user
*writes* (every state change funnels through ``emit_event``, which consults this flag
and returns 503) but leave *reads* untouched. Set/cleared by ``writes_paused()`` around
snapshot creation, in a try/finally so a failed backup never leaves writes locked.
"""

from __future__ import annotations

import contextlib

_active = False


def is_active() -> bool:
    return _active


@contextlib.contextmanager
def writes_paused():
    """Mark a backup in progress for its duration (writes paused, reads allowed)."""
    global _active
    _active = True
    try:
        yield
    finally:
        _active = False
