# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for StorageBackend abstraction in services/attachments.py."""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.services.attachments import (
    LocalBackend,
    S3Backend,
    _build_backend,
    get_backend,
    infer_attachment_type,
    merge_attachments,
    remove_attachment,
    resolve_preview_image_id,
    store_upload,
)


# ── infer_attachment_type ─────────────────────────────────────────────────────

def test_infer_image():
    assert infer_attachment_type("image/jpeg") == "image"
    assert infer_attachment_type("image/png") == "image"


def test_infer_video():
    assert infer_attachment_type("video/mp4") == "video"


def test_infer_certificate_default():
    assert infer_attachment_type("application/pdf") == "certificate"
    assert infer_attachment_type("text/plain") == "certificate"
    assert infer_attachment_type("application/octet-stream") == "certificate"


# ── LocalBackend ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_local_backend_stores_file(tmp_path, monkeypatch):
    backend = LocalBackend()
    monkeypatch.setattr(backend, "_ROOT", tmp_path)
    content = b"fake image data"
    url = await backend.store("company-1", "att-123", "photo.png", content, "image/png")
    assert url.startswith("/static/attachments/company-1/att-123")
    stored = next((tmp_path / "company-1").glob("att-123*"))
    assert stored.read_bytes() == content


@pytest.mark.asyncio
async def test_local_backend_creates_company_dir(tmp_path, monkeypatch):
    backend = LocalBackend()
    monkeypatch.setattr(backend, "_ROOT", tmp_path)
    await backend.store("new-company", "id1", "f.pdf", b"data", "application/pdf")
    assert (tmp_path / "new-company").is_dir()


# ── S3Backend ─────────────────────────────────────────────────────────────────

def _make_s3_mock():
    """Inject a fake aiobotocore module so tests run without the real package."""
    import sys
    import types

    mock_client = AsyncMock()
    mock_client.put_object = AsyncMock()
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_context.__aexit__ = AsyncMock(return_value=False)

    mock_session_instance = MagicMock()
    mock_session_instance.create_client.return_value = mock_context

    mock_get_session = MagicMock(return_value=mock_session_instance)

    # Build a minimal fake aiobotocore package
    aio_mod = types.ModuleType("aiobotocore")
    aio_session_mod = types.ModuleType("aiobotocore.session")
    aio_session_mod.get_session = mock_get_session
    aio_mod.session = aio_session_mod

    sys.modules.setdefault("aiobotocore", aio_mod)
    sys.modules.setdefault("aiobotocore.session", aio_session_mod)

    return mock_client, mock_get_session


@pytest.mark.asyncio
async def test_s3_backend_calls_put_object():
    mock_client, _ = _make_s3_mock()
    backend = S3Backend(
        endpoint="https://minio.example.com",
        bucket="test-bucket",
        access_key="key",
        secret_key="secret",
    )
    url = await backend.store("company-1", "att-456", "img.jpg", b"data", "image/jpeg")

    mock_client.put_object.assert_awaited_once()
    call_kwargs = mock_client.put_object.call_args.kwargs
    assert call_kwargs["Bucket"] == "test-bucket"
    assert "company-1/att-456" in call_kwargs["Key"]
    assert call_kwargs["ContentType"] == "image/jpeg"


@pytest.mark.asyncio
async def test_s3_backend_url_with_custom_endpoint():
    _make_s3_mock()
    backend = S3Backend(
        endpoint="https://minio.example.com",
        bucket="mybucket",
        access_key="k",
        secret_key="s",
    )
    url = await backend.store("co", "id1", "x.png", b"", "image/png")

    assert url.startswith("https://minio.example.com/mybucket/")


@pytest.mark.asyncio
async def test_s3_backend_url_without_endpoint():
    """When no custom endpoint, URL should use standard AWS S3 format."""
    _make_s3_mock()
    backend = S3Backend(endpoint="", bucket="mybucket", access_key="k", secret_key="s")
    url = await backend.store("co", "id1", "x.png", b"", "image/png")

    assert "mybucket.s3.amazonaws.com" in url


