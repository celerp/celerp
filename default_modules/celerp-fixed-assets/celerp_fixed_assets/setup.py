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
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_fixed_assets.calc import rules

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
                "method", "life_months", "residual_percent", "convention", "params", "archived",
                "measurement_policy", "active_market", "construction", "tax_categories")
# How a class measures its assets after recognition. A class moves to revaluation only by
# revaluing every asset in it on one date (celerp_fixed_assets.remeasure).
MEASUREMENT_POLICIES = ("cost", "revaluation")


# What an asset depreciates under, copied from its class when it is added: a later class
# edit changes new assets only, until it is applied to existing ones as a correction.
POLICY_FIELDS = ("method", "life_months", "residual_percent", "convention", "params")


def policy_of(cls: dict, fa: dict) -> dict:
    """The policy an asset of ``cls`` takes, its convention resolved from the settings."""
    return {"method": cls["method"], "life_months": cls.get("life_months"),
            "residual_percent": str(cls.get("residual_percent") or "0"),
            "convention": cls.get("convention") or fa.get("convention") or "full_month",
            "params": dict(cls.get("params") or {})}


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def settings_of(company_settings: dict | None) -> dict:
    return dict((company_settings or {}).get(SETTINGS_KEY) or {})


def view(fa: dict) -> dict:
    """The settings as the API shows them, with the cutover the first managed period implies."""
    first = fa.get("first_managed_period")
    return {
        "run_mode": fa.get("run_mode", "prepare"),
        "run_day": int(fa.get("run_day") or 1),
        "auto_post_user": fa.get("auto_post_user"),
        "auto_post_paused": bool(fa.get("auto_post_paused")),
        "convention": fa.get("convention", "full_month"),
        "threshold": fa.get("threshold"),
        "first_managed_period": first,
        "cutover": (date.fromisoformat(first) - timedelta(days=1)).isoformat() if first else None,
        "tax_year_end": fa.get("tax_year_end") or "12-31",
        "tax_regimes": list(fa.get("tax_regimes") or []),
    }


def rules_view(company_settings: dict | None) -> dict:
    """The rules the company may use, each with the module that registered it (None for
    this module's own), and every rule entry that was skipped with its reason."""
    from ui.i18n import t

    def owner(kind, key):
        found = rules.module_of(kind, key)
        return found[0] if found else None

    def base(kind, rule):
        return {"key": rule.key, "module": owner(kind, rule.key), "label": t(rule.label_key)}

    available = {kind: rules.available(kind, company_settings).values() for kind in rules.KINDS}
    return {
        "methods": [{**base("method", m), "params_schema": dict(m.params_schema), "uses_readings": bool(m.uses_readings)}
                    for m in available["method"]],
        "conventions": [base("convention", c) for c in available["convention"]],
        "tax_regimes": [{**base("tax_regime", r), "country": r.country, "basis": r.basis,
                         "categories": [{"key": c.key, "label": t(c.label_key)} for c in r.categories],
                         "elections_schema": dict(r.elections_schema), "deferred_tax": r.deferred_tax is not None}
                        for r in available["tax_regime"]],
        "statutory_reports": [{**base("statutory_report", f), "country": f.country, "regime": f.regime}
                              for f in available["statutory_report"]],
        "problems": [dict(p) for p in rules.problems()],
    }


def cutover(fa: dict) -> str | None:
    return view(fa)["cutover"]


async def company_settings(session, company_id) -> dict:
    from celerp.models.company import Company

    return dict((await session.execute(select(Company.settings).where(Company.id == company_id))).scalar_one_or_none()
                or {})


