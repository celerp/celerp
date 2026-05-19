# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Attachment endpoints.

POST   /items/{entity_id}/attachments                     — upload one file
DELETE /items/{entity_id}/attachments/{id}                — remove one attachment
PUT    /items/{entity_id}/attachments/{id}/preview        — set preview image (images only)
POST   /items/attachments/bulk                            — ZIP upload; SKU-matched bulk attach
GET    /items/attachments/bulk/template                   — download a sample ZIP README

attachment_type values: image | video | certificate | view_360
preview_image_id: item-level field, only valid for type=image attachments.

On upload:
  - attachment_type query param overrides MIME inference (required for view_360).
  - preview_image_id auto-set to first image if none exists yet.
On delete:
  - If deleted attachment was the preview, fallback to first remaining image (or None).
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path as _Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.services.attachments import (
    AttachmentType,
    merge_attachments,
    remove_attachment,
    resolve_preview_image_id,
    store_upload,
)
from celerp.services.auth import get_current_company_id, get_current_user

router = APIRouter(dependencies=[Depends(get_current_user)])

_VALID_TYPES: set[str] = {"image", "video", "certificate", "view_360"}


async def _patch_item_attachments(
    session: AsyncSession,
    company_id: str,
    entity_id: str,
    actor_id: str,
    new_attachments: list[dict],
    preview_image_id: str | None,
) -> None:
    """Emit item.updated with updated attachments + preview_image_id."""
    fields: dict = {
        "attachments": {"new": new_attachments},
        "preview_image_id": {"new": preview_image_id},
    }
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": fields},
        actor_id=actor_id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )


