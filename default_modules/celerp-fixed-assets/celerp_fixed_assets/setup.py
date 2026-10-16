# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Starting fixed assets in a company: its settings and asset classes.

Settings live under the company settings key ``fixed_assets``. Start writes the default
settings and the starter classes once; starting again changes nothing.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.models.projections import Projection
from celerp_fixed_assets.calc.rules import CONVENTIONS, METHODS

SETTINGS_KEY = "fixed_assets"
RUN_MODES = ("prepare", "post", "manual")
KINDS = ("tangible", "intangible")

# Starter classes: name, kind, asset role, method, suggested life in months.
STARTER_CLASSES = (
    ("Land", "tangible", "fixed_assets", "none", None),
    ("Buildings", "tangible", "fixed_assets", "straight_line", 480),
    ("Plant and machinery", "tangible", "fixed_assets", "straight_line", 120),
    ("Vehicles", "tangible", "fixed_assets", "straight_line", 60),
    ("Furniture and fittings", "tangible", "fixed_assets", "straight_line", 84),
    ("Computer equipment", "tangible", "fixed_assets", "straight_line", 36),
    ("Software", "intangible", "fixed_assets", "straight_line", 36),
    ("Other intangibles", "intangible", "fixed_assets", "straight_line", 60),
    ("Assets under construction", "tangible", "assets_under_construction", "none", None),
)

CLASS_FIELDS = ("name", "kind", "asset_account", "accumulated_depreciation_account", "depreciation_account",
                "method", "life_months", "residual_percent", "convention", "archived")


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def settings_of(company_settings: dict | None) -> dict:
    return dict((company_settings or {}).get(SETTINGS_KEY) or {})


def view(fa: dict) -> dict:
    """The settings as the API shows them, with the cutover the first managed period implies."""
    first = fa.get("first_managed_period")
    return {
        "run_mode": fa.get("run_mode", "prepare"),
        "convention": fa.get("convention", "full_month"),
        "threshold": fa.get("threshold"),
        "first_managed_period": first,
        "cutover": (date.fromisoformat(first) - timedelta(days=1)).isoformat() if first else None,
    }


def cutover(fa: dict) -> str | None:
    return view(fa)["cutover"]


async def company_settings(session, company_id) -> dict:
    from celerp.models.company import Company

    return dict((await session.execute(select(Company.settings).where(Company.id == company_id))).scalar_one_or_none()
                or {})


async def _first_managed_period(session, company_id) -> str:
    """The first day of the month of the earliest posted journal, else of this month."""
    ts = Projection.state["ts"].as_string()
    earliest = (await session.execute(select(func.min(func.substr(ts, 1, 10))).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.state["status"].as_string() == "posted"))).scalar_one_or_none()
    day = date.fromisoformat(earliest) if earliest else date.today()
    return day.replace(day=1).isoformat()


async def classes(session, company_id, *, include_archived: bool = False) -> list[dict]:
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_class"))).scalars().all()
    out = [{"id": r.entity_id, **r.state} for r in rows]
    out.sort(key=lambda c: (int(c.get("seq") or 0), c["id"]))
    return [c for c in out if include_archived or not c.get("archived")]


async def get_class(session, company_id, class_id) -> dict:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": str(class_id or "")})
    if row is None or row.entity_type != "fa_class":
        raise unprocessable("Choose an asset class.")
    return {"id": row.entity_id, **row.state}


async def _emit(session, company_id, user_id, entity_id: str, event_type: str, data: dict, key: str | None = None):
    from celerp.events.engine import emit_event

    return await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="fa_class",
                            event_type=event_type, data=data, actor_id=user_id, location_id=None,
                            source="fixed_assets", idempotency_key=key or str(uuid.uuid4()))


async def start(session, company_id, user_id) -> dict:
    """Start fixed assets with the default settings and the starter classes. Idempotent."""
    from celerp.services.account_roles import role_map
    from celerp.services.company_lock import locked_company
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    company = await locked_company(session, company_id)
    fa = settings_of(company.settings)
    if fa.get("started"):
        return view(fa)
    roles = role_map(company.settings)
    for seq, (name, kind, role, method, life) in enumerate(STARTER_CLASSES):
        data = {"name": name, "kind": kind, "asset_account": roles.get(role) or "",
                "accumulated_depreciation_account": roles.get("accumulated_depreciation") or "",
                "depreciation_account": roles.get("depreciation_expense") or "",
                "method": method, "life_months": life, "residual_percent": "0", "convention": None,
                "archived": False, "seq": seq}
        await _emit(session, company_id, user_id, f"fa:class:{uuid.uuid4()}", "fa.class.created", data,
                    key=f"fa:class:starter:{seq}")
    fa.update(started=True, run_mode="prepare", convention="full_month", threshold=None,
              first_managed_period=await _first_managed_period(session, company_id))
    company.settings = {**(company.settings or {}), SETTINGS_KEY: fa}
    await session.flush()
    return view(fa)


async def require_started(session, company_id) -> dict:
    fa = settings_of(await company_settings(session, company_id))
    if not fa.get("started"):
        raise HTTPException(status_code=409, detail="Start fixed assets first.")
    return fa


