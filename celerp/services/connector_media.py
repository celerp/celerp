# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Short-lived public links to a product image, for a store to fetch it.

A store takes a product image only from a public address, and Celerp's files are not
public. Each image a sync sends gets a link of its own: this instance issues a random
token naming one stored image of one company, valid for MEDIA_TTL, and the relay wraps
it in a share address that routes back here (relay_share.mint_media_url). Only the
token's hash is stored. Only images are ever served this way, never certificates or
documents.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from celerp.db import SessionLocal
from celerp.models.connector_media import ConnectorMediaToken

# The relay's media share address lives as long.
MEDIA_TTL = timedelta(minutes=15)
# 32 random bytes, urlsafe base64 without padding. The relay is the authority on this
# shape (relay/services/connector_media.py MEDIA_TOKEN) and wraps nothing else.
MEDIA_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class MediaFile:
    company_id: str
    url: str
    mime: str


def media_max_bytes() -> int:
    """The largest image a store can fetch: what the relay advertised, never above the
    attachment cap."""
    from celerp.gateway.state import get_tunnel_limits
    from celerp.services.attachments import MAX_FILE_BYTES

    return min(get_tunnel_limits().get("media_max_bytes", MAX_FILE_BYTES), MAX_FILE_BYTES)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_media_token(company_id, file: dict, *, now: datetime | None = None) -> str | None:
    """A token for one stored image of this company, or None for any file that is not
    an image the company stored."""
    from celerp.services.attachments import company_file_name

    mime = str(file.get("mime") or "")
    if not mime.startswith("image/") or company_file_name(company_id, file.get("url")) is None:
        return None
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    async with SessionLocal() as session:
        await session.execute(delete(ConnectorMediaToken).where(
            ConnectorMediaToken.company_id == str(company_id), ConnectorMediaToken.expires_at < now))
        session.add(ConnectorMediaToken(token_hash=_hash(token), company_id=str(company_id),
                                        url=file["url"], mime=mime, expires_at=now + MEDIA_TTL))
        await session.commit()
    return token


async def media_file(token: object, *, now: datetime | None = None) -> MediaFile | None:
    """The image an unexpired token names, or None. Anything not shaped as a token is
    refused before any lookup."""
    if not isinstance(token, str) or not MEDIA_TOKEN.fullmatch(token):
        return None
    async with SessionLocal() as session:
        row = (await session.execute(select(ConnectorMediaToken).where(
            ConnectorMediaToken.token_hash == _hash(token)))).scalar_one_or_none()
    if row is None or row.expires_at <= (now or datetime.now(timezone.utc)) or not row.mime.startswith("image/"):
        return None
    return MediaFile(row.company_id, row.url, row.mime)
