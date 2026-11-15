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
from celerp.services.business_time import business_today
from celerp.services.permissions import assert_role_permission, get_current_company_settings, require_permission
from celerp_fixed_assets import (
    additions, classtable, corrections, disposal, fixaccount, imports, moves, opening, operations, recon, reclassify, remeasure, reports,
    runs, sales, setup, sources, taxbooks, transfer, views, yearend)
from celerp_fixed_assets.events import register_events
from celerp_fixed_assets.search import matching
from ui.i18n import t

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
_MONEY = Depends(views.money_shown)
_VIEW = [require_permission("view_fixed_assets")]
_ACCOUNTING = [require_permission("manage_accounting")]
_MANAGE = [require_permission("manage_fixed_assets")]
_REPORTS = [require_permission("view_financial_reports")]
_SELL = [require_permission("edit_documents"), require_permission("manage_fixed_assets")]


def asset_out(asset_id: str, state: dict, *, money: bool = True) -> dict:
    """An asset as the API shows it: its figures, and its net book value."""
    out = {"id": asset_id, "files": [],
           **{k: v for k, v in state.items() if k not in ("op_key", "movement_count", "version")}}
    if state.get("status") == "physical":
        out.update(dict.fromkeys(_MONEY_FIELDS))
    else:
        out["nbv"] = str(Decimal(str(state.get("cost") or 0)) - Decimal(str(state.get("accumulated_depreciation") or 0))
                         - Decimal(str(state.get("accumulated_impairment") or 0)))
    return views.readable(out, money)


async def _asset(session, company_id, asset_id: str) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": asset_id})
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail=t("fixed_assets.err_no_such_asset_2"))
    return row.state


# ── Setup ────────────────────────────────────────────────────────────────────


@router.get("/setup", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Show the fixed assets setup, such as the start date, accounts and depreciation settings.")
async def get_setup(company_settings: dict = Depends(get_current_company_settings),
                    company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session),
                    money: bool = _MONEY) -> dict:
    fa = setup.settings_of(company_settings)
    first = await setup.first_period(session, company_id, fa) if fa.get("started") else None
    return {"started": bool(fa.get("started")), "settings": setup.view(fa), "first_period": first,
            "rules": setup.rules_view(company_settings), "today": business_today(company_settings.get("timezone")).isoformat(),
            "money_shown": money}


