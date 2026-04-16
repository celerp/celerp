# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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
