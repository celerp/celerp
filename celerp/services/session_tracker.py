# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Session registry for the concurrent connection policy.

Tracks active JWTs by JTI (JWT ID) so the gate is based on whether a token is
still valid, not on recent API activity.  This closes the 15-minute idle window
where a user could log in without seeing the 409 gate.

File format: {"<jti>": {"user_id": "<uid>", "expiry": <unix_float>}, ...,
              "__nonce__": "<uuid4>"}

Session nonce
-------------
A per-tracker nonce (UUID4) is rotated on every invalidate_sessions().  Access
tokens embed it at issuance.  get_current_user() validates the claim so any
token minted before the last logout/force-login is immediately rejected (401).

Eviction IP
-----------
When force-login evicts active sessions the requesting IP is stored in
_evicted_by_ip.  get_current_user() calls pop_evicted_by_ip() and includes the
IP in the 401 detail so the login page can show a meaningful message.
"""
from __future__ import annotations

import json
import time
import uuid as _uuid_mod
from pathlib import Path

# In-memory registry: jti -> {"user_id": str, "expiry": float}
_sessions: dict[str, dict] = {}
_loaded: bool = False
_nonce: str = str(_uuid_mod.uuid4())
_evicted_by_ip: str | None = None

_NONCE_KEY = "__nonce__"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sessions_path() -> Path | None:
    try:
        from celerp.config import config_path
        return config_path().parent / ".active_sessions.json"
    except Exception:
        return None


def _load() -> None:
    global _loaded, _nonce
    if _loaded:
        return
    _loaded = True
    path = _sessions_path()
    if path is None or not path.exists():
        return
    try:
        data: dict = json.loads(path.read_text())
        saved_nonce = data.pop(_NONCE_KEY, None)
        if saved_nonce:
            _nonce = saved_nonce
        now = time.time()
        for jti, entry in data.items():
            if isinstance(entry, dict) and entry.get("expiry", 0) > now:
                _sessions[jti] = entry
    except Exception:
        pass


def _save() -> None:
    path = _sessions_path()
    if path is None:
        return
    try:
        data: dict = {_NONCE_KEY: _nonce}
        data.update(_sessions)
        path.write_text(json.dumps(data))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_token(jti: str, user_id: str, expiry: float) -> None:
    """Record a newly-issued access token.  Call once per login / token refresh."""
    _load()
    _sessions[jti] = {"user_id": user_id, "expiry": expiry}
    _save()


def active_user_ids() -> set[str]:
    """Return user_ids with at least one non-expired JTI registered."""
    _load()
    now = time.time()
    return {entry["user_id"] for entry in _sessions.values() if entry["expiry"] > now}


def get_nonce() -> str:
    """Return the current session nonce.  Embedded in access tokens at issuance."""
    _load()
    return _nonce


def invalidate_sessions(evicting_ip: str | None = None) -> None:
    """Wipe all registered tokens AND rotate the nonce.

    Called by logout and force-login.  After this call every existing access
    token is immediately rejected (401) because the embedded snonce no longer
    matches, regardless of whether its expiry has elapsed.

    evicting_ip: if provided (force-login), stored so the evicted user can see
    who triggered their logout on the next 401 redirect.
    """
    global _loaded, _nonce, _evicted_by_ip
    _sessions.clear()
    _nonce = str(_uuid_mod.uuid4())
    _loaded = True
    _evicted_by_ip = evicting_ip
    _save()


def pop_evicted_by_ip() -> str | None:
    """Return and clear the stored eviction IP (one-shot consumption)."""
    global _evicted_by_ip
    ip = _evicted_by_ip
    _evicted_by_ip = None
    return ip


def clear() -> None:
    """Wipe all tokens WITHOUT rotating the nonce.

    Safe for test gate-bypass: existing tokens remain valid after this call.
    Do NOT use in production logout / force-login paths.
    """
    global _loaded, _evicted_by_ip
    _sessions.clear()
    _evicted_by_ip = None
    _loaded = True
    _save()
