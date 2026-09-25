# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

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

import asyncio
import io
import logging
import mimetypes
import time
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

# The stored extension is decided by the validated type, never the caller's
# chosen name, so a crafted name cannot make the file be served as something it
# is not. One entry per allowlisted MIME keeps the extension deterministic
# across hosts: resolving a type against the host's own registry can yield a
# different extension, or none, on another machine.
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


def _stored_extension(mime: str) -> str:
    """Extension for the stored file, from the validated type alone.

    Unknown types get no extension rather than a guessed one, so a path is never
    fabricated for a type the allowlist does not admit."""
    return _MIME_EXTENSIONS.get(mime, "")


_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Longest side of the JPEG list thumbnail derived from every image upload.
_THUMB_MAX_SIDE = 160
# Largest image (in pixels) a thumbnail is decoded from; bigger uploads keep their original and
# get no preview, so one oversized upload can never pin the process on decoding it.
_MAX_IMAGE_PIXELS = 40_000_000
_THUMB_MIME = "image/jpeg"

logger = logging.getLogger(__name__)


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
        content: bytes,
        mime: str,
    ) -> str:
        """Persist content and return the public URL."""
        ...


# ── LocalBackend ──────────────────────────────────────────────────────────────

class LocalBackend:
    """Writes files to <data_dir>/static/attachments/<company_id>/."""

    @property
    def _root(self) -> Path:
        from celerp.config import settings  # lazy: settings not ready at import time
        return settings.data_dir / "static" / "attachments"

    def _company_dir(self, company_id: str) -> Path:
        d = self._root / str(company_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def store(
        self,
        company_id: str,
        att_id: str,
        content: bytes,
        mime: str,
    ) -> str:
        dest_name = f"{att_id}{_stored_extension(mime)}"
        root = self._company_dir(company_id).resolve()
        dest = (root / dest_name).resolve() if _is_plain_name(att_id) else None
        if dest is None or dest.parent != root:
            raise ValueError(f"Invalid attachment id: {att_id!r}")
        dest.write_bytes(content)
        return f"/static/attachments/{company_id}/{dest_name}"


def _is_plain_name(name: str) -> bool:
    """A single file name: no separator of either platform and no dot reference."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def local_attachment_path(company_id: str, filename: str) -> Path | None:
    """Resolve a stored attachment to its on-disk path for local delivery.

    Returns the file path only when it exists inside this company's own
    attachment directory. A name carrying a separator or a parent reference, or
    a resolved path that escapes the company directory, yields None so the
    caller serves a 404 rather than another tenant's file. This owns the
    on-disk layout shared with :class:`LocalBackend`.
    """
    if not _is_plain_name(filename):
        return None
    from celerp.config import settings  # lazy: settings not ready at import time
    root = (settings.data_dir / "static" / "attachments" / str(company_id)).resolve()
    target = (root / filename).resolve()
    if target.parent != root or not target.is_file():
        return None
    return target


# ── S3Backend ─────────────────────────────────────────────────────────────────

def _s3_client(endpoint: str, access_key: str, secret_key: str):
    """Single construction point for the aiobotocore S3 client, returning the
    async context manager that both S3Backend.store and the settings
    connectivity test consume - one place builds the client (DRY). aiobotocore
    is imported lazily so a build without it degrades at the call site instead of
    at import time."""
    import aiobotocore.session  # type: ignore[import]

    session = aiobotocore.session.get_session()
    return session.create_client(
        "s3",
        endpoint_url=endpoint or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


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
        content: bytes,
        mime: str,
    ) -> str:
        if not _is_plain_name(att_id):
            raise ValueError(f"Invalid attachment id: {att_id!r}")
        key = f"attachments/{company_id}/{att_id}{_stored_extension(mime)}"

        async with _s3_client(self._endpoint, self._access_key, self._secret_key) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime,
            )

        return self._public_url(key)

    def _public_url(self, key: str) -> str:
        if self._endpoint:
            # MinIO / DO Spaces / R2 custom endpoint
            base = self._endpoint.rstrip("/")
            return f"{base}/{self._bucket}/{key}"
        # AWS S3
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"

    async def read(self, company_id: str, url: str, max_bytes: int) -> bytes | None:
        """Read back an object this backend stored for ``company_id``, by its public URL.

        Any other URL, or an object larger than ``max_bytes``, yields None."""
        prefix = f"attachments/{company_id}/"
        base = self._public_url(prefix)
        name = url[len(base):] if url.startswith(base) else ""
        if not name or "/" in name or name in (".", ".."):
            return None
        async with _s3_client(self._endpoint, self._access_key, self._secret_key) as client:
            resp = await client.get_object(Bucket=self._bucket, Key=prefix + name)
            if int(resp.get("ContentLength") or 0) > max_bytes:
                return None
            async with resp["Body"] as stream:
                data = await stream.read(max_bytes + 1)
        return data if len(data) <= max_bytes else None


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

    url = await get_backend().store(company_id, att_id, content, mime)

    meta = {
        "id": att_id,
        "type": att_type,
        "filename": filename,
        "url": url,
        "size": len(content),
        "mime": mime,
    }
    # The preview is best effort: the original is stored and returned whatever happens to
    # the thumbnail, so a failed derivative never turns a completed upload into an error.
    thumb = await asyncio.to_thread(make_thumbnail, content, mime)
    if thumb is not None:
        try:
            meta["thumb_url"] = await get_backend().store(
                company_id, thumbnail_id(att_id), thumb, _THUMB_MIME
            )
        except Exception:
            logger.warning("thumbnail store failed for attachment %s", att_id)
    return meta


def thumbnail_id(att_id: str) -> str:
    """Storage id of the list thumbnail derived from attachment ``att_id``."""
    return f"{att_id}_thumb"


def thumbnail_name(att_id: str) -> str:
    """Stored filename of the list thumbnail for attachment ``att_id``."""
    return thumbnail_id(att_id) + _stored_extension(_THUMB_MIME)


def make_thumbnail(content: bytes, mime: str) -> bytes | None:
    """Return a JPEG thumbnail (longest side ``_THUMB_MAX_SIDE``) or None.

    None for non-image mimes, for images above ``_MAX_IMAGE_PIXELS`` and for bytes
    Pillow cannot decode: the original upload is kept either way and the list simply
    shows no preview. Pillow decodes synchronously; callers on the event loop run this
    in a thread.
    """
    if mime not in _IMAGE_MIMES:
        return None
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(content)) as im:
            if im.width * im.height > _MAX_IMAGE_PIXELS:
                logger.warning("thumbnail skipped: %sx%s image exceeds the pixel limit", im.width, im.height)
                return None
            # JPEG decodes straight to a reduced size; other formats ignore the hint.
            im.draft("RGB", (_THUMB_MAX_SIDE, _THUMB_MAX_SIDE))
            im.load()
            # Honour the camera orientation tag so a portrait photo previews upright.
            im = ImageOps.exif_transpose(im) or im
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((_THUMB_MAX_SIDE, _THUMB_MAX_SIDE))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception:
        logger.warning("thumbnail generation failed for %s upload", mime)
        return None


async def get_or_create_thumbnail(company_id: str, attachment: dict) -> bytes | None:
    """Return the thumbnail bytes for a stored image attachment, or None.

    Locally stored uploads that predate thumbnails get one generated on first
    request and written next to the original, so the next request reads it
    back. Cloud-stored originals yield None here; see :func:`repair_remote_thumbnail`.
    """
    if attachment.get("mime") not in _IMAGE_MIMES:
        return None
    att_id = str(attachment.get("id") or "")
    existing = local_attachment_path(company_id, thumbnail_name(att_id))
    if existing is not None:
        return existing.read_bytes()
    url = str(attachment.get("url") or "")
    if url.startswith(("http://", "https://")):
        return None
    source = local_attachment_path(company_id, url.rsplit("/", 1)[-1])
    if source is None:
        return None
    data = await asyncio.to_thread(lambda: make_thumbnail(source.read_bytes(), attachment["mime"]))
    if data is None:
        return None
    try:
        await LocalBackend().store(company_id, thumbnail_id(att_id), data, _THUMB_MIME)
    except ValueError:
        logger.warning("thumbnail not stored for attachment %r", att_id)
        return None
    return data


# Remote thumbnail repairs run at most this many at a time per process, so a list page full
# of old cloud images cannot hold several full-size originals in memory at once. Further
# views wait their turn; one that waits too long previews from the original instead.
_MAX_REMOTE_REPAIRS = 2
_REMOTE_REPAIR_WAIT_S = 60
_REMOTE_REPAIR_TIMEOUT_S = 15
_remote_repairs = 0


async def repair_remote_thumbnail(company_id: str, attachment: dict) -> str | None:
    """Make and store the missing list thumbnail of a cloud-stored image; return its URL.

    For images stored in the cloud before thumbnails were recorded. Returns None, storing
    nothing, when the backend cannot read back what it stored, the original is not this
    backend's, the read fails or times out, the original is over the upload size limit or
    does not decode, the thumbnail cannot be stored, or no repair slot frees up in time.
    """
    global _remote_repairs
    backend = get_backend()
    read = getattr(backend, "read", None)
    url = str(attachment.get("url") or "")
    mime = attachment.get("mime")
    if read is None or mime not in _IMAGE_MIMES or not url.startswith(("http://", "https://")):
        return None
    deadline = time.monotonic() + _REMOTE_REPAIR_WAIT_S
    while _remote_repairs >= _MAX_REMOTE_REPAIRS:
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)
    _remote_repairs += 1
    att_id = str(attachment.get("id") or "")
    try:
        content = await asyncio.wait_for(read(company_id, url, _MAX_FILE_BYTES), _REMOTE_REPAIR_TIMEOUT_S)
        if content is None:
            return None
        thumb = await asyncio.to_thread(make_thumbnail, content, mime)
        if thumb is None:
            return None
        return await backend.store(company_id, thumbnail_id(att_id), thumb, _THUMB_MIME)
    except Exception:
        logger.warning("thumbnail repair failed for attachment %s", att_id)
        return None
    finally:
        _remote_repairs -= 1


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
