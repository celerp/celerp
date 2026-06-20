# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Gateway session-token state - internal only.

This module is in _PROTECTED_BSL_INTERNALS. Third-party modules MUST NOT import it.
The session token is issued by relay.celerp.com after the hello_ack handshake
and is required for cloud-gated endpoints (/ai/*, /backup/*, /connectors/*).
"""
from __future__ import annotations

_session_token: str = ""
_subscription_tier: str = ""
_subscription_status: str = ""
_feature_flags: dict = {}
_instance_id: str = ""


def get_instance_id() -> str:
    """Return the relay-canonical instance_id (empty string if not connected)."""
    return _instance_id


def set_instance_id(iid: str) -> None:
    """Set the canonical instance_id. Called only by GatewayClient on hello_ack."""
    global _instance_id
    _instance_id = iid


def get_session_token() -> str:
    """Return the current live session token (empty string if not connected)."""
    return _session_token


def set_session_token(token: str) -> None:
    """Set the current session token. Called only by GatewayClient."""
    global _session_token
    _session_token = token


def set_subscription_state(tier: str, status: str) -> None:
    """Update local subscription state from gateway WS push."""
    global _subscription_tier, _subscription_status
    _subscription_tier = tier
    _subscription_status = status


def get_subscription_state() -> tuple[str, str]:
    """Return (tier, status) of the current subscription."""
    return _subscription_tier, _subscription_status


def set_feature_flags(flags: dict) -> None:
    """Store feature flags received from gateway. Called by GatewayClient."""
    global _feature_flags
    _feature_flags = dict(flags)


def get_feature_flags() -> dict:
    """Return a copy of the current feature flags."""
    return dict(_feature_flags)


# ── Relay connection helpers (single source of truth) ────────────────────────
# All relay HTTP calls use these. Never inline these values elsewhere.

def relay_http_url() -> str:
    """Derive the relay HTTP base URL from gateway settings.

    Handles both explicit gateway_http_url config and WS-URL derivation.
    Single source of truth - used by backup, ai/quota, and any future module.
    """
    from celerp.config import settings
    if settings.gateway_http_url:
        return settings.gateway_http_url.rstrip("/")
    url = settings.gateway_url
    url = url.replace("wss://", "https://").replace("ws://", "http://")
    if "/ws/" in url:
        url = url.rsplit("/ws/", 1)[0]
    return url.rstrip("/")


def relay_session_headers() -> dict[str, str]:
    """Return X-Session-Token + X-Instance-ID headers for relay REST calls.

    Always uses the relay-canonical instance_id (set on hello_ack), with the
    config value as fallback. The relay keys its session table on the canonical
    id — using the config id directly causes 401 when they differ.
    """
    from celerp.config import settings
    return {
        "X-Session-Token": _session_token,
        "X-Instance-ID": _instance_id or settings.gateway_instance_id,
    }


def relay_subscribe_url(anchor: str = "") -> str:
    """Return the celerp.com subscribe URL with the current instance_id pre-filled."""
    from celerp.config import settings
    iid = _instance_id or settings.gateway_instance_id
    base = "https://celerp.com/subscribe"
    params = "utm_source=app&utm_medium=inapp"
    if iid:
        url = f"{base}?instance_id={iid}&{params}"
    else:
        url = f"{base}?{params}"
    return f"{url}#{anchor}" if anchor else url

