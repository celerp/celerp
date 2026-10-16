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
from celerp_fixed_assets import (
    additions, classtable, corrections, disposal, fixaccount, moves, opening, operations, recon, reclassify, remeasure, reports,
    runs, sales, setup, sources, taxbooks, transfer, yearend)
from celerp_fixed_assets.events import register_events
from celerp_fixed_assets.search import matching

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])

# The journals a sale invoice or its credit notes post: a linked document posts them even
# while the module is off.
SALE_OPERATIONS = ("sale", "sale_adjust", "sale_return", "sale_reverse")
# Journal operation types this module posts, and the permission each needs.
OPERATION_TYPES = {name: ("manage_accounting",)
                   for name in ("registration", "acquisition", "expense", "maintenance", "undo", "run", "correction",
                                "disposal", "improvement", "replacement", "place_in_service", "revaluation",
                                "impairment", "impairment_reversal", "reclassify", "transfer_out",
                                "transfer_in", "fix_account")} | dict.fromkeys(SALE_OPERATIONS, sales.SALE_KEYS)

_MONEY_FIELDS = ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus", "nbv")
_VIEW = [require_permission("view_fixed_assets")]
_ACCOUNTING = [require_permission("manage_accounting")]
_MANAGE = [require_permission("manage_fixed_assets")]
_REPORTS = [require_permission("view_financial_reports")]
_SELL = [require_permission("edit_documents"), require_permission("manage_fixed_assets")]


def _shows_money(settings: dict, role: str) -> bool:
    return role_has_permission(settings, role, "view_financial_reports")


