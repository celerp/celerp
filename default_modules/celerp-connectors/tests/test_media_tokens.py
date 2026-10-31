# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The short-lived image links a store fetches product images from
(celerp.services.connector_media and the /connector-media route)."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

# The relay's media token shape, copied from relay/services/connector_media.py
# (MEDIA_TOKEN), which is the authority: the relay mints a share address for nothing else.
RELAY_MEDIA_TOKEN = r"[A-Za-z0-9_-]{43}"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300


@pytest.fixture(autouse=True)
def _tokens(media_tokens_in_test_transaction):
    """Tokens are issued and read inside the test's transaction."""


def _image(company_id: str, name: str = "f1.png") -> dict:
    return {"id": name.split(".")[0], "mime": "image/png",
            "url": f"/static/attachments/{company_id}/{name}"}


def _stored(company_id: str, name: str = "f1.png", content: bytes = PNG) -> dict:
    from celerp.config import settings

    folder = settings.data_dir / "static" / "attachments" / company_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return _image(company_id, name)


def _shapes(token: str) -> list[str]:
    """Every request shape that is not exactly a token: traversal, encoded and
    double-encoded slashes and dots, backslashes, lookalike letters, query and
    fragment, wrong lengths, empty."""
    return ["", ".", "..", "../" + token[3:], token[:-1], token + "A", token[:-1] + ".",
            token[:-1] + "/", token[:-1] + "\\", token[:-3] + "%2f", token[:-3] + "%2e",
            token[:-5] + "%252f", token[:-5] + "%252e", token[:-1] + "Ａ", token[:-1] + "а",
            token + "?x=1", token + "#x", token[:-1] + " ", token[:-1] + "\x00", f"{token}/{token}"]


@pytest.mark.asyncio
async def test_a_media_token_has_the_relays_shape_and_only_its_hash_is_kept():
    from sqlalchemy import select

    from celerp.models.connector_media import ConnectorMediaToken
    from celerp.services.connector_media import MEDIA_TOKEN, SessionLocal, issue_media_token

    assert MEDIA_TOKEN.pattern == RELAY_MEDIA_TOKEN
    cid = str(uuid.uuid4())
    token = await issue_media_token(cid, _image(cid))
    assert re.fullmatch(RELAY_MEDIA_TOKEN, token)
    async with SessionLocal() as s:
        rows = (await s.execute(select(ConnectorMediaToken).where(ConnectorMediaToken.company_id == cid))).scalars().all()
    assert [r.token_hash for r in rows] == [hashlib.sha256(token.encode()).hexdigest()]
    assert all(token not in str(v) for r in rows for v in vars(r).values())


@pytest.mark.asyncio
async def test_a_token_names_one_company_image_and_expires():
    from celerp.services.connector_media import MEDIA_TTL, issue_media_token, media_file

    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    token = await issue_media_token(cid, _image(cid), now=now)
    found = await media_file(token, now=now + timedelta(minutes=5))
    assert (found.company_id, found.url, found.mime) == (cid, _image(cid)["url"], "image/png")
    assert await media_file(token, now=now + MEDIA_TTL + timedelta(seconds=1)) is None
    # Never a document, and never a file outside the company's own stored files.
    assert await issue_media_token(cid, {"id": "d1", "mime": "application/pdf",
                                         "url": f"/static/attachments/{cid}/d1.pdf"}) is None
    other = str(uuid.uuid4())
    assert await issue_media_token(cid, _image(other)) is None
    assert await issue_media_token(cid, {"id": "x", "mime": "image/png",
                                         "url": f"/static/attachments/{cid}/../{other}/f1.png"}) is None


@pytest.mark.asyncio
async def test_a_malformed_token_is_refused_before_any_lookup():
    from celerp.services import connector_media

    token = "A" * 43
    with patch.object(connector_media, "SessionLocal", side_effect=AssertionError("looked up")):
        for shape in _shapes(token):
            assert await connector_media.media_file(shape) is None, shape


@pytest.mark.asyncio
async def test_the_media_route_streams_a_valid_image_and_nothing_else(client):
    from celerp.services.connector_media import issue_media_token

    cid = str(uuid.uuid4())
    token = await issue_media_token(cid, _stored(cid))
    ok = await client.get(f"/connector-media/{token}")
    assert ok.status_code == 200 and ok.content == PNG
    assert ok.headers["content-type"] == "image/png" and ok.headers["content-length"] == str(len(PNG))
    assert ok.headers["cache-control"] == "no-store"
    # A query or fragment is not part of the path: the path is still the token.
    for shape in [s for s in _shapes(token) if s[43:44] not in ("?", "#")]:
        try:
            resp = await client.get(f"/connector-media/{shape}")
        except httpx.InvalidURL:  # no HTTP client can send it
            continue
        assert resp.status_code in (404, 405) or resp.content != PNG, shape


@pytest.mark.asyncio
async def test_head_answers_from_the_files_size_without_reading_it(client):
    from celerp.services import attachments
    from celerp.services.connector_media import issue_media_token

    cid = str(uuid.uuid4())
    token = await issue_media_token(cid, _stored(cid))
    with patch.object(attachments.LocalBackend, "read", side_effect=AssertionError("read")), \
            patch.object(attachments.LocalBackend, "chunks", side_effect=AssertionError("read"), create=True), \
            patch.object(attachments, "_read_local", side_effect=AssertionError("read")):
        head = await client.head(f"/connector-media/{token}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(PNG)) and head.headers["content-type"] == "image/png"
    assert (await client.head("/connector-media/" + "A" * 43)).status_code == 404
