# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Attachment storage service.

Stores files via a pluggable StorageBackend:
  - LocalBackend  (default): writes to static/attachments/<company_id>/
  - S3Backend     (Team tier): writes to S3/R2/DO Spaces/MinIO via presigned PUT

Each attachment entry:
  {
    "id": str,
    "type": "image" | "video" | "certificate" | "view_360",
    "filename": str,
    "url": str,
    "size": int,
    "mime": str,
  }

preview_image_id (str | None) is stored at the item level (not per-attachment).
It must refer to an attachment whose type == "image".  Only images can be previews.

Backend selection: driven by celerp.config.settings.storage_backend.
  STORAGE_BACKEND=local  → LocalBackend (default, no extra config)
  STORAGE_BACKEND=s3     → S3Backend (requires STORAGE_S3_* env vars)
"""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Literal, Protocol

from fastapi import UploadFile

# ── Types ─────────────────────────────────────────────────────────────────────

AttachmentType = Literal["image", "video", "certificate", "view_360"]

# ── MIME sets ─────────────────────────────────────────────────────────────────

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
_VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime", "video/x-msvideo"}
_CERT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
_ALLOWED_MIMES = _IMAGE_MIMES | _VIDEO_MIMES | _CERT_MIMES

_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def infer_attachment_type(mime: str) -> AttachmentType:
    """Infer attachment type from MIME. Returns 'certificate' as default for docs."""
    if mime in _IMAGE_MIMES:
        return "image"
    if mime in _VIDEO_MIMES:
        return "video"
    return "certificate"


# ── StorageBackend protocol ───────────────────────────────────────────────────

class StorageBackend(Protocol):
    async def store(
        self,
        company_id: str,
        att_id: str,
        filename: str,
        content: bytes,
        mime: str,
    ) -> str:
        """Persist content and return the public URL."""
        ...


# ── LocalBackend ──────────────────────────────────────────────────────────────

class LocalBackend:
    """Writes files to static/attachments/<company_id>/. Default backend."""

    _ROOT = Path("static/attachments")

    def _company_dir(self, company_id: str) -> Path:
        d = self._ROOT / str(company_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def store(
        self,
        company_id: str,
        att_id: str,
        filename: str,
        content: bytes,
        mime: str,
    ) -> str:
        ext = Path(filename).suffix or (mimetypes.guess_extension(mime) or "")
        dest_name = f"{att_id}{ext}"
        dest = self._company_dir(company_id) / dest_name
        dest.write_bytes(content)
        return f"/static/attachments/{company_id}/{dest_name}"


# ── S3Backend ─────────────────────────────────────────────────────────────────

class S3Backend:
    """Stores files in S3/R2/DO Spaces/MinIO via aiobotocore."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
        self._endpoint = endpoint
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key

    async def store(
        self,
        company_id: str,
        att_id: str,
        filename: str,
        content: bytes,
        mime: str,
    ) -> str:
        import aiobotocore.session  # type: ignore[import]

        ext = Path(filename).suffix or (mimetypes.guess_extension(mime) or "")
        key = f"attachments/{company_id}/{att_id}{ext}"

        session = aiobotocore.session.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self._endpoint or None,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        ) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime,
            )

        # Construct public URL
        if self._endpoint:
            # MinIO / DO Spaces / R2 custom endpoint
            base = self._endpoint.rstrip("/")
            return f"{base}/{self._bucket}/{key}"
        else:
            # AWS S3
            return f"https://{self._bucket}.s3.amazonaws.com/{key}"


# ── Backend factory ───────────────────────────────────────────────────────────

def _build_backend() -> StorageBackend:
    from celerp.config import settings
    if settings.storage_backend == "s3":
        return S3Backend(
            endpoint=settings.storage_s3_endpoint,
            bucket=settings.storage_s3_bucket,
            access_key=settings.storage_s3_access_key,
            secret_key=settings.storage_s3_secret_key,
        )
    return LocalBackend()


# Module-level singleton — resolved once at import time.
# Tests can monkey-patch this to inject a fake backend.
_backend: StorageBackend = _build_backend()


def get_backend() -> StorageBackend:
    return _backend


# ── Public API ────────────────────────────────────────────────────────────────

async def store_upload(
    company_id: str,
    file: UploadFile,
    attachment_type: AttachmentType | None = None,
) -> dict:
    """Save an uploaded file; return attachment metadata dict.

    If attachment_type is provided, it overrides MIME-based inference.
    Callers pass attachment_type="view_360" for 360 images uploaded as image/jpeg.
    """
    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise ValueError(f"File exceeds {_MAX_FILE_BYTES // 1024 // 1024} MB limit")

    mime = file.content_type or (
        mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    )
    if mime not in _ALLOWED_MIMES:
        raise ValueError(f"Unsupported file type: {mime}")

    att_type: AttachmentType = attachment_type or infer_attachment_type(mime)
    att_id = str(uuid.uuid4())
    filename = file.filename or f"file_{att_id}"

    url = await get_backend().store(company_id, att_id, filename, content, mime)

    return {
        "id": att_id,
        "type": att_type,
        "filename": filename,
        "url": url,
        "size": len(content),
        "mime": mime,
    }


def merge_attachments(existing: list[dict], new_entry: dict) -> list[dict]:
    """Add or replace attachment by id."""
    updated = [a for a in existing if a.get("id") != new_entry["id"]]
    updated.append(new_entry)
    return updated


def remove_attachment(existing: list[dict], att_id: str) -> list[dict]:
    return [a for a in existing if a.get("id") != att_id]


def resolve_preview_image_id(
    existing_preview: str | None,
    attachments: list[dict],
    removed_id: str | None = None,
) -> str | None:
    """Return the preview_image_id to store after an attachment change.

    - If existing_preview refers to a still-valid image attachment, keep it.
    - If it was removed, fall back to the first remaining image.
    - If there are no image attachments, return None.
    """
    images = [a for a in attachments if a.get("type") == "image"]
    if not images:
        return None
    if existing_preview and existing_preview != removed_id:
        if any(a["id"] == existing_preview for a in images):
            return existing_preview
    return images[0]["id"]