async def first_period(session, company_id, fa: dict) -> dict | None:
    """While the first managed period is unconfirmed: the period suggested and what it was
    read from. The suggestion is the month after the latest depreciation already booked by
    hand to an accumulated depreciation account, else the month of the earliest journal,
    else this month. It is never applied until the user confirms it."""
    if fa.get("first_managed_period"):
        return None
    from celerp.services.ledger_rows import journal_rows

    accumulated = {c["accumulated_depreciation_account"] for c in await classes(session, company_id,
                                                                                   include_archived=True)
                   if c.get("accumulated_depreciation_account")}
    rows = sorted(await journal_rows(session, company_id), key=lambda r: (r[2], r[0]))
    booked = [{"account": e["account"], "date": day, "journal_id": je_id}
              for je_id, je, day in rows if je_id.startswith("je:manual:") and day
              for e in je.get("entries") or []
              if e.get("account") in accumulated and Decimal(str(e.get("credit") or 0)) > 0]
    earliest = next((day for _, _, day in rows if day), None)
    if booked:
        suggested = date.fromisoformat(booked[-1]["date"]) + timedelta(days=1)
        if suggested.day != 1:
            suggested = (suggested.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        suggested = (date.fromisoformat(earliest) if earliest else date.today()).replace(day=1)
    return {"suggested": suggested.isoformat(),
            "evidence": {"depreciation": booked, "earliest_journal": earliest}}


async def require_managed(session, company_id) -> dict:
    """The settings, once fixed assets is started and its first managed period confirmed."""
    fa = await require_started(session, company_id)
    if not fa.get("first_managed_period"):
        raise HTTPException(status_code=409, detail="Confirm the first managed period in the fixed asset "
                                                    "settings first. Nothing is recorded before it is confirmed.")
    return fa


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
                "params": {}, "archived": False, "seq": seq, "measurement_policy": "cost", "active_market": None,
                "construction": role == "assets_under_construction"}
        await _emit(session, company_id, user_id, f"fa:class:{uuid.uuid4()}", "fa.class.created", data,
                    key=f"fa:class:starter:{seq}")
    fa.update(started=True, run_mode="prepare", convention="full_month", threshold=None, first_managed_period=None)
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
    unknown = set(body) - {"threshold", "run_mode", "run_day", "convention", "first_managed_period", "tax_year_end",
                           "tax_regimes"}
    if unknown:
        raise unprocessable(f"Unknown setting: {sorted(unknown)[0]}.")
    if "threshold" in body:
        fa["threshold"] = _money(body["threshold"], "Threshold")
    if "run_mode" in body:
        if body["run_mode"] not in RUN_MODES:
            raise unprocessable("Choose how depreciation runs: prepare, post or manual.")
        fa["run_mode"] = body["run_mode"]
        if body["run_mode"] == "post":
            # Runs post as the user who chose to post them, while that user may.
            from celerp_fixed_assets import notices

            fa.update(auto_post_user=str(user_id), auto_post_paused=False)
            await notices.clear(session, company_id, notices.PAUSED, notices.NO_USER)
    if "run_day" in body:
        day = body["run_day"]
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 28:
            raise unprocessable("The run day is a day of the month from 1 to 28.")
        fa["run_day"] = day
    if "convention" in body:
        if body["convention"] != fa.get("convention") and not rules.is_available(
                "convention", body["convention"], company.settings):
            raise unprocessable("Choose a timing convention.")
        fa["convention"] = body["convention"]
    if "tax_year_end" in body:
        fa["tax_year_end"] = _month_day(body["tax_year_end"])
    if "tax_regimes" in body:
        chosen = body["tax_regimes"]
        if not isinstance(chosen, list) or not all(isinstance(k, str) for k in chosen):
            raise unprocessable("Tax regimes are a list of regime keys.")
        for key in chosen:
            if key not in (fa.get("tax_regimes") or []) and not rules.is_available("tax_regime", key, company.settings):
                raise unprocessable(f"{key} is not a tax regime this company can use.")
        fa["tax_regimes"] = list(dict.fromkeys(chosen))
    if "first_managed_period" in body:
        try:
            first = date.fromisoformat(str(body["first_managed_period"]))
        except ValueError:
            raise unprocessable("The first managed period is a date.") from None
        if first.day != 1:
            raise unprocessable("The first managed period starts on the first day of a month.")
        if fa.get("first_managed_period") and fa["first_managed_period"] != first.isoformat():
            from celerp_fixed_assets import opening

            await opening.move_cutover(session, company_id, user_id, fa, (first - timedelta(days=1)).isoformat())
            fa["opening"] = None
        fa["first_managed_period"] = first.isoformat()
    company.settings = {**(company.settings or {}), SETTINGS_KEY: fa}
    await session.flush()
    return view(fa)


def _month_day(value) -> str:
    """A tax year end, as MM-DD. 29 February is refused: the year would end on a different
    day in three years out of four."""
    try:
        day = date.fromisoformat(f"2025-{value}") if isinstance(value, str) and len(value) == 5 else None
    except ValueError:
        day = None
    if day is None:
        raise unprocessable("The tax year end is a month and day, such as 12-31.")
    return day.strftime("%m-%d")