def _money(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise unprocessable(f"{field} must be a number.") from None
    if not amount.is_finite() or amount < 0:
        raise unprocessable(f"{field} must be zero or more.")
    return str(amount)


async def update_settings(session, company_id, user_id, body: dict) -> dict:
    from celerp.services.company_lock import locked_company
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    company = await locked_company(session, company_id)
    fa = settings_of(company.settings)
    if not fa.get("started"):
        raise HTTPException(status_code=409, detail="Start fixed assets first.")
    unknown = set(body) - {"threshold", "run_mode", "convention", "first_managed_period"}
    if unknown:
        raise unprocessable(f"Unknown setting: {sorted(unknown)[0]}.")
    if "threshold" in body:
        fa["threshold"] = _money(body["threshold"], "Threshold")
    if "run_mode" in body:
        if body["run_mode"] not in RUN_MODES:
            raise unprocessable("Choose how depreciation runs: prepare, post or manual.")
        fa["run_mode"] = body["run_mode"]
    if "convention" in body:
        if body["convention"] not in CONVENTIONS:
            raise unprocessable("Choose a timing convention.")
        fa["convention"] = body["convention"]
    if "first_managed_period" in body:
        try:
            first = date.fromisoformat(str(body["first_managed_period"]))
        except ValueError:
            raise unprocessable("The first managed period is a date.") from None
        if first.day != 1:
            raise unprocessable("The first managed period starts on the first day of a month.")
        fa["first_managed_period"] = first.isoformat()
    company.settings = {**(company.settings or {}), SETTINGS_KEY: fa}
    await session.flush()
    return view(fa)


async def _check_class(session, company_id, data: dict) -> None:
    from celerp.services.journal_accounts import lock_accounts

    if not str(data.get("name") or "").strip():
        raise unprocessable("A class needs a name.")
    if data.get("kind") not in KINDS:
        raise unprocessable("A class is tangible or intangible.")
    if data.get("method") not in METHODS:
        raise unprocessable("Choose a depreciation method.")
    if data.get("convention") not in (None, *CONVENTIONS):
        raise unprocessable("Choose a timing convention.")
    life = data.get("life_months")
    if life is not None and (not isinstance(life, int) or isinstance(life, bool) or life <= 0):
        raise unprocessable("The useful life is a whole number of months.")
    if data["method"] != "none" and life is None:
        raise unprocessable("A depreciated class needs a useful life.")
    residual = Decimal(_money(data.get("residual_percent"), "Residual percent") or 0)
    if residual >= 100:
        raise unprocessable("The residual percent is below 100.")
    codes = {data[k] for k in ("asset_account", "accumulated_depreciation_account", "depreciation_account")
             if data.get(k)}
    chart = await lock_accounts(session, company_id, codes)
    missing = sorted(codes - set(chart or {})) if chart is not None else []
    if missing:
        raise unprocessable(f"Account {missing[0]} is not in the chart of accounts.")


async def create_class(session, company_id, user_id, body: dict) -> dict:
    from celerp.services.account_roles import role_map
    from celerp.services.permissions import locked_authority

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await require_started(session, company_id)
    roles = role_map(settings)
    data = {"name": str(body.get("name") or "").strip(), "kind": body.get("kind") or "tangible",
            "asset_account": body.get("asset_account") or roles.get("fixed_assets") or "",
            "accumulated_depreciation_account": (body.get("accumulated_depreciation_account")
                                                 or roles.get("accumulated_depreciation") or ""),
            "depreciation_account": body.get("depreciation_account") or roles.get("depreciation_expense") or "",
            "method": body.get("method") or "straight_line", "life_months": body.get("life_months"),
            "residual_percent": str(body.get("residual_percent") or "0"), "convention": body.get("convention"),
            "archived": False, "seq": len(await classes(session, company_id, include_archived=True))}
    await _check_class(session, company_id, data)
    entity_id = f"fa:class:{uuid.uuid4()}"
    await _emit(session, company_id, user_id, entity_id, "fa.class.created", data)
    return {"id": entity_id, **data}


async def update_class(session, company_id, user_id, class_id: str, body: dict) -> dict:
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    rows = await lock_projections(session, company_id, [class_id])
    row = rows.get(class_id)
    if row is None or row.entity_type != "fa_class":
        raise HTTPException(status_code=404, detail="No such asset class.")
    unknown = set(body) - set(CLASS_FIELDS)
    if unknown:
        raise unprocessable(f"Unknown class field: {sorted(unknown)[0]}.")
    changes = {k: v for k, v in body.items() if row.state.get(k) != v}
    if "name" in changes:
        changes["name"] = str(changes["name"] or "").strip()
    if "archived" in changes and not isinstance(changes["archived"], bool):
        raise unprocessable("Archived is true or false.")
    await _check_class(session, company_id, {**row.state, **changes})
    if changes:
        await _emit(session, company_id, user_id, class_id, "fa.class.updated", {"changes": changes})
    return {"id": class_id, **row.state, **changes}
