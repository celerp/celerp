# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Physical checks: scan an asset's tag (or its number) to record that it was seen, by whom
and on which day, and list the assets not seen since a date.

A check is its own entity (``fa:check:<asset>:<uuid>``), never held on the asset, and is
removed only by its author on the day it was recorded.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.company_lock import lock_projections
from celerp.services.permissions import require_permission

from celerp_fixed_assets import setup
from celerp_fixed_assets.custody import _locations
from celerp_fixed_assets.moves import _GONE
from celerp_fixed_assets.operations import by_tag
from celerp_fixed_assets.store import _emit, _entities, _state

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])
_CHECK = [require_permission("check_fixed_assets")]
ENTITY_TYPE = "fa_check"
_LEFT = {"disposed": "Disposed on {date}.", "sold": "Sold on {date}."}


async def record_check(session, company_id, user_id, tag: str) -> dict:
    """Record that the asset carrying ``tag`` (or numbered ``tag``) was seen today."""
    tag = str(tag or "").strip()
    if not tag:
        raise setup.unprocessable("Scan or type an asset tag.")
    found = by_tag(await _entities(session, company_id, "fa_asset"), tag)
    if found is None:
        raise setup.unprocessable(f"No asset with tag {tag}.")
    asset_id, asset = found
    if asset.get("status") in _LEFT:
        raise setup.unprocessable(_LEFT[asset["status"]].format(date=(asset.get("disposal") or {}).get("date")))
    check_id = f"fa:check:{asset_id.removeprefix('fa:asset:')}:{uuid.uuid4()}"
    data = {"asset_id": asset_id, "number": asset["number"], "user_id": str(user_id),
            "date": (await setup.business_today(session, company_id)).isoformat(), "at": datetime.now(timezone.utc).isoformat()}
    await _emit(session, company_id, user_id, check_id, ENTITY_TYPE, "fa.check.recorded", data)
    await session.commit()
    return {"id": check_id, **data}


async def live_checks(session, company_id) -> list[dict]:
    """The checks still standing, newest first."""
    rows = [{"id": cid, **s} for cid, s in await _entities(session, company_id, ENTITY_TYPE) if not s.get("removed")]
    return sorted(rows, key=lambda c: c["at"], reverse=True)


async def not_seen(session, company_id, since: str) -> list[dict]:
    """The assets on the books with no check on or after ``since``, each with the day it was
    last seen (None when never)."""
    try:
        since = date.fromisoformat(str(since)).isoformat()
    except ValueError:
        raise setup.unprocessable("Choose the date to report from.") from None
    last: dict[str, str] = {}
    for check in await live_checks(session, company_id):
        last[check["asset_id"]] = max(last.get(check["asset_id"], ""), check["date"])
    locations = await _locations(session, company_id)
    rows = [{"id": a, "number": s["number"], "tag": s.get("tag"), "name": s.get("name"),
             "location": locations.get(str(s.get("location_id"))), "last_seen": last.get(a)}
            for a, s in await _entities(session, company_id, "fa_asset")
            if s.get("number") and s.get("status") not in _GONE and last.get(a, "") < since]
    return sorted(rows, key=lambda r: r["number"], reverse=True)


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/checks", dependencies=_CHECK)
async def create_check(body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await record_check(session, company_id, user.id, body.get("tag"))


@router.get("/checks", dependencies=_CHECK)
async def list_checks(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await live_checks(session, company_id)}


@router.get("/checks/not-seen", dependencies=_CHECK)
async def report_not_seen(since: str, company_id=Depends(get_current_company_id),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return {"since": since, "items": await not_seen(session, company_id, since)}


@router.delete("/checks/{check_id}", dependencies=_CHECK)
async def remove_check(check_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)) -> dict:
    """Its author removes a check recorded by mistake, on the day it was recorded."""
    row = (await lock_projections(session, company_id, [check_id])).get(check_id)
    if row is None or row.entity_type != ENTITY_TYPE or row.state.get("removed"):
        raise HTTPException(status_code=404, detail="No such check.")
    if row.state["user_id"] != str(user.id):
        raise HTTPException(status_code=403, detail="Only the person who recorded a check can remove it.")
    if row.state["date"] != (await setup.business_today(session, company_id)).isoformat():
        raise setup.unprocessable(f"This check was recorded on {row.state['date']}. A check can only be removed on "
                                  "the day it was recorded.")
    await _emit(session, company_id, user.id, check_id, ENTITY_TYPE, "fa.check.removed", {"user_id": str(user.id)})
    await session.commit()
    return {"id": check_id, **(await _state(session, company_id, check_id) or {})}