def method_params(method: str, params) -> dict:
    """A method's parameters, checked against its schema. A method that reads units
    takes its life from ``total_units``, so that one is required."""
    schema = rules.method(method).params_schema
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise unprocessable("Method settings are a set of named values.")
    unknown = set(params) - set(schema)
    if unknown:
        raise unprocessable(f"{sorted(unknown)[0]} is not a setting of this method.")
    out = {}
    for name, value in params.items():
        if value in (None, ""):
            continue
        if schema[name] == "bool":
            if not isinstance(value, bool):
                raise unprocessable(f"{name} is true or false.")
            out[name] = value
            continue
        amount = Decimal(_money(value, name))
        if amount == 0:
            raise unprocessable(f"{name} must be more than zero.")
        out[name] = str(amount)
    if rules.method(method).uses_readings and "total_units" not in out:
        raise unprocessable("A units-of-use class needs the total units the asset is expected to produce.")
    return out


def check_policy(data: dict, company_settings: dict | None, current: dict | None = None) -> None:
    """Check the policy fields of a class or an asset, normalising its method settings.
    A method or convention from a module the company has turned off is refused, unless it
    is the one already there: the record keeps it, and its parameters stand as they were."""
    current = current or {}
    method = data.get("method")
    kept = method == current.get("method") and not rules.is_available("method", method, company_settings)
    if not kept and not (isinstance(method, str) and rules.is_available("method", method, company_settings)):
        raise unprocessable("Choose a depreciation method.")
    convention = data.get("convention")
    if convention is not None and convention != current.get("convention") and not (
            isinstance(convention, str) and rules.is_available("convention", convention, company_settings)):
        raise unprocessable("Choose a timing convention.")
    life = data.get("life_months")
    if life is not None and (not isinstance(life, int) or isinstance(life, bool) or life <= 0):
        raise unprocessable("The useful life is a whole number of months.")
    if kept:
        if data.get("params") != current.get("params"):
            raise unprocessable(rules.unavailable_reason("method", method))
    else:
        if method != "none" and not rules.method(method).uses_readings and life is None:
            raise unprocessable("A depreciated class needs a useful life.")
        data["params"] = method_params(method, data.get("params"))
    residual = Decimal(_money(data.get("residual_percent"), "Residual percent") or 0)
    if residual >= 100:
        raise unprocessable("The residual percent is below 100.")


def _active_market(value) -> dict | None:
    """The evidence of an active market an intangible class is revalued against."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"date", "basis"}:
        raise unprocessable("An active market has a date and a basis.")
    try:
        day = date.fromisoformat(str(value.get("date"))).isoformat()
    except ValueError:
        raise unprocessable("The active market date is a date.") from None
    basis = str(value.get("basis") or "").strip()
    if not basis:
        raise unprocessable("Say where the active market is quoted.")
    return {"date": day, "basis": basis}


def check_tax_categories(value, current: dict | None = None) -> dict:
    """A class's tax category per regime: each regime registered by a module, each
    category one of that regime's own. A mapping already there to a regime no longer
    loaded is kept as it was."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise unprocessable("Tax categories name a category for each tax regime.")
    current = current or {}
    for regime_key, category in value.items():
        if current.get(regime_key) == category:
            continue
        try:
            regime = rules.regime(regime_key)
        except rules.RuleUnavailable:
            raise unprocessable(f"{regime_key} is not a tax regime any installed module provides.") from None
        if category not in {c.key for c in regime.categories}:
            raise unprocessable(f"{category} is not a category of the tax regime {regime_key}.")
    return dict(value)


async def check_class(session, company_id, data: dict, company_settings: dict | None,
                       current: dict | None = None) -> None:
    from celerp.services.journal_accounts import require_destinations

    if not str(data.get("name") or "").strip():
        raise unprocessable("A class needs a name.")
    if data.get("kind") not in KINDS:
        raise unprocessable("A class is tangible or intangible.")
    if not isinstance(data.get("construction"), bool):
        raise unprocessable("Under construction is true or false.")
    if data["construction"] and data.get("method") != "none":
        raise unprocessable("A class for assets under construction is not depreciated. Choose no depreciation.")
    check_policy(data, company_settings, current)
    data["tax_categories"] = check_tax_categories(data.get("tax_categories"), (current or {}).get("tax_categories"))
    await require_destinations(session, company_id, {
        data[k] for k in ("asset_account", "accumulated_depreciation_account", "depreciation_account") if data.get(k)})