@router.post("/start", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Start using fixed assets with the chosen start date and settings.")
async def start(company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.start(session, company_id, user.id)
    await session.commit()
    return result


@router.patch("/settings", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Change the fixed assets settings, such as the depreciation posting day or accounts.")
async def update_settings(body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.update_settings(session, company_id, user.id, body)
    await session.commit()
    return result


@router.get("/classes", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True}, summary="List the asset classes with their depreciation method, useful life and accounts.")
async def list_classes(include_archived: bool = False, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": [{**c, "account_problems": await fixaccount.unusable(session, company_id, c)}
                      for c in await setup.classes(session, company_id, include_archived=include_archived)]}


@router.post("/classes", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Add an asset class with its depreciation method, useful life and accounts.")
async def create_class(body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.create_class(session, company_id, user.id, body)
    await session.commit()
    return result


@router.patch("/classes/{class_id}", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/classes/{class_id}/apply"}, summary="Change an asset class, such as its useful life or accounts.")
async def update_class(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    result = await setup.update_class(session, company_id, user.id, class_id, body)
    await session.commit()
    return result


# ── Source lines ─────────────────────────────────────────────────────────────


@router.get("/intake", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="List the bill and journal lines posted to asset accounts that wait to be registered as assets.")
async def intake(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": await sources.intake(session, company_id)}


@router.get("/source-line", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="Read one bill or journal line posted to an asset account and what became of it.")
async def source_line(source: str = "", company_id=Depends(get_current_company_id),
                      session: AsyncSession = Depends(get_session)) -> dict:
    """One source line, waiting or not, with what became of it."""
    return await sources.line(session, company_id, source)


async def _write(session, call, *args) -> dict:
    result = await call(session, *args)
    await session.commit()
    return result


@router.post("/register", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Register a fixed asset from a bill or journal line posted to an asset account.")
async def register(body: dict = Body(...), company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.register, company_id, user.id, body)


@router.post("/not-acquisition", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Mark a line posted to an asset account as not an asset purchase.")
async def not_acquisition(body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.disposition, company_id, user.id, body, "not_acquisition")


@router.post("/keep-line", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Keep a line posted to an asset account waiting in the intake list.")
async def keep_line(body: dict = Body(...), company_id=Depends(get_current_company_id),
                    user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.disposition, company_id, user.id, body, "keep")


@router.post("/link-depreciation", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Link a depreciation journal line to the asset it belongs to.")
async def link_depreciation(body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.link_depreciation, company_id, user.id, body)


@router.post("/sources/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Undo what was done with a line from the intake list so it waits again.")
async def undo_source(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.undo_source, company_id, user.id, body)


# ── Opening balances and the reconciliation ──────────────────────────────────


@router.get("/opening", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="Show the opening asset register being entered and how it compares with the ledger.")
async def get_opening(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return await opening.view(session, company_id)


@router.post("/opening/finish", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/opening"}, summary="Finish entering the opening asset register.")
async def finish_opening(body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, opening.finish, company_id, user.id, body)


@router.post("/imports", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Upload an asset register file to import.")
async def import_register(body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    options = {k: v for k, v in body.items() if k not in ("rows", "as_at", "source")}
    result = await imports.import_register(session, company_id=company_id, user_id=user.id, rows=body.get("rows"),
                                           as_at=body.get("as_at"), source=body.get("source"), body=options)
    await session.commit()
    return result


@router.get("/imports", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="List the asset register imports, newest first.")
async def list_imports(limit: int = 50, offset: int = 0, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.refuse("fixed_assets.err_choose_page_1_500_imports")
    found = await imports.batches(session, company_id)
    return {"items": found[offset:offset + limit], "total": len(found), "limit": limit, "offset": offset}


@router.get("/imports/{batch_id}", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="Read one asset register import with its rows and any errors.")
async def get_import(batch_id: str, status: str | None = None, limit: int = 100, offset: int = 0,
                     company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    return await imports.batch_view(session, company_id, batch_id, status=status, limit=limit, offset=offset)


@router.post("/imports/{batch_id}/rows", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/imports/{batch_id}"}, summary="Import the checked rows of an asset register file.")
async def import_rows(batch_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, imports.import_rows, company_id, user.id, batch_id, body)


@router.post("/imports/{batch_id}/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/imports/{batch_id}"}, summary="Undo an asset register import, removing the assets it created.")
async def undo_import(batch_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, imports.undo_import, company_id, user.id, batch_id, body)


@router.get("/reconciliation", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True}, summary="Compare the asset register with the asset accounts in the ledger.")
async def get_reconciliation(at: str | None = None, company_id=Depends(get_current_company_id),
                             session: AsyncSession = Depends(get_session)) -> dict:
    return await recon.report(session, company_id, at)


@router.post("/reconciliation/acknowledge", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/reconciliation"}, summary="Acknowledge a difference between the asset register and the ledger.")
async def acknowledge(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, recon.acknowledge, company_id, user.id, body)


# ── Assets ───────────────────────────────────────────────────────────────────


@router.get("/assets", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Find fixed assets by name, class, location, custodian or status.")
async def list_assets(class_id: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0,
                      company_id=Depends(get_current_company_id), money: bool = _MONEY,
                      session: AsyncSession = Depends(get_session)) -> dict:
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.refuse("fixed_assets.err_choose_page_1_500_assets")
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_asset"))).all()
    found = [(eid, s) for eid, s in matching(rows, q) if not class_id or s.get("class_id") == class_id]
    return {"items": [asset_out(eid, s, money=money) for eid, s in found[offset:offset + limit]],
            "total": len(found), "limit": limit, "offset": offset}


@router.post("/assets", dependencies=[require_permission("manage_fixed_assets")], openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new", "x-celerp-agent-result-permissions": ["manage_accounting"]}, summary="Add a fixed asset with its class, cost and date in service.")
async def create_asset(body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), role: str = Depends(get_current_role),
                       company_settings: dict = Depends(get_current_company_settings),
                       session: AsyncSession = Depends(get_session)) -> dict:
    # A physical record posts nothing; an opening or contributed asset posts its cost.
    if body.get("kind") != "physical":
        assert_role_permission(company_settings, role, "manage_accounting")
    return await _write(session, operations.create_asset, company_id, user.id, body)


def _left_use(asset_id: str, state: dict) -> dict | None:
    """How an asset no longer in use left it and the way back, as its forms say it first."""
    why = operations.left_use(state)
    return None if why is None else {**why, "params": {**why["params"],
                                                      "links": operations.left_use_links(asset_id, state)}}


async def _undo_refused(session, company_id, asset_id: str, state: dict) -> dict:
    """Why each undo the asset page offers would be refused now (None where it would not), so
    the page says so beside the button rather than after it is pressed."""
    movements = await operations.asset_movements(session, company_id, asset_id)
    found = {"registration": await operations.registration_refusal(session, company_id, asset_id, state),
             "disposal": await disposal.undo_refusal(session, company_id, state, movements)}
    return {k: e.detail if e else None for k, e in found.items()}


@router.get("/assets/{asset_id}", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Read one fixed asset in full, with its cost, depreciation, location and history.")
async def get_asset(asset_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                    session: AsyncSession = Depends(get_session)) -> dict:
    state = await _asset(session, company_id, asset_id)
    return {**asset_out(asset_id, state, money=money),
            **views.readable(await remeasure.asset_facts(session, company_id, asset_id, state), money),
            "undoable": await additions.undoable(session, company_id, asset_id),
            "undo_refused": await _undo_refused(session, company_id, asset_id, state),
            "left_use": _left_use(asset_id, state),
            "next_charge": views.readable(await runs.next_charge(session, company_id, asset_id, state), money)}


@router.patch("/assets/{asset_id}", dependencies=[require_permission("manage_fixed_assets")], openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new", "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Change a fixed asset's details, such as its name, serial number or notes.")
async def update_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), money: bool = _MONEY,
                       session: AsyncSession = Depends(get_session)) -> dict:
    state = await _write(session, operations.update_asset, company_id, user.id, asset_id, body)
    return asset_out(asset_id, state, money=money)


@router.post("/assets/{asset_id}/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Undo adding a fixed asset that has posted nothing yet.")
async def undo_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.undo_asset, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/financial-details", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Set a fixed asset's cost, useful life and depreciation details.")
async def financial_details(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, operations.add_financial_details, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/movements", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the moves of a fixed asset between locations and custodians.")
async def list_movements(asset_id: str, limit: int = 50, offset: int = 0,
                         company_id=Depends(get_current_company_id), money: bool = _MONEY,
                         session: AsyncSession = Depends(get_session)) -> dict:
    if limit < 1 or limit > 500 or offset < 0:
        raise setup.refuse("fixed_assets.err_choose_page_1_500_movements")
    await _asset(session, company_id, asset_id)
    items = sorted(await operations.asset_movements(session, company_id, asset_id),
                   key=lambda m: (m.get("posting_date") or "", int(m["id"].rsplit(":", 1)[1])), reverse=True)
    return {"items": views.readable(items[offset:offset + limit], money), "total": len(items), "limit": limit, "offset": offset}


# ── Depreciation runs ────────────────────────────────────────────────────────


@router.get("/runs", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the monthly depreciation runs, newest first.")
async def list_runs(company_id=Depends(get_current_company_id), money: bool = _MONEY,
                    session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": views.readable(await runs.records(session, company_id), money)}


@router.post("/runs", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/runs"}, summary="Post the depreciation of a month to the books.")
async def run(body: dict = Body(...), company_id=Depends(get_current_company_id), user=Depends(get_current_user),
              session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.run, company_id, user.id, body)


@router.post("/runs/{month}/reverse", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/runs"}, summary="Reverse the depreciation posted for one month.")
async def reverse_run(month: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.reverse, company_id, user.id, month, body)


@router.get("/attention", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the fixed assets and tasks that need attention.")
async def attention(company_id=Depends(get_current_company_id), money: bool = _MONEY,
                    session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": views.readable(await runs.attention(session, company_id), money)}


@router.post("/assets/{asset_id}/readings", dependencies=[require_permission("manage_fixed_assets")], openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Record a usage reading of a fixed asset, such as hours or kilometres.")
async def record_reading(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.record_reading, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/skip", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Skip the depreciation of a fixed asset for one month.")
async def skip_month(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.skip_month, company_id, user.id, asset_id, body)


@router.delete("/assets/{asset_id}/skip/{month}", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Withdraw a skipped month of depreciation on a fixed asset.")
async def withdraw_skip(asset_id: str, month: str, company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, runs.withdraw_skip, company_id, user.id, asset_id, month)


# ── Corrections ──────────────────────────────────────────────────────────────


@router.get("/assets/{asset_id}/correct-fields", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the fields of a fixed asset that can be corrected.")
async def correct_fields(asset_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                         session: AsyncSession = Depends(get_session)) -> dict:
    return views.readable(await corrections.fields(session, company_id, asset_id), money)


@router.post("/assets/{asset_id}/dispose", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/dispose"}, summary="Dispose of a fixed asset, such as when it is scrapped or lost.")
async def dispose_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.dispose, company_id, user.id, body, [asset_id])


@router.post("/dispose", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/dispose"}, summary="Dispose of several fixed assets at once.")
async def dispose_assets(body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.dispose, company_id, user.id, body)


@router.post("/assets/{asset_id}/sell", dependencies=_SELL, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/sell"}, summary="Record the sale of a fixed asset with its price and buyer.")
async def sell_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, sales.sell, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/sale", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Read the sale of a fixed asset.")
async def get_sale(asset_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                   session: AsyncSession = Depends(get_session)) -> dict:
    return views.readable(await sales.view(session, company_id, asset_id), money)


@router.post("/assets/{asset_id}/sale/{action}", dependencies=_SELL, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Change the sale of a fixed asset, such as invoicing or cancelling it.")
async def change_sale(asset_id: str, action: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    call = {"credit": sales.credit, "reissue": sales.reissue, "cancel": sales.cancel}.get(action)
    if call is None:
        raise HTTPException(status_code=404, detail=t("fixed_assets.err_no_such_sale_action"))
    return await _write(session, call, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/undo-disposal", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Undo the disposal of a fixed asset.")
async def undo_disposal(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, disposal.undo, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/correct", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/correct"}, summary="Correct a field of a fixed asset, reposting depreciation where it changes.")
async def correct(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                  user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.correct, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/corrections", dependencies=[require_permission("manage_fixed_assets")], openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Ask for a correction of a fixed asset that needs approval.")
async def request_correction(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                             user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.request, company_id, user.id, asset_id, body)


@router.get("/corrections", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the fixed asset corrections waiting for approval.")
async def list_corrections(asset_id: str | None = None, company_id=Depends(get_current_company_id),
                           money: bool = _MONEY, session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": views.readable(await corrections.records(session, company_id, asset_id), money)}


@router.get("/corrections/{correction_id}", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Read one fixed asset correction with what it would change.")
async def get_correction(correction_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                         session: AsyncSession = Depends(get_session)) -> dict:
    return {"id": correction_id,
            **views.readable(await corrections.record(session, company_id, correction_id), money)}


@router.post("/corrections/{correction_id}/apply", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/corrections"}, summary="Apply a fixed asset correction.")
async def apply_correction(correction_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.apply_request, company_id, user.id, correction_id, body)


@router.post("/corrections/{correction_id}/withdraw", dependencies=[require_permission("manage_fixed_assets")], openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/corrections"}, summary="Withdraw a fixed asset correction that was asked for.")
async def withdraw_correction(correction_id: str, company_id=Depends(get_current_company_id),
                              user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.withdraw, company_id, user.id, correction_id)


@router.post("/corrections/{correction_id}/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/corrections"}, summary="Undo an applied fixed asset correction.")
async def undo_correction(correction_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.undo, company_id, user.id, correction_id, body)


# ── Remeasurement and the annual review ──────────────────────────────────────


@router.post("/assets/{asset_id}/revalue", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Revalue a fixed asset to a new fair value.")
async def revalue(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                  user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "revaluation")


@router.post("/assets/{asset_id}/impair", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Record an impairment loss on a fixed asset.")
async def impair(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                 user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "impairment")


@router.post("/assets/{asset_id}/reverse-impairment", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Reverse an impairment loss on a fixed asset.")
async def reverse_impairment(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                             user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.remeasure, company_id, user.id, asset_id, body, "impairment_reversal")


@router.post("/classes/{class_id}/revalue", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/classes/{class_id}/revalue"}, summary="Revalue every fixed asset in a class.")
async def revalue_class(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.revalue_class, company_id, user.id, class_id, body)


@router.get("/reviews", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the fixed assets due for an impairment or useful life review.")
async def due_reviews(company_id=Depends(get_current_company_id),
                      company_settings: dict = Depends(get_current_company_settings), money: bool = _MONEY,
                      session: AsyncSession = Depends(get_session)) -> dict:
    today = business_today(company_settings.get("timezone"))
    return {"items": views.readable(await remeasure.due_reviews(session, company_id, today), money)}


@router.get("/assets/{asset_id}/reviews", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the reviews recorded on a fixed asset.")
async def list_reviews(asset_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                       session: AsyncSession = Depends(get_session)) -> dict:
    await _asset(session, company_id, asset_id)
    return {"items": views.readable(list(reversed(await remeasure.reviews(session, company_id, asset_id))), money)}


@router.post("/assets/{asset_id}/reviews", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Record a review of a fixed asset, such as its useful life or impairment.")
async def record_review(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.record_review, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/reviews/{review_id}/withdraw", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Withdraw a review recorded on a fixed asset.")
async def withdraw_review(asset_id: str, review_id: str, company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, remeasure.withdraw_review, company_id, user.id, asset_id, review_id)


# ── Additions and construction ───────────────────────────────────────────────


@router.post("/assets/{asset_id}/add", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/add"}, summary="Add a component or improvement to a fixed asset.")
async def add_to_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.add, company_id, user.id, asset_id, body)


@router.get("/assets/{asset_id}/components", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="List the components and improvements of a fixed asset.")
async def list_components(asset_id: str, company_id=Depends(get_current_company_id), money: bool = _MONEY,
                          session: AsyncSession = Depends(get_session)) -> dict:
    await _asset(session, company_id, asset_id)
    return {"items": [asset_out(c.pop("id"), c, money=money)
                      for c in await additions.components(session, company_id, asset_id)]}


@router.post("/assets/{asset_id}/place-in-service", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/place-in-service"}, summary="Place a fixed asset in service so it starts depreciating.")
async def place_in_service(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.place_in_service, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/undo-latest", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Undo the latest change made to a fixed asset.")
async def undo_latest(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, additions.undo_latest, company_id, user.id, asset_id, body)


# ── Move, reclassify and transfer ────────────────────────────────────────────


@router.post("/assets/{asset_id}/move", dependencies=_MANAGE, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/move"}, summary="Move a fixed asset to another location or custodian.")
async def move_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.move, company_id, user.id, body, [asset_id])


@router.post("/move", dependencies=_MANAGE, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/move"}, summary="Move several fixed assets to another location or custodian at once.")
async def move_assets(body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.move, company_id, user.id, body)


@router.post("/assets/{asset_id}/undo-move", dependencies=_MANAGE, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/new"}, summary="Undo the latest move of a fixed asset.")
async def undo_move(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                    user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, moves.undo_move, company_id, user.id, asset_id, body)


@router.post("/assets/{asset_id}/reclassify", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/reclassify"}, summary="Move a fixed asset to another asset class.")
async def reclassify_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, reclassify.reclassify, company_id, user.id, asset_id, body)


@router.get("/transfer-targets", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": "*"}, summary="List the companies a fixed asset can be transferred to.")
async def transfer_targets(company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return await transfer.targets(session, company_id, user.id)


@router.post("/assets/{asset_id}/transfer", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/assets/{asset_id}/transfer"}, summary="Transfer a fixed asset to another company.")
async def transfer_asset(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, transfer.transfer, company_id, user.id, asset_id, body)


@router.post("/classes/{class_id}/apply-to-assets", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/classes/{class_id}/apply"}, summary="Apply an asset class's settings to the assets already in it.")
async def apply_to_assets(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, corrections.apply_to_assets, company_id, user.id, class_id, body)


@router.post("/classes/{class_id}/fix-account", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/classes/{class_id}/fix-account"}, summary="Fix the account an asset class posts to, moving its balances.")
async def fix_account(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                      user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, fixaccount.fix_account, company_id, user.id, class_id, body)


@router.post("/classes/{class_id}/fix-account/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/classes/{class_id}/fix-account"}, summary="Undo fixing the account of an asset class.")
async def undo_fix_account(class_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, fixaccount.undo_fix, company_id, user.id, class_id, body)


@router.get("/reports/prior-period-adjustments", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show the fixed asset adjustments that changed earlier periods.")
async def prior_period_adjustments(year: int, company_id=Depends(get_current_company_id),
                                   session: AsyncSession = Depends(get_session)) -> dict:
    return await corrections.prior_period_report(session, company_id, year)


@router.get("/reports/schedule", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show the fixed asset schedule with cost, depreciation and book value by class.")
async def schedule_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.schedule(session, company_id, date_from, date_to)


@router.get("/reports/depreciation", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show the depreciation of each fixed asset over a period.")
async def depreciation_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                              session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.depreciation(session, company_id, date_from, date_to)


@router.get("/reports/disposals", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show the fixed assets disposed of or sold over a period, with gains and losses.")
async def disposals_report(date_from: str, date_to: str, company_id=Depends(get_current_company_id),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.disposals(session, company_id, date_from, date_to)


@router.post("/class-table", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Apply a table of asset classes, such as a country's tax depreciation rates.")
async def apply_class_table(body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, classtable.apply_table, company_id, user.id, body)


@router.post("/class-table/{op_key}/undo", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Undo applying a table of asset classes.")
async def undo_class_table(op_key: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                           user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, classtable.undo_table, company_id, user.id, op_key, body)


# ── Tax books and the year end ───────────────────────────────────────────────


@router.get("/tax-books", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show the tax depreciation book of the fixed assets for one tax regime.")
async def get_tax_book(regime: str, year: int, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return await taxbooks.book(session, company_id, regime, year)


@router.patch("/tax-books/{asset_id}/{regime}", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/tax-books"}, summary="Change the tax depreciation settings of one fixed asset for one tax regime.")
async def set_tax_book(asset_id: str, regime: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, taxbooks.set_book, company_id, user.id, asset_id, regime, body)


@router.post("/tax-years/{regime}/{year}/close", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Close a tax year of the fixed asset tax book.")
async def close_tax_year(regime: str, year: int, company_id=Depends(get_current_company_id),
                         user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.close, company_id, user.id, regime, year)


@router.post("/tax-years/{regime}/{year}/reopen", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets"}, summary="Reopen a closed tax year of the fixed asset tax book.")
async def reopen_tax_year(regime: str, year: int, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.reopen, company_id, user.id, regime, year, body)


@router.get("/year-end", dependencies=_VIEW, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_financial_reports"]}, summary="Show the fixed asset year end checklist and what is marked done.")
async def get_year_end(year: int, money: bool = _MONEY, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    result = await yearend.report(session, company_id, year)
    return {**result, "items": views.readable(result["items"], money)}


@router.post("/year-end/{year}/marks", dependencies=_ACCOUNTING, openapi_extra={"x-celerp-agent": True, "x-celerp-agent-check": "/fixed-assets/year-end"}, summary="Mark fixed asset year end checklist steps as done or not done.")
async def mark_year_end(year: int, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write(session, yearend.mark, company_id, user.id, year, body)


@router.get("/reports/book-tax", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Compare the book and tax depreciation of the fixed assets.")
async def book_tax_report(regime: str, year: int, company_id=Depends(get_current_company_id),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.book_tax(session, company_id, regime, year)


@router.get("/reports/statutory/{report_key}", dependencies=_REPORTS, openapi_extra={"x-celerp-agent": True}, summary="Show a statutory fixed asset report required in the company's country.")
async def statutory_report(report_key: str, year: int, company_id=Depends(get_current_company_id),
                           session: AsyncSession = Depends(get_session)) -> dict:
    return await reports.statutory(session, company_id, report_key, year)


def setup_api_routes(app) -> None:
    """Called by the module loader: the event types, the journal operation types, the files
    an asset holds, the routes."""
    from celerp.services.attachments import register_file_attached_event
    from celerp.services.module_journals import register_operation_types

    from celerp_fixed_assets import checks, custody, files, upkeep

    register_events()
    register_operation_types("celerp-fixed-assets", OPERATION_TYPES, source_prefix="fa:",
                             document_corrections=SALE_OPERATIONS)
    register_file_attached_event(files.ENTITY_TYPE, files.ATTACHED)
    app.include_router(router)
    for module in (files, custody, checks, upkeep):
        app.include_router(module.router)
