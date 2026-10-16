# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Validate company handoff links before downloading an encrypted backup."""
from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_FRAGMENT = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_HTTPS_HOST = "relay.celerp.com"


@dataclass(frozen=True)
class CompanyHandoffLink:
    copy_id: uuid.UUID
    secret: bytes = field(repr=False)

    @property
    def proof_hash(self) -> str:
        return hashlib.sha256(self.secret).hexdigest()


def parse_handoff_link(value: str) -> CompanyHandoffLink:
    """Accept only the emailed handoff page or its Celerp desktop link."""
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid company copy link")
    parsed = urlsplit(value)
    if parsed.query or parsed.username or parsed.password or parsed.port is not None:
        raise ValueError("Invalid company copy link")
    if parsed.scheme == "https" and parsed.netloc == _HTTPS_HOST:
        parts = parsed.path.split("/")
        if len(parts) != 4 or parts[1] != "handoffs" or parts[3] != "open":
            raise ValueError("Invalid company copy link")
        raw_id = parts[2]
    elif parsed.scheme == "celerp" and parsed.netloc == "handoff":
        raw_id = parsed.path.removeprefix("/")
        if not parsed.path.startswith("/") or "/" in raw_id:
            raise ValueError("Invalid company copy link")
    else:
        raise ValueError("Invalid company copy link")
    try:
        copy_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        raise ValueError("Invalid company copy link") from None
    if str(copy_id) != raw_id or not _FRAGMENT.fullmatch(parsed.fragment):
        raise ValueError("Invalid company copy link")
    try:
        secret = base64.urlsafe_b64decode(parsed.fragment + "=")
    except ValueError:
        raise ValueError("Invalid company copy link") from None
    if len(secret) != 32 or base64.urlsafe_b64encode(secret).decode().rstrip("=") != parsed.fragment:
        raise ValueError("Invalid company copy link")
    return CompanyHandoffLink(copy_id, secret)


def handoff_page_link(copy_id: uuid.UUID, secret: bytes) -> str:
    """Construct an emailed link with the decryption key only in its fragment."""
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError("Invalid company copy key")
    fragment = base64.urlsafe_b64encode(secret).decode().rstrip("=")
    return f"https://{_HTTPS_HOST}/handoffs/{copy_id}/open#{fragment}"
