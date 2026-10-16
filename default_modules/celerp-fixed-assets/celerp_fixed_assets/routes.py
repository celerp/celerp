# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixed assets API, under /fixed-assets/api.

Every write takes an operation key from its form and is previewed before it is
confirmed (celerp_fixed_assets.operations). Amounts are shown only to roles that may
see financial reports.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import (
    assert_role_permission, get_current_company_settings, require_permission, role_has_permission)
from celerp_fixed_assets import corrections, operations, runs, setup, sources
from celerp_fixed_assets.events import register_events

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])

# Journal operation types this module posts, and the permission each needs.
OPERATION_TYPES = {name: ("manage_accounting",)
                   for name in ("registration", "acquisition", "expense", "maintenance", "undo", "run", "correction")}

_MONEY_FIELDS = ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus", "nbv")
_VIEW = [require_permission("view_fixed_assets")]
_ACCOUNTING = [require_permission("manage_accounting")]


def _shows_money(settings: dict, role: str) -> bool:
    return role_has_permission(settings, role, "view_financial_reports")


def asset_out(asset_id: str, state: dict, *, money: bool = True) -> dict:
    """An asset as the API shows it: its figures, and its net book value."""
    out = {"id": asset_id, **{k: v for k, v in state.items() if k not in ("op_key", "movement_count", "version")}}
    if state.get("status") == "physical":
        out.update(dict.fromkeys(_MONEY_FIELDS))
    else:
        out["nbv"] = str(Decimal(str(state.get("cost") or 0)) - Decimal(str(state.get("accumulated_depreciation") or 0))
                         - Decimal(str(state.get("accumulated_impairment") or 0)))
    if not money:
        out.update(dict.fromkeys(_MONEY_FIELDS))
        out["sources"] = [{"source": s.get("source"), "ref": s.get("ref")} for s in out.get("sources") or []]
        if out.get("exception"):
            out["exception"] = {**out["exception"], "amount": None}
    return out


async def _asset(session, company_id, asset_id: str) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": asset_id})
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    return row.state


# ── Setup ────────────────────────────────────────────────────────────────────


@router.get("/setup", dependencies=_VIEW)
async def get_setup(company_settings: dict = Depends(get_current_company_settings)) -> dict:
    fa = setup.settings_of(company_settings)
    return {"started": bool(fa.get("started")), "settings": setup.view(fa)}