def new_class(body: dict, company_settings: dict | None, seq: int) -> dict:
    """A new class's record from what was entered, its accounts defaulting to the
    company's account roles. Checked by check_class before it is recorded."""
    from celerp.services.account_roles import role_map

    roles = role_map(company_settings)
    if body.get("measurement_policy") not in (None, "cost"):
        raise unprocessable("A new class uses the cost model. Change class to revaluation once its assets are in.")
    return {"name": str(body.get("name") or "").strip(), "kind": body.get("kind") or "tangible",
            "asset_account": body.get("asset_account") or roles.get("fixed_assets") or "",
            "accumulated_depreciation_account": (body.get("accumulated_depreciation_account")
                                                 or roles.get("accumulated_depreciation") or ""),
            "depreciation_account": body.get("depreciation_account") or roles.get("depreciation_expense") or "",
            "method": body.get("method") or "straight_line", "life_months": body.get("life_months"),
            "residual_percent": str(body.get("residual_percent") or "0"), "convention": body.get("convention"),
            "params": body.get("params"), "archived": False, "seq": seq,
            "measurement_policy": "cost", "active_market": _active_market(body.get("active_market")),
            "construction": body.get("construction", False), "tax_categories": body.get("tax_categories")}


async def create_class(session, company_id, user_id, body: dict) -> dict:
    from celerp.services.permissions import locked_authority

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await require_started(session, company_id)
    data = new_class(body, settings, len(await classes(session, company_id, include_archived=True)))
    await check_class(session, company_id, data, settings)
    entity_id = f"fa:class:{uuid.uuid4()}"
    await _emit(session, company_id, user_id, entity_id, "fa.class.created", data)
    return {"id": entity_id, **data}


def measurement(cls: dict) -> str:
    return cls.get("measurement_policy") or "cost"


async def _check_measurement(session, company_id, class_id: str, policy) -> None:
    """A class moves to revaluation only through its revaluation table; it moves back to
    cost while none of its assets carries a revaluation."""
    from celerp_fixed_assets import remeasure

    if policy not in MEASUREMENT_POLICIES:
        raise unprocessable("A class is measured at cost or by revaluation.")
    if policy == "revaluation":
        raise unprocessable("Use Change class to revaluation, which revalues every asset in the class on one date.")
    revalued = await remeasure.revalued_numbers(session, company_id, class_id)
    if revalued:
        raise HTTPException(status_code=409, detail=f"{', '.join(revalued)} are revalued, so this class stays on "
                                                    "the revaluation model.")


async def class_changes(session, company_id, company_settings: dict | None, class_id: str, current: dict,
                        body: dict) -> dict:
    """What ``body`` changes on a class, checked against the class it leaves."""
    unknown = set(body) - set(CLASS_FIELDS)
    if unknown:
        raise unprocessable(f"Unknown class field: {sorted(unknown)[0]}.")
    changes = {k: v for k, v in body.items() if current.get(k) != v}
    if "name" in changes:
        changes["name"] = str(changes["name"] or "").strip()
    if "archived" in changes and not isinstance(changes["archived"], bool):
        raise unprocessable("Archived is true or false.")
    if "measurement_policy" in changes:
        await _check_measurement(session, company_id, class_id, changes["measurement_policy"])
    if "active_market" in changes:
        changes["active_market"] = _active_market(changes["active_market"])
    checked = {"construction": False, **current, **changes}
    await check_class(session, company_id, checked, company_settings, current)
    for name in ("params", "tax_categories"):
        if name in changes:
            changes[name] = checked[name]
    return changes


async def update_class(session, company_id, user_id, class_id: str, body: dict) -> dict:
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    rows = await lock_projections(session, company_id, [class_id])
    row = rows.get(class_id)
    if row is None or row.entity_type != "fa_class":
        raise HTTPException(status_code=404, detail="No such asset class.")
    changes = await class_changes(session, company_id, settings, class_id, row.state, body)
    if changes:
        await _emit(session, company_id, user_id, class_id, "fa.class.updated", {"changes": changes})
    return {"id": class_id, **row.state, **changes}