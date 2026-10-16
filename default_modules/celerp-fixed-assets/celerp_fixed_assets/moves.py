# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Moving assets: where an asset is and who holds it.

A move posts nothing and leaves the figures alone. Each move is kept on the asset as a
placement with the values it replaced, so the latest one can be undone.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException

from celerp_fixed_assets import setup
from celerp_fixed_assets.operations import _date, _effects, _run, conflict
from celerp_fixed_assets.store import _emit

KEYS = ("manage_fixed_assets",)
# An asset that has left the books stays where it was last placed.
_GONE = ("disposed", "sold", "undone")


async def _location(session, company_id, value) -> dict | None:
    from celerp.models.company import Location

    if value in (None, ""):
        return None
    try:
        loc = await session.get(Location, uuid.UUID(str(value)))
    except ValueError:
        loc = None
    if loc is None or loc.company_id != company_id:
        raise setup.unprocessable("Choose one of this company's locations.")
    return {"id": str(loc.id), "name": loc.name}


async def member_name(session, company_id, user_id) -> str | None:
    """The name of an active member of the company, or None when ``user_id`` is not one."""
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp.models.company import User

    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    return (await session.execute(select(User.name).join(UserCompany, UserCompany.user_id == User.id).where(
        User.id == uid, UserCompany.company_id == company_id, UserCompany.is_active.is_(True)))).scalar()


async def _custodian(session, company_id, value) -> dict | None:
    """Who holds the asset: a member of the company, or a person named here who is not a user."""
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise setup.unprocessable("Choose the custodian.")
    if value.get("user_id"):
        name = await member_name(session, company_id, value["user_id"])
        if not name:
            raise setup.unprocessable("The custodian is not a member of this company. Choose a member, or name a "
                                      "person who is not a user.")
        return {"user_id": str(uuid.UUID(str(value["user_id"]))), "name": name}
    name = str(value.get("name") or "").strip()
    if not name:
        raise setup.unprocessable("Name the custodian.")
    return {"name": name, "not_user": True}


async def _locked_assets(session, company_id, asset_ids) -> list[tuple[str, dict]]:
    from celerp.services.company_lock import lock_projections

    ids = list(dict.fromkeys(str(a) for a in asset_ids or []))
    if not ids:
        raise setup.unprocessable("Choose the assets to move.")
    rows = await lock_projections(session, company_id, ids)
    out = []
    for asset_id in ids:
        row = rows.get(asset_id)
        if row is None or row.entity_type != "fa_asset":
            raise HTTPException(status_code=404, detail="No such asset.")
        out.append((asset_id, row.state))
    return out


async def move(session, company_id, user_id, body: dict, asset_ids=None) -> dict:
    """Move one or more assets to a location, to a custodian, or both, from a date."""

    async def plan(fa):
        day = _date(body.get("date"), "The move date")
        location = await _location(session, company_id, body.get("location_id"))
        custodian = await _custodian(session, company_id, body.get("custodian"))
        if location is None and custodian is None:
            raise setup.unprocessable("Choose a location or a custodian to move to.")
        assets = await _locked_assets(session, company_id, asset_ids if asset_ids is not None else body.get("asset_ids"))
        for _, asset in assets:
            if asset.get("status") in _GONE:
                raise setup.unprocessable(f"{asset['number']} is no longer on the books, so it cannot be moved.")
            last = (asset.get("placements") or [{}])[-1].get("date")
            if last and day < last:
                raise setup.unprocessable(f"{asset['number']} was last moved on {last}. Date the move on or after it.")
        shown = {"assets": [{"id": a, "number": s["number"], "name": s.get("name")} for a, s in assets],
                 "date": day, "location": location, "custodian": custodian}
        return shown, {"assets": assets, "day": day, "location": location, "custodian": custodian,
                       "basis": [s.get("version") for _, s in assets]}

    async def apply(context, op_key):
        for asset_id, asset in context["assets"]:
            previous = {"location_id": asset.get("location_id"), "custodian": asset.get("custodian")}
            changes = {k: v for k, v in (("location_id", (context["location"] or {}).get("id")),
                                         ("custodian", context["custodian"])) if v is not None}
            placement = {"date": context["day"], **previous, **changes, "previous": previous, "op_key": op_key}
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {
                "changes": {**changes, "placements": [*(asset.get("placements") or []), placement]}})
        return ({"assets": [{"id": a, "number": s["number"]} for a, s in context["assets"]]},
                _effects())

    return await _run(session, company_id, user_id, body, op_type="move", keys=KEYS, plan=plan, apply=apply,
                      managed=False)


async def undo_move(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Put an asset back where it was, and with whom, before its latest move."""

    async def plan(fa):
        ((_, asset),) = await _locked_assets(session, company_id, [asset_id])
        placements = asset.get("placements") or []
        if not placements:
            raise setup.unprocessable(f"{asset['number']} has not been moved.")
        if asset.get("status") in _GONE:
            raise conflict(f"{asset['number']} is no longer on the books, so its move cannot be undone.")
        latest = placements[-1]
        shown = {"assets": [{"id": asset_id, "number": asset["number"]}], "date": latest["date"],
                 **latest["previous"]}
        return shown, {"asset": asset, "basis": asset.get("version")}

    async def apply(context, op_key):
        asset = context["asset"]
        placements = asset["placements"]
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {
            "changes": {**placements[-1]["previous"], "placements": placements[:-1]}})
        return {"assets": [{"id": asset_id, "number": asset["number"]}]}, _effects()

    return await _run(session, company_id, user_id, body, op_type="move_undo", keys=KEYS, plan=plan, apply=apply,
                      managed=False)
