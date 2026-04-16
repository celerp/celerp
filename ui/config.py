# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

API_BASE = os.getenv("API_URL", os.getenv("CELERP_API_URL", "http://localhost:8000"))
RELAY_URL = os.getenv("CELERP_RELAY_URL", "https://relay.celerp.com")
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


def get_role(request) -> str:
    """Decode the role claim from the JWT cookie without signature verification.

    Returns the role string (e.g. 'viewer', 'operator', 'manager', 'admin', 'owner').
    Falls back to 'viewer' (least privilege) on any decode error.
    """
    import base64
    import json as _json

    from celerp.services.auth import _ROLE_MIGRATION

    token = get_token(request)
    if not token:
        return "viewer"
    try:
        payload_b64 = token.split(".")[1]
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        claims = _json.loads(payload_bytes)
        raw_role = claims.get("role", "viewer")
        return _ROLE_MIGRATION.get(raw_role, raw_role)
    except Exception:
        return "viewer"