@router.post("/{entity_id}/attachments")
async def upload_attachment(
    entity_id: str,
    file: UploadFile = File(...),
    attachment_type: str | None = Query(default=None),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload one file and attach it to an item.

    Pass ?attachment_type=view_360 to tag a 360 image/video explicitly.
    """
    if attachment_type is not None and attachment_type not in _VALID_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid attachment_type: {attachment_type!r}")

    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    try:
        att = await store_upload(
            company_id,
            file,
            attachment_type=attachment_type,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing: list[dict] = row.state.get("attachments") or []
    updated = merge_attachments(existing, att)
    existing_preview: str | None = row.state.get("preview_image_id")
    new_preview = resolve_preview_image_id(existing_preview, updated)
    await _patch_item_attachments(session, company_id, entity_id, user.id, updated, new_preview)
    await session.commit()
    return att


@router.delete("/{entity_id}/attachments/{att_id}", status_code=204)
async def delete_attachment(
    entity_id: str,
    att_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove one attachment from an item."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    existing: list[dict] = row.state.get("attachments") or []
    updated = remove_attachment(existing, att_id)
    existing_preview: str | None = row.state.get("preview_image_id")
    new_preview = resolve_preview_image_id(existing_preview, updated, removed_id=att_id)
    await _patch_item_attachments(session, company_id, entity_id, user.id, updated, new_preview)
    await session.commit()


@router.put("/{entity_id}/attachments/{att_id}/preview", status_code=200)
async def set_preview_image(
    entity_id: str,
    att_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Explicitly set preview_image_id for an item.

    The referenced attachment must exist and have type == "image".
    Returns {"preview_image_id": att_id}.
    """
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    attachments: list[dict] = row.state.get("attachments") or []
    target = next((a for a in attachments if a["id"] == att_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if target["type"] != "image":
        raise HTTPException(status_code=422, detail="preview_image_id can only be set to an image attachment")

    await _patch_item_attachments(session, company_id, entity_id, user.id, attachments, att_id)
    await session.commit()
    return {"preview_image_id": att_id}


@router.post("/attachments/bulk")
async def bulk_attach_legacy(
    file: UploadFile = File(...),
    override_hero: bool = Query(False),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Deprecated: redirects to /items/files/bulk."""
    return await bulk_attach_files(file=file, override_hero=override_hero, company_id=company_id, user=user, session=session)


def _detect_tag_and_label(stem: str) -> tuple[str, str | None, bool]:
    """Parse a ZIP entry stem into (document_tag, label, is_hero_candidate).

    Markers (checked in priority order, last occurrence wins for ambiguous SKUs):
      -cert-   → certificates
      -spec-   → spec_sheets
      -safety- → safety_docs
      -360-    → view_360
      -img-    → product_images, not hero
      -doc-    → certificates (alias, backward compat)
    Bare SKU (no marker) → product_images, hero candidate
    """
    for marker, tag in (
        ("-cert-", "certificates"),
        ("-spec-", "spec_sheets"),
        ("-safety-", "safety_docs"),
        ("-360-", "view_360"),
        ("-img-", "product_images"),
        ("-doc-", "certificates"),
    ):
        idx = stem.rfind(marker)
        if idx != -1:
            sku_part = stem[:idx]
            label = stem[idx + len(marker):]
            return tag, label, False
    return "product_images", None, True  # bare SKU → hero candidate


@router.post("/files/bulk")
async def bulk_attach_files(
    file: UploadFile = File(...),
    override_hero: bool = Query(False, description="If true, override existing hero image for matched items"),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bulk-attach files from a ZIP archive via item.file.attached events.

    File naming convention inside the ZIP:
      <SKU>.jpg / .png / .webp        — hero product image (first bare image per SKU)
      <SKU>-img-<n>.<ext>             — additional product image (not hero)
      <SKU>-cert-<label>.<ext>        — certificates tag
      <SKU>-doc-<label>.<ext>         — certificates tag (alias for -cert-, backward compat)
      <SKU>-spec-<label>.<ext>        — spec_sheets tag
      <SKU>-safety-<label>.<ext>      — safety_docs tag
      <SKU>-360-<label>.<ext>         — view_360 tag

    Returns:
      {matched, unmatched, errors, report: [{sku, file, status, url, tag, is_hero}]}
    """
    from datetime import datetime, timezone
    import mimetypes as _mt
    from starlette.datastructures import Headers as _Headers

    content = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise HTTPException(status_code=422, detail="Uploaded file is not a valid ZIP archive")

    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()
    sku_index: dict[str, Projection] = {
        str(r.state.get("sku", "")).strip().lower(): r
        for r in rows
        if r.state.get("sku")
    }

    matched = unmatched = 0
    errors: list[str] = []
    report: list[dict] = []
    # Track which SKUs have already had a hero assigned in this batch
    hero_assigned: set[str] = set()

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in sorted(zf.namelist()):  # sorted for deterministic hero selection
            if name.endswith("/") or name.startswith("__") or name.startswith("."):
                continue

            stem = _Path(name).stem
            tag, label, is_hero_candidate = _detect_tag_and_label(stem)

            # SKU is always the stem up to the first marker (or full stem for bare)
            # _detect_tag_and_label returns the full stem as sku for bare files.
            # For marked files it returns sku_part inside the function but we need it here.
            # Re-derive sku_part:
            sku_part = stem
            for marker in ("-cert-", "-spec-", "-safety-", "-360-", "-img-", "-doc-"):
                idx = stem.rfind(marker)
                if idx != -1:
                    sku_part = stem[:idx]
                    break

            sku_key = sku_part.strip().lower()
            row = sku_index.get(sku_key)
            if row is None:
                unmatched += 1
                report.append({"sku": sku_part, "file": name, "status": "unmatched"})
                continue

            try:
                raw = zf.read(name)
                guessed_mime = _mt.guess_type(name)[0] or "application/octet-stream"
                upload = UploadFile(
                    file=io.BytesIO(raw),
                    filename=name.split("/")[-1],
                    headers=_Headers({"content-type": guessed_mime}),
                )
                meta = await store_upload(company_id, upload)

                # Determine is_hero: hero candidate + no hero yet in batch +
                # (no existing hero OR override_hero) + must be image MIME
                is_image = guessed_mime.startswith("image/")
                existing_hero = any(
                    f.get("is_hero") for f in (row.state.get("files") or [])
                )
                is_hero = (
                    is_hero_candidate
                    and is_image
                    and sku_key not in hero_assigned
                    and (not existing_hero or override_hero)
                )
                if is_hero:
                    hero_assigned.add(sku_key)

                await emit_event(
                    session,
                    company_id=company_id,
                    entity_id=row.entity_id,
                    entity_type="item",
                    event_type="item.file.attached",
                    data={
                        "entity_id": row.entity_id,
                        "entity_type": "item",
                        "file_id": meta["id"],
                        "filename": meta["filename"],
                        "mime": meta["mime"],
                        "size": meta["size"],
                        "url": meta.get("url", ""),
                        "document_tag": tag,
                        "description": label,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "is_hero": is_hero,
                    },
                    actor_id=user.id,
                    location_id=None,
                    source="api",
                    idempotency_key=str(uuid.uuid4()),
                    metadata_={},
                )
                # Keep in-memory projection current for subsequent files on same SKU
                from celerp_inventory.projections import apply_item_event as _apply
                row.state = _apply(dict(row.state), "item.file.attached", {
                    "entity_id": row.entity_id, "entity_type": "item",
                    "file_id": meta["id"], "filename": meta["filename"],
                    "mime": meta["mime"], "size": meta["size"],
                    "url": meta.get("url", ""), "document_tag": tag,
                    "description": label, "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "is_hero": is_hero,
                })

                matched += 1
                report.append({"sku": sku_part, "file": name, "status": "ok",
                                "url": meta.get("url", ""), "tag": tag, "is_hero": is_hero})
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                report.append({"sku": sku_part, "file": name, "status": "error", "detail": str(exc)})

    await session.commit()
    return {"matched": matched, "unmatched": unmatched, "errors": errors, "report": report}


# ── New file-event endpoints (Phase 1: unified file system) ──────────────────

def _get_item_file(files: list[dict], file_id: str) -> dict:
    match = next((f for f in files if f.get("id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")
    return match


@router.post("/{entity_id}/files")
async def upload_item_file(
    entity_id: str,
    file: UploadFile = File(...),
    document_tag: str | None = None,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload a file and attach it to an item via item.file.attached event."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    try:
        meta = await store_upload(company_id, file)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    # First image uploaded → auto-set as hero if no hero exists
    existing_files: list[dict] = row.state.get("files", [])
    has_hero = any(f.get("is_hero") for f in existing_files)
    is_hero = (not has_hero) and meta.get("mime", "").startswith("image/")

    from datetime import datetime, timezone
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
            "document_tag": document_tag,
            "description": None,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "is_hero": is_hero,
        },
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"file_id": meta["id"], **meta, "is_hero": is_hero}


@router.post("/{entity_id}/files/{file_id}/tag")
async def tag_item_file(
    entity_id: str,
    file_id: str,
    document_tag: str = Form(""),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _get_item_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.file.tagged",
        data={"entity_id": entity_id, "entity_type": "item", "file_id": file_id, "document_tag": document_tag},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return (updated.state if updated else {}) | {"id": entity_id}


@router.patch("/{entity_id}/files/{file_id}/description")
async def update_item_file_description(
    entity_id: str,
    file_id: str,
    description: str = Form(""),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _get_item_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.file.description_updated",
        data={"entity_id": entity_id, "entity_type": "item", "file_id": file_id, "description": description},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    updated = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    return (updated.state if updated else {}) | {"id": entity_id}


@router.post("/{entity_id}/files/{file_id}/hero")
async def set_item_file_hero(
    entity_id: str,
    file_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark a file as the hero (featured) image. Must be an image MIME type."""
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    target = _get_item_file(row.state.get("files", []), file_id)
    if not target.get("mime", "").startswith("image/"):
        raise HTTPException(status_code=422, detail="Hero can only be set on an image file")
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.file.hero_set",
        data={"entity_id": entity_id, "entity_type": "item", "file_id": file_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()
    return {"preview_image_id": file_id}


@router.delete("/{entity_id}/files/{file_id}", status_code=204)
async def delete_item_file(
    entity_id: str,
    file_id: str,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    _get_item_file(row.state.get("files", []), file_id)
    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.file.deleted",
        data={"entity_id": entity_id, "entity_type": "item", "file_id": file_id},
        actor_id=user.id,
        location_id=None,
        source="api",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()


@router.get("/{entity_id}/files/{file_id}")
async def download_item_file(
    entity_id: str,
    file_id: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
):
    from fastapi.responses import FileResponse
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    # Check new files first, fall back to old attachments
    files = row.state.get("files") or []
    atts = row.state.get("attachments") or []
    match = next((f for f in files + atts if f.get("id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="File not found")
    url = match.get("url", "")
    # If stored as an absolute URL (cloud/S3 backend), redirect directly
    if url.startswith("http://") or url.startswith("https://"):
        from fastapi.responses import RedirectResponse as _Redir
        return _Redir(url)
    from celerp.config import settings as _settings
    dest = _settings.data_dir / url.lstrip("/")
    if not dest.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")
    return FileResponse(
        path=str(dest),
        filename=match["filename"],
        media_type=match.get("mime", "application/octet-stream"),
    )
