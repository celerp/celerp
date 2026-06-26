# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for connector image/file helpers (celerp/connectors/images.py)."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from celerp.connectors.images import (
    _file_already_stored,
    _is_image_mime,
    build_platform_cert_payload,
    build_platform_image_payload,
    download_and_emit_file,
)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_is_image_mime():
    assert _is_image_mime("image/png")
    assert not _is_image_mime("application/pdf")


def test_file_already_stored():
    assert _file_already_stored([{"url": "a"}], "a")
    assert not _file_already_stored([{"url": "a"}], "b")


def test_build_image_payload_hero_and_additional():
    files = [
        {"url": "h", "is_hero": True, "mime": "image/png", "document_tag": "product_images"},
        {"url": "a", "is_hero": False, "mime": "image/jpeg", "document_tag": "product_images"},
        {"url": "doc", "is_hero": False, "mime": "application/pdf", "document_tag": "product_images"},
    ]
    p = build_platform_image_payload(files)
    assert p["hero_url"] == "h"
    assert p["additional_urls"] == ["a"]  # pdf excluded (not image)


def test_build_cert_payload():
    files = [
        {"url": "c", "filename": "cert.pdf", "document_tag": "certificates"},
        {"url": "i", "filename": "x.jpg", "document_tag": "product_images"},
    ]
    p = build_platform_cert_payload(files)
    assert p == {"certificates": [{"name": "cert.pdf", "url": "c"}]}


# ── download_and_emit_file ───────────────────────────────────────────────────

def _proj(files):
    p = MagicMock()
    p.state = {"files": files}
    return p


def _sess(projection):
    s = MagicMock()
    s.get = AsyncMock(return_value=projection)
    return s


_ARGS = ("co", "item:1", "sys", "https://x.test/img.jpg", "f.jpg", "product_images", True)


@pytest.mark.asyncio
async def test_download_emit_no_projection_returns_false():
    assert await download_and_emit_file(_sess(None), *_ARGS) is False


@pytest.mark.asyncio
async def test_download_emit_already_stored_returns_false():
    sess = _sess(_proj([{"url": "https://x.test/img.jpg"}]))
    assert await download_and_emit_file(sess, *_ARGS) is False


@pytest.mark.asyncio
async def test_download_emit_download_failure_returns_false():
    sess = _sess(_proj([]))
    with respx.mock:
        respx.get("https://x.test/img.jpg").mock(return_value=httpx.Response(500))
        assert await download_and_emit_file(sess, *_ARGS) is False


@pytest.mark.asyncio
async def test_download_emit_store_failure_returns_false():
    sess = _sess(_proj([]))
    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=AsyncMock(side_effect=RuntimeError("disk full"))):
        respx.get("https://x.test/img.jpg").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"}))
        assert await download_and_emit_file(sess, *_ARGS) is False


@pytest.mark.asyncio
async def test_download_emit_success_emits_event():
    sess = _sess(_proj([]))
    meta = {"id": "f1", "filename": "f.jpg", "mime": "image/jpeg", "size": 3, "url": "/files/f1"}
    emit = AsyncMock()
    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=AsyncMock(return_value=meta)), \
         patch("celerp.events.engine.emit_event", new=emit):
        respx.get("https://x.test/img.jpg").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"}))
        result = await download_and_emit_file(sess, *_ARGS)
    assert result is True
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["event_type"] == "item.file.attached"
    assert kwargs["data"]["file_id"] == "f1"
    assert kwargs["data"]["is_hero"] is True


@pytest.mark.asyncio
async def test_download_emit_guesses_mime_when_header_missing():
    """No content-type header → fall back to mimetypes.guess_type from filename."""
    sess = _sess(_proj([]))
    meta = {"id": "f2", "filename": "f.jpg", "mime": "image/jpeg", "size": 3, "url": "/x"}
    captured = {}

    async def _store(company_id, upload):
        captured["ct"] = upload.headers.get("content-type")
        return meta

    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=_store), \
         patch("celerp.events.engine.emit_event", new=AsyncMock()):
        respx.get("https://x.test/img.jpg").mock(return_value=httpx.Response(200, content=b"abc"))  # no content-type
        result = await download_and_emit_file(sess, *_ARGS)
    assert result is True
    assert captured["ct"] == "image/jpeg"  # guessed from f.jpg