# ── _build_backend factory ────────────────────────────────────────────────────

def test_build_backend_local_by_default(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    # Reload settings to pick up env
    from celerp import config as cfg
    original = cfg.settings.storage_backend
    cfg.settings.storage_backend = "local"
    backend = _build_backend()
    cfg.settings.storage_backend = original
    assert isinstance(backend, LocalBackend)


def test_build_backend_s3(monkeypatch):
    from celerp import config as cfg
    original = cfg.settings.storage_backend
    cfg.settings.storage_backend = "s3"
    cfg.settings.storage_s3_endpoint = "https://r2.example.com"
    cfg.settings.storage_s3_bucket = "bucket"
    cfg.settings.storage_s3_access_key = "key"
    cfg.settings.storage_s3_secret_key = "secret"
    backend = _build_backend()
    cfg.settings.storage_backend = original
    assert isinstance(backend, S3Backend)


# ── store_upload dispatches to backend ───────────────────────────────────────

@pytest.mark.asyncio
async def test_store_upload_uses_backend(monkeypatch):
    """store_upload should call the active backend and return correct metadata."""
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    captured = {}

    class FakeBackend:
        async def store(self, company_id, att_id, filename, content, mime):
            captured.update({"company_id": company_id, "mime": mime})
            return f"/fake/{att_id}.png"

    import celerp.services.attachments as _mod
    original = _mod._backend
    _mod._backend = FakeBackend()

    content = b"\x89PNG\r\n"
    upload = UploadFile(
        file=io.BytesIO(content),
        filename="test.png",
        headers=Headers({"content-type": "image/png"}),
    )
    result = await store_upload("co-1", upload)

    _mod._backend = original

    assert result["type"] == "image"
    assert result["size"] == len(content)
    assert "/fake/" in result["url"]
    assert captured["company_id"] == "co-1"
    assert captured["mime"] == "image/png"


@pytest.mark.asyncio
async def test_store_upload_rejects_oversized():
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    big = b"x" * (51 * 1024 * 1024)
    upload = UploadFile(
        file=io.BytesIO(big),
        filename="big.png",
        headers=Headers({"content-type": "image/png"}),
    )
    with pytest.raises(ValueError, match="exceeds"):
        await store_upload("co-1", upload)


@pytest.mark.asyncio
async def test_store_upload_rejects_bad_mime():
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    upload = UploadFile(
        file=io.BytesIO(b"#!/bin/sh"),
        filename="script.sh",
        headers=Headers({"content-type": "application/x-sh"}),
    )
    with pytest.raises(ValueError, match="Unsupported"):
        await store_upload("co-1", upload)


# ── merge / remove / resolve_preview ─────────────────────────────────────────

def test_merge_adds_new():
    existing = [{"id": "a", "type": "image"}]
    result = merge_attachments(existing, {"id": "b", "type": "certificate"})
    assert len(result) == 2


def test_merge_replaces_existing():
    existing = [{"id": "a", "type": "image", "url": "/old"}]
    result = merge_attachments(existing, {"id": "a", "type": "image", "url": "/new"})
    assert len(result) == 1
    assert result[0]["url"] == "/new"


def test_remove_attachment():
    existing = [{"id": "a"}, {"id": "b"}]
    result = remove_attachment(existing, "a")
    assert len(result) == 1
    assert result[0]["id"] == "b"


def test_resolve_preview_keeps_existing():
    atts = [{"id": "a", "type": "image"}, {"id": "b", "type": "image"}]
    assert resolve_preview_image_id("a", atts) == "a"


def test_resolve_preview_falls_back_on_removal():
    atts = [{"id": "b", "type": "image"}]
    assert resolve_preview_image_id("a", atts, removed_id="a") == "b"


def test_resolve_preview_none_when_no_images():
    atts = [{"id": "c", "type": "certificate"}]
    assert resolve_preview_image_id("a", atts) is None
