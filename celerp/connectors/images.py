# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Shared image and file helpers for connectors.

Used by WooCommerce and Shopify connectors to pull product images and certificate
metafields, and to send product images, in a DRY, platform-agnostic way.
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_REMOTE_FILE_BYTES = 50 * 1024 * 1024

_CERT_TAGS = ("certificates", "spec_sheets", "safety_docs")
_PRODUCT_IMAGE_TAG = "product_images"


def _is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def _already_pulled(files: list[dict], platform: str, remote: dict) -> bool:
    """Whether the item already holds this platform file: the same image id from the
    same store, or the same address when the platform gives no id."""
    key = "id" if remote.get("id") else "src"
    return any(
        (f.get("remote") or {}).get(platform, {}).get(key) == remote[key]
        and (f.get("remote") or {}).get(platform, {}).get("store") == remote.get("store")
        for f in files
    )


def product_image_files(files: list[dict]) -> list[dict]:
    """An item's product images in the order a store shows them: the hero first, then
    the other product images."""
    images = [f for f in files if _is_image_mime(f.get("mime", "")) and f.get("id")]
    hero = [f for f in images if f.get("is_hero")][:1]
    rest = [f for f in images if not f.get("is_hero") and f.get("document_tag") == _PRODUCT_IMAGE_TAG]
    return hero + rest


def store_media_id(file: dict, platform: str, link: dict, store: str | None) -> str | None:
    """The store's own id for an image it already holds: the one it gave when the image
    was pulled from this store, or the one it returned when Celerp sent it."""
    remote = (file.get("remote") or {}).get(platform) or {}
    if remote.get("id") and remote.get("store") == store:
        return str(remote["id"])
    known = (link.get("media_ids") or {}).get(file["id"])
    return str(known) if known else None


async def store_images(ctx, platform: str, service: str, files: list[dict],
                       link: dict) -> tuple[list[dict], str | None]:
    """The image list to send a store for an item's product images, or the reason none
    can be sent. An image the store holds goes by its id; any other by a short-lived
    public link (relay_share.mint_media_url) when it is within the size a store can fetch
    (connector_media.media_max_bytes); never by a Celerp address the store cannot reach.
    When one image cannot be sent none are, so the store's images are never replaced by
    part of the item's."""
    from celerp.services import relay_share
    from ui.i18n import t
    from celerp.services.attachments import company_file_size
    from celerp.services.connector_media import media_max_bytes

    images, limit = [], media_max_bytes()
    for file in product_image_files(files):
        media_id = store_media_id(file, platform, link, ctx.store_handle)
        if media_id is not None:
            images.append({"id": int(media_id) if media_id.isdigit() else media_id})
            continue
        size = await company_file_size(ctx.company_id, file["url"]) if file.get("url") else None
        if size is not None and size > limit:
            return [], t("connectors.attention_image_too_large", service=service,
                         name=file.get("filename") or file["id"], size=_megabytes(size), limit=_megabytes(limit))
        url = await relay_share.mint_media_url(ctx.company_id, file) if size is not None else None
        if url is None:
            return [], t("connectors.attention_image_unshared", service=service)
        images.append({"src": url})
    return images, None


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}".removesuffix(".0")


async def download_and_emit_file(
    session,
    company_id: str,
    entity_id: str,
    url: str,
    filename: str,
    tag: str,
    is_hero: bool,
    *,
    platform: str,
    store: str | None,
    remote_id=None,
    operation: str | None = None,
) -> str | None:
    """Download a platform file, store it, and attach it to the item, recording where it
    came from (its platform image id and store) on the attachment, and the first sync
    pulling it (``operation``, first_sync_metadata) so only that first sync's undo removes it.

    Returns the attachment's ledger key, or None when nothing was attached: the item
    already holds this file (a pull is idempotent), or the download or storing failed.
    """
    from celerp.connectors.base import first_sync_metadata
    from celerp.services.field_fingerprints import fingerprint

    from celerp.models.projections import Projection
    from celerp.services.attachments import attach_file, store_upload
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    # Idempotency: check current state
    row = await session.get(
        Projection, {"company_id": company_id, "entity_id": entity_id}, populate_existing=True
    )
    if row is None:
        return None
    existing_files: list[dict] = row.state.get("files") or []
    remote = {"id": str(remote_id) if remote_id not in (None, "") else None, "src": url, "store": store}
    if _already_pulled(existing_files, platform, remote):
        return None

    try:
        from celerp.services.outbound_url import fetch_public_bytes

        resp = await fetch_public_bytes(
            url,
            max_bytes=_MAX_REMOTE_FILE_BYTES,
            timeout=30,
            max_redirects=3,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"remote server returned {resp.status_code}")
        content = resp.content
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    except Exception as exc:
        log.warning("connector: failed to download %s: %s", url, exc)
        return None

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
        return None

    key = f"connector-file:{platform}:{entity_id}:{fingerprint(remote)}"
    await attach_file(session, company_id, "item", entity_id, meta, None, source="connector",
                      idempotency_key=key, document_tag=tag, is_hero=is_hero,
                      remote={platform: remote}, metadata_=first_sync_metadata(operation))
    return key
