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

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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
async def bulk_attach(
    file: UploadFile = File(...),
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bulk-attach files from a ZIP archive.

    File naming convention inside the ZIP:
      <SKU>.<ext>                    — type inferred from MIME
      <SKU>-cert-<label>.<ext>       — explicitly typed as certificate
      <SKU>-360-<label>.<ext>        — explicitly typed as view_360
      <SKU>-doc-<label>.<ext>        — alias for certificate (backward compat)

    Returns:
      {matched: int, unmatched: int, errors: [...], report: [{sku, file, status}]}
    """
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

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.endswith("/") or name.startswith("__") or name.startswith("."):
                continue

            stem = _Path(name).stem
            label: str | None = None
            explicit_type: AttachmentType | None = None

            # Detect typed suffix: <SKU>-cert-<label>, <SKU>-360-<label>, <SKU>-doc-<label>
            # Split on last occurrence so SKUs containing the marker string still work.
            for marker, atype in (("-cert-", "certificate"), ("-360-", "view_360"), ("-doc-", "certificate")):
                idx = stem.rfind(marker)
                if idx != -1:
                    sku_part = stem[:idx]
                    label = stem[idx + len(marker):]
                    explicit_type = atype  # type: ignore[assignment]
                    break
            else:
                sku_part = stem

            sku_key = sku_part.strip().lower()
            row = sku_index.get(sku_key)
            if row is None:
                unmatched += 1
                report.append({"sku": sku_part, "file": name, "status": "unmatched"})
                continue

            try:
                raw = zf.read(name)
                import mimetypes as _mt
                from starlette.datastructures import Headers as _Headers
                guessed_mime = _mt.guess_type(name)[0] or "application/octet-stream"
                upload = UploadFile(
                    file=io.BytesIO(raw),
                    filename=name.split("/")[-1],
                    headers=_Headers({"content-type": guessed_mime}),
                )
                att = await store_upload(company_id, upload, attachment_type=explicit_type)
                if label:
                    att["label"] = label

                existing: list[dict] = row.state.get("attachments") or []
                updated = merge_attachments(existing, att)
                existing_preview: str | None = row.state.get("preview_image_id")
                new_preview = resolve_preview_image_id(existing_preview, updated)
                await _patch_item_attachments(session, company_id, row.entity_id, user.id, updated, new_preview)
                # Keep projection in sync for subsequent files in same SKU
                row.state = row.state | {"attachments": updated, "preview_image_id": new_preview}

                matched += 1
                report.append({"sku": sku_part, "file": name, "status": "ok", "url": att["url"]})
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                report.append({"sku": sku_part, "file": name, "status": "error", "detail": str(exc)})

    await session.commit()
    return {"matched": matched, "unmatched": unmatched, "errors": errors, "report": report}
