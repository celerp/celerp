# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Files on an asset: photos, manuals, warranties and the like, each tagged and described.

The bill or invoice an asset came from, or was sold on, keeps its own files; they are
listed with the asset's only to roles that see money (view_financial_reports), since a
bill shows what was paid.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.services.attachments import attach_file, local_attachment_url_path, storing
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import get_current_company_settings, require_permission, role_has_permission

from celerp_fixed_assets import setup
from celerp_fixed_assets.moves import _locked_assets
from celerp_fixed_assets.store import _emit, _state

ENTITY_TYPE = "fa_asset"
ATTACHED = "fa.asset.file_attached"
# The tags an asset's files carry; the order is the order offered.
ASSET_FILE_TAGS = ("bills", "photos", "warranties", "manuals", "insurance", "maintenance", "registrations",
                   "disposal", "other")
# What a custodian sees of the files on the assets in their care.
CUSTODIAN_TAGS = ("photos", "manuals")
MANUAL_PREFIX = "je:manual:"

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])
_VIEW = [require_permission("view_fixed_assets")]
_MANAGE = [require_permission("manage_fixed_assets")]


def file_of(state: dict, file_id: str) -> dict:
    match = next((f for f in state.get("files") or [] if f.get("id") == file_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="No such file on this asset.")
    return match


def checked_tag(tag: str | None) -> str | None:
    """A tag an asset's file may carry, or None for no tag; anything else is refused by name."""
    tag = (tag or "").strip() or None
    if tag is not None and tag not in ASSET_FILE_TAGS:
        raise setup.unprocessable(f"An asset's file cannot be tagged {tag}. Choose one of: {', '.join(ASSET_FILE_TAGS)}.")
    return tag


def serve(company_id, match: dict):
    """The stored file itself: a redirect for a file kept in cloud storage, else the local file."""
    url = match.get("url") or ""
    if url.startswith(("http://", "https://")):
        return RedirectResponse(url)
    dest = local_attachment_url_path(str(company_id), url)
    if dest is None or not dest.exists():
        raise HTTPException(status_code=404, detail="This file is no longer stored.")
    return FileResponse(path=str(dest), filename=match["filename"],
                        media_type=match.get("mime") or "application/octet-stream")


async def store_file(session, company_id, user_id, asset_id: str, file: UploadFile, tag: str | None,
                     *, also=None) -> dict:
    """Store an upload and attach it to the asset in one unit. ``also(state)`` runs on the
    locked asset before the file is attached and in the same unit, so its refusal leaves no
    stored file behind and what it records lands only with the file."""
    async with storing(session, company_id) as store:
        try:
            meta = await store.upload(file)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None
        ((_, state),) = await _locked_assets(session, company_id, [asset_id])
        if also is not None:
            await also(state)
        await attach_file(session, company_id, ENTITY_TYPE, asset_id, meta, user_id, source="fixed_assets",
                          document_tag=tag)
    return meta


async def _locked_file(session, company_id, asset_id: str, file_id: str) -> dict:
    ((_, state),) = await _locked_assets(session, company_id, [asset_id])
    return file_of(state, file_id)


async def _record(session, company_id, user_id, asset_id: str, event_type: str, data: dict) -> dict:
    await _emit(session, company_id, user_id, asset_id, ENTITY_TYPE, event_type,
                {"entity_id": asset_id, "entity_type": ENTITY_TYPE, **data})
    await session.commit()
    return {"id": asset_id, "files": (await _state(session, company_id, asset_id) or {}).get("files") or []}


async def linked(session, company_id, state: dict) -> list[dict]:
    """The files on the documents behind an asset: the bills its cost came from and its sale
    invoice. Each carries the document it sits on and that document's number."""
    from celerp_fixed_assets.sales import doc_number

    documents = [s["source"].rsplit(":", 1)[0] for s in state.get("sources") or [] if s.get("source")]
    if (state.get("sale") or {}).get("invoice_id"):
        documents.append(state["sale"]["invoice_id"])
    out = []
    for doc_id in dict.fromkeys(d for d in documents if not d.startswith(MANUAL_PREFIX)):
        doc = await _state(session, company_id, doc_id)
        for f in (doc or {}).get("files") or []:
            out.append({**f, "document": doc_id, "document_ref": doc_number(doc, doc_id)})
    return out


# ── Routes ───────────────────────────────────────────────────────────────────


async def _asset_state(session, company_id, asset_id: str) -> dict:
    state = await _state(session, company_id, asset_id)
    if state is None or not state.get("number"):
        raise HTTPException(status_code=404, detail="No such asset.")
    return state


@router.get("/assets/{asset_id}/files", dependencies=_VIEW)
async def list_files(asset_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                     company_settings: dict = Depends(get_current_company_settings),
                     session: AsyncSession = Depends(get_session)) -> dict:
    state = await _asset_state(session, company_id, asset_id)
    money = role_has_permission(company_settings, role, "view_financial_reports")
    return {"files": state.get("files") or [], "linked": await linked(session, company_id, state) if money else [],
            "tags": list(ASSET_FILE_TAGS)}


@router.post("/assets/{asset_id}/files", dependencies=_MANAGE)
async def upload_file(asset_id: str, file: UploadFile, document_tag: str = Form(""),
                      company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)) -> dict:
    tag = checked_tag(document_tag)
    return await store_file(session, company_id, user.id, asset_id, file, tag)


@router.post("/assets/{asset_id}/files/{file_id}/tag", dependencies=_MANAGE)
async def tag_file(asset_id: str, file_id: str, document_tag: str = Form(""),
                   company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    tag = checked_tag(document_tag)
    f = await _locked_file(session, company_id, asset_id, file_id)
    return await _record(session, company_id, user.id, asset_id, "fa.asset.file_tagged",
                         {"file_id": file_id, "document_tag": tag or "", "filename": f.get("filename")})


@router.patch("/assets/{asset_id}/files/{file_id}/description", dependencies=_MANAGE)
async def describe_file(asset_id: str, file_id: str, description: str = Form(""),
                        company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> dict:
    f = await _locked_file(session, company_id, asset_id, file_id)
    return await _record(session, company_id, user.id, asset_id, "fa.asset.file_described",
                         {"file_id": file_id, "description": description.strip(), "filename": f.get("filename")})


@router.get("/assets/{asset_id}/files/{file_id}", dependencies=_VIEW)
async def download_file(asset_id: str, file_id: str, company_id=Depends(get_current_company_id),
                        session: AsyncSession = Depends(get_session)):
    return serve(company_id, file_of(await _asset_state(session, company_id, asset_id), file_id))


@router.delete("/assets/{asset_id}/files/{file_id}", dependencies=_MANAGE)
async def delete_file(asset_id: str, file_id: str, company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    f = await _locked_file(session, company_id, asset_id, file_id)
    return await _record(session, company_id, user.id, asset_id, "fa.asset.file_deleted",
                         {"file_id": file_id, "filename": f.get("filename")})
