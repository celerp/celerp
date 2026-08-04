# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

from celerp.config import settings as _settings

API_BASE = os.getenv("API_URL", os.getenv("CELERP_API_URL", "http://localhost:8000"))
RELAY_URL = os.getenv("CELERP_RELAY_URL", "https://relay.celerp.com")
PRIVACY_POLICY_URL = "https://relay.celerp.com/privacy"
COOKIE_NAME = "celerp_token"
REFRESH_COOKIE_NAME = "celerp_refresh"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def get_token(request) -> str | None:
    """Extract the auth token from a request's cookies. DRY helper used by all UI routes."""
    return request.cookies.get(COOKIE_NAME)


def cookie_domain(request) -> str | None:
    """Return the cookie domain to use for set_cookie calls.

    Returns None for local development hosts (no domain restriction needed).
    Returns the hostname for external/cloud URLs so cookies are correctly
    scoped when the app is accessed via a Cloudflare tunnel subdomain.
    """
    host = request.url.hostname or ""
    if host in _LOCAL_HOSTS:
        return None
    return host


def set_session_cookies(resp, access_token: str, refresh_token: str, request=None) -> None:
    """Write the auth + refresh cookies onto a response. The one authoritative
    session-cookie writer, reused by login, company switch, and the modules
    restart flow."""
    domain = cookie_domain(request) if request is not None else None
    resp.set_cookie(COOKIE_NAME, access_token, httponly=True, samesite="lax", max_age=900, secure=_settings.cookie_secure, domain=domain)
    resp.set_cookie(REFRESH_COOKIE_NAME, refresh_token, httponly=True, samesite="lax", max_age=86400 * 30, secure=_settings.cookie_secure, domain=domain)


def clear_session_cookies(resp) -> None:
    """Delete the auth + refresh cookies from a response."""
    resp.delete_cookie(COOKIE_NAME)
    resp.delete_cookie(REFRESH_COOKIE_NAME)


def get_claims(request) -> dict:
    """Decode the JWT cookie payload without signature verification.

    The API is the enforcement point for every claim; the UI reads them only to
    shape what it renders and which company's files it will fetch. Returns an
    empty dict when there is no cookie or the payload will not decode, so every
    caller's own fallback is the least-privilege one.
    """
    import base64
    import json as _json

    token = get_token(request)
    if not token:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        claims = _json.loads(payload_bytes)
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def get_role(request) -> str:
    """Return the role claim (e.g. 'viewer', 'operator', 'manager', 'admin', 'owner').

    Falls back to 'viewer' (least privilege) when the claim is absent or unreadable.
    """
    from celerp.services.auth import _ROLE_MIGRATION

    raw_role = get_claims(request).get("role", "viewer")
    return _ROLE_MIGRATION.get(raw_role, raw_role)


def get_user_email(request) -> str | None:
    """Return the email claim, or None when it is absent or unreadable."""
    return get_claims(request).get("email") or None


def get_company_id(request) -> str | None:
    """Return the company_id claim, or None when it is absent or unreadable."""
    company_id = get_claims(request).get("company_id")
    return str(company_id) if company_id else None


def get_enabled_modules(request) -> set[str]:
    """Return the set of enabled module names for the current company.

    Empty set when the claim is absent or unreadable (shows all nav items as fallback).
    """
    raw = get_claims(request).get("modules", [])
    return set(raw) if isinstance(raw, list) else set()


async def get_relay_info(request) -> dict:
    """Return relay status info for the topbar indicator.

    Returns a dict with keys:
      connected (bool), public_url (str | None)

    Best-effort: any failure returns connected=False.
    """
    token = get_token(request)
    if not token:
        return {"connected": False, "public_url": None}
    try:
        from ui.api_client import get_relay_status
        data = await get_relay_status(token)
        connected = data.get("connected", False)
        public_url = data.get("public_url") or None
        return {"connected": bool(connected), "public_url": public_url}
    except Exception:
        return {"connected": False, "public_url": None}
