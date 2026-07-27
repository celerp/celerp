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


async def fetch_relay_bearer(http_client) -> str:
    """Exchange the instance API key (gateway_token) for a short-lived relay
    bearer JWT via POST /auth/token.

    Single source of the relay auth handshake every relay REST call needs.
    Callers pass their own httpx client so they own the timeout and connection
    lifecycle, and reuse it for the follow-up request. Raises RuntimeError on a
    non-200 so each caller degrades in one place.
    """
    from celerp.config import settings
    resp = await http_client.post(
        f"{relay_http_url()}/auth/token", json={"api_key": settings.gateway_token})
    if resp.status_code != 200:
        raise RuntimeError(f"relay auth failed ({resp.status_code})")
    return resp.json()["access_token"]


def activate_payload(instance_id: str, *, first_boot: bool | None = None) -> dict:
    """Build the /auth/activate request body.

    Single source of truth for every activation call site (startup probe,
    Cloud settings, claim-by-email), so the relay always learns version and
    platform. first_boot is only known by the startup probe.
    """
    import platform as _platform

    from celerp import __version__

    payload = {
        "instance_id": instance_id,
        "version": __version__,
        "platform": _platform.system(),
    }
    if first_boot is not None:
        payload["first_boot"] = first_boot
    return payload


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


HANDOFF_BASE = "https://celerp.com"


def build_handoff_url(path: str, *, medium: str = "inapp", lead: str = "", extra: str = "", anchor: str = "") -> str:
    """Single source of truth for celerp.com handoff links (subscribe, github, ...).

    Keeps the format identical everywhere: an optional ``lead`` param first (e.g.
    ``instance_id=...``, so attribution tags never bury it), then the UTM tags
    (``utm_source=app`` + the caller's ``medium``), then any ``extra`` params, then
    an optional ``#anchor``.
    """
    params = ([lead] if lead else []) + [f"utm_source=app&utm_medium={medium}"]
    if extra:
        params.append(extra.lstrip("?&"))
    url = f"{HANDOFF_BASE}{path}?{'&'.join(params)}"
    return f"{url}#{anchor}" if anchor else url


def build_subscribe_url(instance_id: str = "", anchor: str = "", *, topup: bool = False, extra: str = "") -> str:
    """In-app celerp.com/subscribe handoff URL. Thin caller of build_handoff_url.

    ``topup=True`` selects the /subscribe/topup variant. Callers resolve the instance
    id however they need (the gateway id, ``ensure_instance_id()``, or a payload value).
    """
    path = "/subscribe/topup" if topup else "/subscribe"
    lead = f"instance_id={instance_id}" if instance_id else ""
    return build_handoff_url(path, medium="inapp", lead=lead, extra=extra, anchor=anchor)


def relay_subscribe_url(anchor: str = "") -> str:
    """Subscribe URL pre-filled with the connected gateway instance id."""
    from celerp.config import settings
    return build_subscribe_url(_instance_id or settings.gateway_instance_id, anchor)

