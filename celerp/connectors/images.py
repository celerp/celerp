# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shared image and file helpers for connectors.

Used by WooCommerce and Shopify connectors to push/pull product images
and certificate metafields in a DRY, platform-agnostic way.
"""
from __future__ import annotations

import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_CERT_TAGS = ("certificates", "spec_sheets", "safety_docs")
_PRODUCT_IMAGE_TAG = "product_images"


def _is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def _file_already_stored(files: list[dict], url: str) -> bool:
    """Check whether a remote URL is already present in the item's files list."""
    return any(f.get("url") == url for f in files)


def build_platform_image_payload(files: list[dict]) -> dict:
    """Return hero_url and additional_urls from an item's files list.

    Returns:
        {"hero_url": str | None, "additional_urls": list[str]}
    """
    hero = next((f for f in files if f.get("is_hero") and _is_image_mime(f.get("mime", ""))), None)
    additional = [
        f["url"] for f in files
        if f.get("document_tag") == _PRODUCT_IMAGE_TAG
        and not f.get("is_hero")
        and _is_image_mime(f.get("mime", ""))
        and f.get("url")
    ]
    return {
        "hero_url": hero["url"] if hero and hero.get("url") else None,
        "additional_urls": additional,
    }


def build_platform_cert_payload(files: list[dict]) -> dict:
    """Return {tag_slug: [{"name": filename, "url": url}]} for each cert-like tag.

    Only includes tags that have at least one file.
    """
    result: dict[str, list[dict]] = {}
    for tag in _CERT_TAGS:
        entries = [
            {"name": f.get("filename", ""), "url": f["url"]}
            for f in files
            if f.get("document_tag") == tag and f.get("url")
        ]
        if entries:
            result[tag] = entries
    return result


async def download_and_emit_file(
    session,
    company_id: str,
    entity_id: str,
    actor_id: str,
    url: str,
    filename: str,
    tag: str,
    is_hero: bool,
) -> bool:
    """Download a remote file URL, store it, and emit item.file.attached.

    Returns True if stored successfully, False if download failed.
    Skips if URL already stored (idempotent).
    """
    from celerp.models.projections import Projection
    from celerp.events.engine import emit_event
    from celerp.services.attachments import store_upload
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    # Idempotency: check current state
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        return False
    existing_files: list[dict] = row.state.get("files") or []
    if _file_already_stored(existing_files, url):
        return False  # already have this URL

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            if not content_type:
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    except Exception as exc:
        log.warning("connector: failed to download %s: %s", url, exc)
        return False

    import io
    upload = UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )
    try:
        meta = await store_upload(company_id, upload)
    except Exception as exc:
        log.warning("connector: failed to store %s: %s", filename, exc)
        return False

    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.file.attached",
        data={
            "entity_id": entity_id,
            "entity_type": "item",
            "file_id": meta["id"],
            "filename": meta["filename"],
            "mime": meta["mime"],
            "size": meta["size"],
            "url": meta.get("url", ""),
            "document_tag": tag,
            "description": None,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "is_hero": is_hero,
        },
        actor_id=actor_id,
        location_id=None,
        source="connector",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    return True
