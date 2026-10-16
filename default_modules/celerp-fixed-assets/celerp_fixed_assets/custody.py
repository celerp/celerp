# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""My assets: what a member holds as custodian, whatever their role.

A custodian sees the assets in their care without money, sources or journals, and only
the photos and manuals on them. They confirm they still hold an asset, and report a
problem with it, which tells the asset's responsible person (else the whole company).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.services.auth import get_current_company_id, get_current_user

from celerp_fixed_assets import files, setup
from celerp_fixed_assets.moves import _GONE, _locked_assets
from celerp_fixed_assets.store import _emit, _entities, _state

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])
# What a custodian sees of an asset in their care.
SHOWN = ("number", "tag", "name", "description", "serial", "make", "model", "location_id", "custodian",
         "confirmed", "problems")


def held_by(state: dict | None, user_id) -> bool:
    """Whether the member holds the asset as its custodian, and it is still on the books."""
    return bool(state) and state.get("status") not in _GONE \
        and (state.get("custodian") or {}).get("user_id") == str(user_id)


async def _locations(session, company_id) -> dict[str, str]:
    from sqlalchemy import select

    from celerp.models.company import Location

    return {str(i): n for i, n in (await session.execute(
        select(Location.id, Location.name).where(Location.company_id == company_id))).all()}


def shown(asset_id: str, state: dict, locations: dict[str, str]) -> dict:
    return {"id": asset_id, **{k: state.get(k) for k in SHOWN},
            "location": locations.get(str(state.get("location_id"))),
            "files": [f for f in state.get("files") or [] if f.get("document_tag") in files.CUSTODIAN_TAGS]}


async def mine(session, company_id, user_id) -> list[tuple[str, dict]]:
    """The assets in the member's care, newest first."""
    held = [(a, s) for a, s in await _entities(session, company_id, "fa_asset") if held_by(s, user_id)]
    return sorted(held, key=lambda r: r[1].get("number") or "", reverse=True)


async def held(session, company_id, user_id, asset_id: str) -> dict:
    state = await _state(session, company_id, asset_id)
    if not held_by(state, user_id):
        raise HTTPException(status_code=404, detail="No such asset in your care.")
    return state


async def _locked_held(session, company_id, user_id, asset_id: str) -> dict:
    ((_, state),) = await _locked_assets(session, company_id, [asset_id])
    if not held_by(state, user_id):
        raise HTTPException(status_code=404, detail="No such asset in your care.")
    return state


async def _today(session, company_id) -> str:
    from celerp.services.business_time import business_date_of

    return business_date_of(None, (await setup.company_settings(session, company_id)).get("timezone"))


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/mine")
async def list_mine(company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)) -> dict:
    locations = await _locations(session, company_id)
    return {"items": [shown(a, s, locations) for a, s in await mine(session, company_id, user.id)]}


@router.get("/mine/{asset_id}")
async def get_mine(asset_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    state = await held(session, company_id, user.id, asset_id)
    return shown(asset_id, state, await _locations(session, company_id))


@router.get("/mine/{asset_id}/files/{file_id}")
async def download_mine(asset_id: str, file_id: str, company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    match = files.file_of(await held(session, company_id, user.id, asset_id), file_id)
    if match.get("document_tag") not in files.CUSTODIAN_TAGS:
        raise HTTPException(status_code=404, detail="No such file on this asset.")
    return files.serve(company_id, match)


@router.post("/mine/{asset_id}/confirm")
async def confirm(asset_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)) -> dict:
    """The custodian confirms they still hold the asset, today."""
    await _locked_held(session, company_id, user.id, asset_id)
    await _emit(session, company_id, user.id, asset_id, "fa_asset", "fa.asset.confirmed",
                {"confirmed": {"user_id": str(user.id), "date": await _today(session, company_id)}})
    await session.commit()
    return shown(asset_id, await _state(session, company_id, asset_id), await _locations(session, company_id))


@router.post("/mine/{asset_id}/problems")
async def report_problem(asset_id: str, text: str = Form(""), photo: UploadFile | None = None,
                         company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)) -> dict:
    """The custodian reports a problem, with a photo if they have one; the asset's responsible
    person is told, or the whole company when no one is responsible."""
    from celerp.notifications.service import create_keyed

    text = text.strip()
    if not text:
        raise setup.unprocessable("Describe the problem.")

    async def record(state: dict) -> None:
        if not held_by(state, user.id):
            raise HTTPException(status_code=404, detail="No such asset in your care.")
        problem = {"id": str(uuid.uuid4()), "text": text, "user_id": str(user.id),
                   "date": await _today(session, company_id)}
        await _emit(session, company_id, user.id, asset_id, "fa_asset", "fa.asset.problem", {"problem": problem})
        responsible = state.get("responsible_user_id")
        await create_keyed(session, company_id, "fixed_assets", "fixed_assets.notice.problem",
                           {"number": state["number"], "text": text},
                           user_id=uuid.UUID(responsible) if responsible else None,
                           action_url=f"/fixed-assets/assets/{asset_id}")

    if photo is not None and photo.filename:
        await files.store_file(session, company_id, user.id, asset_id, photo, "photos", also=record)
    else:
        await record(await _locked_held(session, company_id, user.id, asset_id))
        await session.commit()
    return shown(asset_id, await _state(session, company_id, asset_id), await _locations(session, company_id))