def asset_out(asset_id: str, state: dict, *, money: bool = True) -> dict:
    """An asset as the API shows it: its figures, and its net book value."""
    out = {"id": asset_id, "files": [],
           **{k: v for k, v in state.items() if k not in ("op_key", "movement_count", "version")}}
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
async def get_setup(company_settings: dict = Depends(get_current_company_settings),
                    company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    fa = setup.settings_of(company_settings)
    first = await setup.first_period(session, company_id, fa) if fa.get("started") else None
    return {"started": bool(fa.get("started")), "settings": setup.view(fa), "first_period": first,
            "rules": setup.rules_view(company_settings)}


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
    return await _write(session, operations.disposition, company_id, user.id, body, "not_acquisition")


@router.post("/keep-line", dependencies=_ACCOUNTING)
async def keep_line(body: dict = Body(...), company_id=Depends(get_current_company_id),
                    user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.disposition, company_id, user.id, body, "keep")


@router.post("/link-depreciation", dependencies=_ACCOUNTING)
async def link_depreciation(body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.link_depreciation, company_id, user.id, body)


@router.post("/sources/undo", dependencies=_ACCOUNTING)
async def undo_source(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.undo_source, company_id, user.id, body)


# ── Opening balances and the reconciliation ──────────────────────────────────


@router.get("/opening", dependencies=_ACCOUNTING)
async def get_opening(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return await opening.view(session, company_id)


@router.post("/opening/finish", dependencies=_ACCOUNTING)
async def finish_opening(body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, opening.finish, company_id, user.id, body)


@router.get("/reconciliation", dependencies=_ACCOUNTING)
async def get_reconciliation(at: str | None = None, company_id=Depends(get_current_company_id),
                             session: AsyncSession = Depends(get_session)) -> dict:
    return await recon.report(session, company_id, at)


@router.post("/reconciliation/acknowledge", dependencies=_ACCOUNTING)
async def acknowledge(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, recon.acknowledge, company_id, user.id, body)


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
    found = [(eid, s) for eid, s in matching(rows, q) if not class_id or s.get("class_id") == class_id]
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
    state = await _asset(session, company_id, asset_id)
    return {**asset_out(asset_id, state, money=_shows_money(company_settings, role)),
            **await remeasure.asset_facts(session, company_id, asset_id, state),
            "undoable": await additions.undoable(session, company_id, asset_id)}


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


@router.post("/assets/{asset_id}/dispose", dependencies=_ACCOUNTING)
async def dispose_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.dispose, company_id, user.id, body, [asset_id])


@router.post("/dispose", dependencies=_ACCOUNTING)
async def dispose_assets(body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.dispose, company_id, user.id, body)


@router.post("/assets/{asset_id}/sell", dependencies=_SELL)
async def sell_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, sales.sell, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/sale", dependencies=_VIEW)
async def get_sale(asset_id: str, company_id=Depends(get_current_company_id),
                   session: AsyncSession = Depends(get_session)) -> dict:
    return await sales.view(session, company_id, asset_id)


@router.post("/assets/{asset_id}/sale/{action}", dependencies=_SELL)
async def change_sale(asset_id: str, action: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    call = {"credit": sales.credit, "reissue": sales.reissue, "cancel": sales.cancel}.get(action)
    if call is None:
        raise HTTPException(status_code=404, detail="No such sale action.")
    return await _write(session, call, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/undo-disposal", dependencies=_ACCOUNTING)
async def undo_disposal(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.undo, company_id, user.id, asset_id, body)


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


# ── Remeasurement and the annual review ──────────────────────────────────────


@router.post("/assets/{asset_id}/revalue", dependencies=_ACCOUNTING)
async def revalue(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                  user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "revaluation")


@router.post("/assets/{asset_id}/impair", dependencies=_ACCOUNTING)
async def impair(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                 user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "impairment")


@router.post("/assets/{asset_id}/reverse-impairment", dependencies=_ACCOUNTING)
async def reverse_impairment(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                             user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "impairment_reversal")


@router.post("/classes/{class_id}/revalue", dependencies=_ACCOUNTING)
async def revalue_class(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.revalue_class, company_id, user.id, class_id, body)


@router.get("/reviews", dependencies=_VIEW)
async def due_reviews(company_id=Depends(get_current_company_id),
                      company_settings: dict = Depends(get_current_company_settings),
                      session: AsyncSession = Depends(get_session)) -> dict:
    from datetime import date

    from celerp.services.business_time import business_date_of

    today = date.fromisoformat(business_date_of(None, company_settings.get("timezone")))
    return {"items": await remeasure.due_reviews(session, company_id, today)}


@router.get("/assets/{asset_id}/reviews", dependencies=_VIEW)
async def list_reviews(asset_id: str, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    await _asset(session, company_id, asset_id)
    return {"items": list(reversed(await remeasure.reviews(session, company_id, asset_id)))}


@router.post("/assets/{asset_id}/reviews", dependencies=_ACCOUNTING)
async def record_review(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.record_review, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/reviews/{review_id}/withdraw", dependencies=_ACCOUNTING)
async def withdraw_review(asset_id: str, review_id: str, company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.withdraw_review, company_id, user.id, asset_id, review_id)


# ── Additions and construction ───────────────────────────────────────────────


@router.post("/assets/{asset_id}/add", dependencies=_ACCOUNTING)
async def add_to_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.add, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/components", dependencies=_VIEW)
async def list_components(asset_id: str, company_id=Depends(get_current_company_id),
                          role: str = Depends(get_current_role),
                          company_settings: dict = Depends(get_current_company_settings),
                          session: AsyncSession = Depends(get_session)) -> dict:
    await _asset(session, company_id, asset_id)
    money = _shows_money(company_settings, role)
    return {"items": [asset_out(c.pop("id"), c, money=money)
                      for c in await additions.components(session, company_id, asset_id)]}


@router.post("/assets/{asset_id}/place-in-service", dependencies=_ACCOUNTING)
async def place_in_service(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.place_in_service, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/undo-latest", dependencies=_ACCOUNTING)
async def undo_latest(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.undo_latest, company_id, user.id, asset_id, body)


# ── Move, reclassify and transfer ────────────────────────────────────────────


@router.post("/assets/{asset_id}/move", dependencies=_MANAGE)
async def move_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.move, company_id, user.id, body, [asset_id])


@router.post("/move", dependencies=_MANAGE)
async def move_assets(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.move, company_id, user.id, body)


@router.post("/assets/{asset_id}/undo-move", dependencies=_MANAGE)
async def undo_move(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                    user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.undo_move, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/reclassify", dependencies=_ACCOUNTING)
async def reclassify_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, reclassify.reclassify, company_id, user.id, asset_id, body)


@router.get("/transfer-targets", dependencies=_ACCOUNTING)
async def transfer_targets(company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await transfer.targets(session, company_id, user.id)}


@router.post("/assets/{asset_id}/transfer", dependencies=_ACCOUNTING)
async def transfer_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, transfer.transfer, company_id, user.id, asset_id, body)


@router.post("/classes/{class_id}/apply-to-assets", dependencies=_ACCOUNTING)
async def apply_to_assets(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.apply_to_assets, company_id, user.id, class_id, body)


@router.post("/classes/{class_id}/fix-account", dependencies=_ACCOUNTING)
async def fix_account(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, fixaccount.fix_account, company_id, user.id, class_id, body)


@router.post("/classes/{class_id}/fix-account/undo", dependencies=_ACCOUNTING)
async def undo_fix_account(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, fixaccount.undo_fix, company_id, user.id, class_id, body)


@router.get("/reports/prior-period-adjustments", dependencies=_REPORTS)
async def prior_period_adjustments(year: int, company_id=Depends(get_current_company_id),
                                   session: AsyncSession = Depends(get_session)) -> dict:
    return await corrections.prior_period_report(session, company_id, year)


@router.get("/reports/schedule", dependencies=_REPORTS)
async def schedule_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.schedule(session, company_id, date_from, date_to)


@router.get("/reports/depreciation", dependencies=_REPORTS)
async def depreciation_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                              session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.depreciation(session, company_id, date_from, date_to)


@router.get("/reports/disposals", dependencies=_REPORTS)
async def disposals_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.disposals(session, company_id, date_from, date_to)


@router.post("/class-table", dependencies=_ACCOUNTING)
async def apply_class_table(body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, classtable.apply_table, company_id, user.id, body)


@router.post("/class-table/{op_key}/undo", dependencies=_ACCOUNTING)
async def undo_class_table(op_key: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, classtable.undo_table, company_id, user.id, op_key, body)


# ── Tax books and the year end ───────────────────────────────────────────────


@router.get("/tax-books", dependencies=_REPORTS)
async def get_tax_book(regime: str, year: int, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return await taxbooks.book(session, company_id, regime, year)


@router.patch("/tax-books/{asset_id}/{regime}", dependencies=_ACCOUNTING)
async def set_tax_book(asset_id: str, regime: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, taxbooks.set_book, company_id, user.id, asset_id, regime, body)


@router.post("/tax-years/{regime}/{year}/close", dependencies=_ACCOUNTING)
async def close_tax_year(regime: str, year: int, company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.close, company_id, user.id, regime, year)


@router.post("/tax-years/{regime}/{year}/reopen", dependencies=_ACCOUNTING)
async def reopen_tax_year(regime: str, year: int, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.reopen, company_id, user.id, regime, year, body)


@router.get("/year-end", dependencies=_VIEW)
async def get_year_end(year: int, company_settings: dict = Depends(get_current_company_settings),
                       role: str = Depends(get_current_role), company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    result = await yearend.report(session, company_id, year)
    if not _shows_money(company_settings, role):
        result["items"] = [{**i, "amount": None, "accepted_amount": None} for i in result["items"]]
    return result


@router.post("/year-end/{year}/marks", dependencies=_ACCOUNTING)
async def mark_year_end(year: int, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.mark, company_id, user.id, year, body)


@router.get("/reports/book-tax", dependencies=_REPORTS)
async def book_tax_report(regime: str, year: int, company_id=Depends(get_current_company_id),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.book_tax(session, company_id, regime, year)


@router.get("/reports/statutory/{report_key}", dependencies=_REPORTS)
async def statutory_report(report_key: str, year: int, company_id=Depends(get_current_company_id),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.statutory(session, company_id, report_key, year)


def setup_api_routes(app) -> None:
    """Called by the module loader: the event types, the journal operation types, the files
    an asset holds, the routes."""
    from celerp.services.attachments import register_file_attached_event
    from celerp.services.module_journals import register_operation_types

    from celerp_fixed_assets import checks, custody, files

    register_events()
    register_operation_types("celerp-fixed-assets", OPERATION_TYPES, source_prefix="fa:",
                             document_corrections=SALE_OPERATIONS)
    register_file_attached_event(files.ENTITY_TYPE, files.ATTACHED)
    app.include_router(router)
    for module in (files, custody, checks):
        app.include_router(module.router)