@router.post("/start", dependencies=_ACCOUNTING)
async def start(company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.start(session, company_id, user.id)
    await session.commit()
    return result


@router.patch("/settings", dependencies=_ACCOUNTING)
async def update_settings(body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.update_settings(session, company_id, user.id, body)
    await session.commit()
    return result


@router.get("/classes", dependencies=_VIEW)
async def list_classes(include_archived: bool = False, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await setup.classes(session, company_id, include_archived=include_archived)}


@router.post("/classes", dependencies=_ACCOUNTING)
async def create_class(body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.create_class(session, company_id, user.id, body)
    await session.commit()
    return result


@router.patch("/classes/{class_id}", dependencies=_ACCOUNTING)
async def update_class(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.update_class(session, company_id, user.id, class_id, body)
    await session.commit()
    return result


# ── Source lines ─────────────────────────────────────────────────────────────


@router.get("/intake", dependencies=_ACCOUNTING)
async def intake(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await sources.intake(session, company_id)}


async def _write(session, call, *args) -> dict:
    result = await call(session, *args)
    await session.commit()
    return result


@router.post("/register", dependencies=_ACCOUNTING)
async def register(body: dict = Body(...), company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.register, company_id, user.id, body)


@router.post("/not-acquisition", dependencies=_ACCOUNTING)
async def not_acquisition(body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.not_acquisition, company_id, user.id, body)


@router.post("/link-depreciation", dependencies=_ACCOUNTING)
async def link_depreciation(body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.link_depreciation, company_id, user.id, body)


@router.post("/sources/undo", dependencies=_ACCOUNTING)
async def undo_source(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.undo_source, company_id, user.id, body)


# ── Assets ───────────────────────────────────────────────────────────────────


@router.get("/assets", dependencies=_VIEW)
async def list_assets(class_id: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0,
                      company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                      company_settings: dict = Depends(get_current_company_settings),
                      session: AsyncSession = Depends(get_session)) -> dict:
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.unprocessable("Choose a page of 1 to 500 assets.")
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_asset"))).all()
    needle = (q or "").strip().lower()
    found = [(eid, s) for eid, s in rows if s.get("status") != "undone"
             and (not class_id or s.get("class_id") == class_id)
             and (not needle or any(needle in str(s.get(k) or "").lower() for k in ("number", "name", "serial")))]
    found.sort(key=lambda r: r[1].get("number") or "", reverse=True)
    money = _shows_money(company_settings, role)
    return {"items": [asset_out(eid, s, money=money) for eid, s in found[offset:offset + limit]],
            "total": len(found), "limit": limit, "offset": offset}


@router.post("/assets", dependencies=[require_permission("manage_fixed_assets")])
async def create_asset(body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), role: str = Depends(get_current_role),
                       company_settings: dict = Depends(get_current_company_settings),
                       session: AsyncSession = Depends(get_session)) -> dict:
    # A physical record posts nothing; an opening or contributed asset posts its cost.
    if body.get("kind") != "physical":
        assert_role_permission(company_settings, role, "manage_accounting")
    return await _write(session, operations.create_asset, company_id, user.id, body)


@router.get("/assets/{asset_id}", dependencies=_VIEW)
async def get_asset(asset_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                    company_settings: dict = Depends(get_current_company_settings),
                    session: AsyncSession = Depends(get_session)) -> dict:
    return asset_out(asset_id, await _asset(session, company_id, asset_id),
                     money=_shows_money(company_settings, role))


@router.patch("/assets/{asset_id}", dependencies=[require_permission("manage_fixed_assets")])
async def update_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), role: str = Depends(get_current_role),
                       company_settings: dict = Depends(get_current_company_settings),
                       session: AsyncSession = Depends(get_session)) -> dict:
    state = await _write(session, operations.update_asset, company_id, user.id, asset_id, body)
    return asset_out(asset_id, state, money=_shows_money(company_settings, role))


@router.post("/assets/{asset_id}/undo", dependencies=_ACCOUNTING)
async def undo_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.undo_asset, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/financial-details", dependencies=_ACCOUNTING)
async def financial_details(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.add_financial_details, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/movements", dependencies=_VIEW)
async def list_movements(asset_id: str, limit: int = 50, offset: int = 0,
                         company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                         company_settings: dict = Depends(get_current_company_settings),
                         session: AsyncSession = Depends(get_session)) -> dict:
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.unprocessable("Choose a page of 1 to 500 movements.")
    await _asset(session, company_id, asset_id)
    items = sorted(await operations.asset_movements(session, company_id, asset_id),
                   key=lambda m: (m.get("posting_date") or "", int(m["id"].rsplit(":", 1)[1])), reverse=True)
    if not _shows_money(company_settings, role):
        items = [{**m, "amounts": None, "contributions": None} for m in items]
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}


# ── Depreciation runs ────────────────────────────────────────────────────────


@router.get("/runs", dependencies=_VIEW)
async def list_runs(company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                    company_settings: dict = Depends(get_current_company_settings),
                    session: AsyncSession = Depends(get_session)) -> dict:
    items = await runs.records(session, company_id)
    if not _shows_money(company_settings, role):
        items = [{**r, "total": None, "exceptions": [{**e, "amount": None} for e in r.get("exceptions") or []]}
                 for r in items]
    return {"items": items}


@router.post("/runs", dependencies=_ACCOUNTING)
async def run(body: dict = Body(...), company_id=Depends(get_current_company_id), user=Depends(get_current_user),
              session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.run, company_id, user.id, body)


@router.post("/runs/{month}/reverse", dependencies=_ACCOUNTING)
async def reverse_run(month: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.reverse, company_id, user.id, month, body)


@router.get("/attention", dependencies=_VIEW)
async def attention(company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                    company_settings: dict = Depends(get_current_company_settings),
                    session: AsyncSession = Depends(get_session)) -> dict:
    items = await runs.attention(session, company_id)
    if not _shows_money(company_settings, role):
        items = [{**a, "exception": {**a["exception"], "amount": None}} for a in items]
    return {"items": items}


@router.post("/assets/{asset_id}/readings", dependencies=[require_permission("manage_fixed_assets")])
async def record_reading(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.record_reading, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/skip", dependencies=_ACCOUNTING)
async def skip_month(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.skip_month, company_id, user.id, asset_id, body)


@router.delete("/assets/{asset_id}/skip/{month}", dependencies=_ACCOUNTING)
async def withdraw_skip(asset_id: str, month: str, company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.withdraw_skip, company_id, user.id, asset_id, month)


# ── Corrections ──────────────────────────────────────────────────────────────


@router.get("/assets/{asset_id}/correct-fields", dependencies=_VIEW)
async def correct_fields(asset_id: str, company_id=Depends(get_current_company_id),
                         session: AsyncSession = Depends(get_session)) -> dict:
    return await corrections.fields(session, company_id, asset_id)


@router.post("/assets/{asset_id}/correct", dependencies=_ACCOUNTING)
async def correct(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                  user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.correct, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/corrections", dependencies=[require_permission("manage_fixed_assets")])
async def request_correction(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                             user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.request, company_id, user.id, asset_id, body)


@router.get("/corrections", dependencies=_VIEW)
async def list_corrections(asset_id: str | None = None, company_id=Depends(get_current_company_id),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await corrections.records(session, company_id, asset_id)}


@router.get("/corrections/{correction_id}", dependencies=_VIEW)
async def get_correction(correction_id: str, company_id=Depends(get_current_company_id),
                         session: AsyncSession = Depends(get_session)) -> dict:
    return {"id": correction_id, **await corrections.record(session, company_id, correction_id)}


@router.post("/corrections/{correction_id}/apply", dependencies=_ACCOUNTING)
async def apply_correction(correction_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.apply_request, company_id, user.id, correction_id, body)


@router.post("/corrections/{correction_id}/withdraw", dependencies=[require_permission("manage_fixed_assets")])
async def withdraw_correction(correction_id: str, company_id=Depends(get_current_company_id),
                              user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.withdraw, company_id, user.id, correction_id)


@router.post("/corrections/{correction_id}/undo", dependencies=_ACCOUNTING)
async def undo_correction(correction_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.undo, company_id, user.id, correction_id, body)


@router.post("/classes/{class_id}/apply-to-assets", dependencies=_ACCOUNTING)
async def apply_to_assets(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.apply_to_assets, company_id, user.id, class_id, body)


@router.get("/reports/prior-period-adjustments", dependencies=[require_permission("view_financial_reports")])
async def prior_period_adjustments(year: int, company_id=Depends(get_current_company_id),
                                   session: AsyncSession = Depends(get_session)) -> dict:
    return await corrections.prior_period_report(session, company_id, year)


def setup_api_routes(app) -> None:
    """Called by the module loader: the event types, the journal operation types, the routes."""
    from celerp.services.module_journals import register_operation_types

    register_events()
    register_operation_types("celerp-fixed-assets", OPERATION_TYPES, source_prefix="fa:")
    app.include_router(router)
