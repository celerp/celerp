# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Short-lived public links to a product image, for a store to fetch it.

A store takes a product image only from a public address, and Celerp's files are not
public. Each image a sync sends gets a link of its own: this instance signs a token
naming one stored image of one company, valid for MEDIA_TTL, and the relay wraps it in
a share address that routes back here (relay_share.mint_media_url). Only images are
ever served this way, never certificates or documents.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

MEDIA_TTL = timedelta(minutes=15)


def _key() -> bytes:
    from celerp.config import settings

    return hashlib.sha256(b"connector-media:" + settings.jwt_secret.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def media_token(company_id, file: dict, *, now: datetime | None = None) -> str | None:
    """A token for one stored image of one company, or None for any file that is not
    an image."""
    mime = str(file.get("mime") or "")
    if not mime.startswith("image/") or not file.get("url"):
        return None
    expires = (now or datetime.now(timezone.utc)) + MEDIA_TTL
    body = _b64(json.dumps({"c": str(company_id), "u": file["url"], "m": mime,
                            "e": int(expires.timestamp())}, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())}"


def media_claims(token: str, *, now: datetime | None = None) -> dict | None:
    """The image a valid, unexpired token names ({"c", "u", "m"}), or None."""
    body, _, signature = str(token).partition(".")
    expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not body or not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if int(claims.get("e", 0)) < int((now or datetime.now(timezone.utc)).timestamp()):
        return None
    if not str(claims.get("m", "")).startswith("image/"):
        return None
    return claims
