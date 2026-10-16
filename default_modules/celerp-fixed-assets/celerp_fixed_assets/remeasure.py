# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Revaluation, impairment and its reversal, and the annual impairment review.

Each remeasurement is one journal per asset, dated the measurement date. Depreciation is
first brought up to date through that month (and restated for any later month a run has
already charged), then the carrying amount at the end of the month is measured again:

- Revaluation (a class on the revaluation model) eliminates accumulated depreciation and
  impairment against cost, so cost becomes the fair value. An increase first reverses
  what earlier decreases charged to profit or loss, the rest goes to the revaluation
  surplus; a decrease first uses the surplus, the rest is a loss.
- Impairment writes the carrying amount down to the recoverable amount, against the
  surplus first on a revalued asset, the rest a loss.
- Its reversal writes back no more than the impairment and, in profit or loss, no more
  than the losses still to reverse or the carrying amount the asset would have had had it
  never been remeasured. On the revaluation model the rest goes to the surplus.

The new carrying amount depreciates over the life left from the month after.

A class moves to the revaluation model by revaluing every asset in it on one date. An
intangible class is revalued only against an active market. An intangible with an
indefinite life is reviewed for impairment at least once a year.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from celerp.services.money import books_currency
from celerp_fixed_assets import disposal, notices, projections, runs, setup
from celerp_fixed_assets.calc.periods import month_index, parse_period, period_label
from celerp_fixed_assets.operations import (
    _cents,
    _date,
    _dec,
    _effects,
    _journal_view,
    _lines,
    _move,
    _post,
    _run,
    asset_movements,
)
from celerp_fixed_assets.store import _emit, _entities, _state
from fastapi import HTTPException

_AD, _IMP, _SURPLUS = "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus"
_ZERO = Decimal(0)
REVIEW_OUTCOMES = ("no_indication", "recoverable_amount")
REVIEWS_URL = "/fixed-assets/reviews"


def _money(value, field: str, currency: str) -> Decimal:
    from celerp.services.money import round_money

    if value in (None, ""):
        raise setup.unprocessable(f"Enter the {field}.")
    amount = setup.number(value, f"The {field}")
    if amount < 0:
        raise setup.unprocessable(f"The {field} must be zero or more.")
    return round_money(amount, currency)


def _basis(body: dict) -> str | None:
    return str(body.get("basis") or "").strip() or None


def _position(books: runs.Books, asset: dict, movements: list[dict], day: str) -> dict:
    """Where the asset stands at the end of ``day``'s month: the depreciation that brings it
    up to date, its carrying amount, what it would carry had it never been remeasured, and
    the losses in profit or loss its remeasurements have not yet reversed."""
    month = month_index(date.fromisoformat(day))
    live = projections.live(movements)
    charged = runs.charged_by_month(movements)
    depreciates = asset["policy"]["method"] != "none"
    restated: dict[int, Decimal] = {}
    base = [m for m in live if m.get("operation") not in projections.REMEASUREMENTS]
    cost_base = sum((_dec((m.get("amounts") or {}).get("cost")) for m in base), _ZERO)
    opening = sum((_dec((m.get("amounts") or {}).get(k)) for m in base if m.get("operation") in runs.OPENING_OPERATIONS
                   for k in (_AD, _IMP)), _ZERO)
    shadow = cost_base - opening
    if depreciates:
        planned = runs.full_schedule(asset, movements, books.settings, month)
        missing = [p.period for p in planned.periods if p.exception is not None]
        if missing:
            raise setup.unprocessable(f"Enter the reading for {missing[0]} on {asset['number']} first.")
        restated = {parse_period(p.period): p.amount for p in planned.periods}
        never = runs.full_schedule(asset, base, books.settings, month)
        shadow -= sum((p.amount for p in never.periods), _ZERO)
    caught_up = sum((restated.get(k, _ZERO) - charged.get(k, _ZERO) for k in {*restated, *charged} if k <= month),
                    _ZERO)
    later = sum((v for k, v in charged.items() if k > month), _ZERO)
    accumulated = _dec(asset.get(_AD)) + caught_up - later
    carrying = _dec(asset.get("cost")) - accumulated - _dec(asset.get(_IMP))
    losses = sum((_dec(m.get("pnl")) for m in live if m.get("operation") in projections.REMEASUREMENTS), _ZERO)
    return {"month": month, "accumulated": accumulated, "carrying": carrying, "shadow": max(shadow, _ZERO),
            "losses": max(losses, _ZERO), "charged": charged}


def _depreciation(books: runs.Books, asset: dict, movements: list[dict], day: str, operation: str,
                  carrying: Decimal, month: int, charged: dict) -> dict[int, Decimal]:
    """Depreciation to post, by month, through the later of the measurement month and the
    last month run: what the schedule with the remeasurement owes beyond what is charged."""
    if asset["policy"]["method"] == "none":
        return {}
    through = max(month, books.last if books.last is not None else month)
    new = {"id": "fa:mv:remeasure", "operation": operation, "effective_date": day, "amounts": {},
           "carrying_amount": str(carrying)}
    planned = runs.full_schedule(asset, [*movements, new], books.settings, through)
    restated = {parse_period(p.period): p.amount for p in planned.periods}
    diff = {k: restated.get(k, _ZERO) - charged.get(k, _ZERO) for k in sorted({*restated, *charged}) if k <= through}
    return {k: v for k, v in diff.items() if v}


def _plan(books: runs.Books, asset_id: str, asset: dict, cls: dict, movements: list[dict], day: str,
          operation: str, value: Decimal, basis: str | None) -> dict:
    """What remeasuring one asset on ``day`` at ``value`` posts and moves. ``value`` is the
    fair value for a revaluation and the recoverable amount for an impairment or its reversal."""
    from celerp.services.module_journals import JournalLine

    if asset.get("status") != "active":
        raise HTTPException(status_code=409, detail=f"{asset['number']} is not in use, so it cannot be remeasured.")
    disposal.check_date(books, asset, projections.live(movements), day, what="measurement", verb="Measure")
    at = _position(books, asset, movements, day)
    carrying, impairment, surplus = at["carrying"], _dec(asset.get(_IMP)), _dec(asset.get(_SURPLUS))
    roles = disposal.accounts(books)
    revalues = setup.measurement(cls) == "revaluation"
    accumulated_account = cls["accumulated_depreciation_account"]
    lines: list = []
    amounts = {"cost": _ZERO, _IMP: _ZERO, _SURPLUS: _ZERO}
    to_pnl = to_surplus = _ZERO
    extra: dict = {}
    if operation == "revaluation":
        delta = value - carrying
        eliminated = at["accumulated"] + impairment
        if delta >= 0:
            to_pnl = -min(delta, at["losses"], max(at["shadow"] - carrying, _ZERO))
            to_surplus = delta + to_pnl
        else:
            to_surplus = -min(-delta, surplus)
            to_pnl = -delta + to_surplus
        if eliminated:
            lines += _lines(accumulated_account, cls["asset_account"], eliminated)
        if delta > 0:
            lines.append(JournalLine(account=cls["asset_account"], debit=delta))
        lines += _surplus_and_pnl(roles, to_surplus, to_pnl)
        if delta < 0:
            lines.append(JournalLine(account=cls["asset_account"], credit=-delta))
        amounts = {"cost": delta - eliminated, _IMP: -impairment, _SURPLUS: to_surplus}
        new_carrying = value
        accumulated_after = -at["accumulated"]
    elif operation == "impairment":
        if value >= carrying:
            raise setup.unprocessable(f"The recoverable amount {_cents(value)} is not below the carrying amount "
                                      f"{_cents(carrying)} of {asset['number']}, so it is not impaired.")
        loss = carrying - value
        to_surplus = -min(loss, surplus) if revalues else _ZERO
        to_pnl = loss + to_surplus
        lines += _surplus_and_pnl(roles, to_surplus, to_pnl)
        lines.append(JournalLine(account=accumulated_account, credit=loss))
        amounts = {"cost": _ZERO, _IMP: loss, _SURPLUS: to_surplus}
        new_carrying = value
        accumulated_after = _ZERO
    else:
        if not impairment:
            raise setup.unprocessable(f"{asset['number']} has no impairment to reverse.")
        if value <= carrying:
            raise setup.unprocessable(f"The recoverable amount {_cents(value)} is not above the carrying amount "
                                      f"{_cents(carrying)} of {asset['number']}, so there is nothing to reverse.")
        reversal = min(value - carrying, impairment)
        to_pnl = -min(reversal, at["losses"], max(at["shadow"] - carrying, _ZERO))
        if not revalues:
            reversal = -to_pnl
        if not reversal:
            raise setup.unprocessable(f"{asset['number']} already carries what it would have without the "
                                      f"impairment ({_cents(at['shadow'])}), so there is nothing to reverse.")
        to_surplus = reversal + to_pnl
        lines.append(JournalLine(account=accumulated_account, debit=reversal))
        lines += _surplus_and_pnl(roles, to_surplus, to_pnl)
        amounts = {"cost": _ZERO, _IMP: -reversal, _SURPLUS: to_surplus}
        new_carrying = carrying + reversal
        accumulated_after = _ZERO
        extra = {"reversal": _cents(reversal), "limit": _cents(at["shadow"])}
    to_pnl, to_surplus = to_pnl + 0, to_surplus + 0  # no negative zero
    diff = _depreciation(books, asset, movements, day, operation, new_carrying, at["month"], at["charged"])
    depreciation = sum(diff.values(), _ZERO)
    if depreciation:
        expense = asset.get("depreciation_account") or cls["depreciation_account"]
        lines = _lines(expense, accumulated_account, depreciation) + lines
    amounts[_AD] = depreciation + accumulated_after
    shown = {"number": asset["number"], "name": asset["name"], "depreciation": _cents(depreciation),
             "carrying_amount": _cents(carrying), "measured": _cents(value), "new_carrying_amount": _cents(new_carrying),
             "surplus": _cents(to_surplus), "profit_or_loss": _cents(-to_pnl), **extra}
    return {"asset_id": asset_id, "asset": asset, "shown": shown, "lines": lines, "amounts": amounts,
            "periods": {period_label(k): str(v) for k, v in diff.items()}, "pnl": to_pnl,
            "carrying_amount": new_carrying, "value": value, "basis": basis}


def _surplus_and_pnl(roles: dict, to_surplus: Decimal, to_pnl: Decimal) -> list:
    """The surplus and profit-or-loss lines: a positive ``to_surplus`` credits the surplus,
    a positive ``to_pnl`` is a loss (a debit)."""
    from celerp.services.module_journals import JournalLine

    out = []
    if to_surplus < 0:
        out.append(JournalLine(account=roles["surplus"](), debit=-to_surplus))
    if to_pnl > 0:
        out.append(JournalLine(account=roles["impairment"](), debit=to_pnl))
    elif to_pnl < 0:
        out.append(JournalLine(account=roles["impairment"](), credit=-to_pnl))
    if to_surplus > 0:
        out.append(JournalLine(account=roles["surplus"](), credit=to_surplus))
    return out


async def _record(session, company_id, user_id, item: dict, day: str, operation: str, op_key: str) -> tuple[str, str]:
    asset_id, asset = item["asset_id"], item["asset"]
    short = asset_id.removeprefix("fa:asset:")
    memo = {"revaluation": "revalued", "impairment": "impaired", "impairment_reversal": "impairment reversed"}
    je_id = await _post(session, company_id, user_id,
                        key=f"asset:{short}:{operation}:{int(asset.get('movement_count') or 0) + 1}",
                        operation=operation, day=day, memo=f"{asset['number']} {asset['name']} {memo[operation]}",
                        lines=item["lines"], source_id=asset_id, href=f"/fixed-assets/assets/{asset_id}",
                        label=asset["number"], op_key=op_key)
    movement_id = await _move(session, company_id, user_id, asset_id, kind="posted", operation=operation,
                              effective=day, posting=day, amounts=item["amounts"], op_key=op_key,
                              periods=item["periods"], pnl=str(item["pnl"]),
                              carrying_amount=str(item["carrying_amount"]), measured=str(item["value"]),
                              basis=item["basis"], journal_id=je_id)
    return je_id, movement_id


async def _asset_and_class(session, company_id, asset_id: str):
    from celerp.services.company_lock import lock_projections

    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    return row.state, await setup.get_class(session, company_id, row.state.get("class_id"))


async def class_assets(session, company_id, class_id: str) -> list[tuple[str, dict]]:
    """The assets in use in a class, by number."""
    return sorted(((eid, s) for eid, s in await _entities(session, company_id, "fa_asset")
                   if s.get("class_id") == class_id and s.get("status") == "active"),
                  key=lambda r: r[1].get("number") or "")


def _needs_market(cls: dict) -> None:
    if cls.get("kind") == "intangible" and not cls.get("active_market"):
        raise setup.unprocessable(f"{cls['name']} is intangible, so it is revalued only by reference to an active "
                                  "market. Record the active market on the class first.")


async def remeasure(session, company_id, user_id, asset_id: str, body: dict, operation: str) -> dict:
    """Revalue, impair or reverse the impairment of one asset."""
    field = "fair_value" if operation == "revaluation" else "recoverable_amount"

    async def plan(fa):
        day = _date(body.get("date"), "The measurement date")
        asset, cls = await _asset_and_class(session, company_id, asset_id)
        if operation == "revaluation":
            if setup.measurement(cls) != "revaluation":
                count = len(await class_assets(session, company_id, cls["id"]))
                raise setup.unprocessable(f"This class uses the cost model. Change class to revaluation revalues all "
                                          f"{count} assets in it on one date.")
            _needs_market(cls)
        books = await runs._books(session, company_id)
        currency = books_currency(books.settings)
        value = _money(body.get(field), field.replace("_", " "), currency)
        item = _plan(books, asset_id, asset, cls, await asset_movements(session, company_id, asset_id), day,
                     operation, value, _basis(body))
        shown = {"date": day, **item["shown"], "journals": [_journal_view(item["lines"])]}
        basis = [asset.get("version"), asset.get("movement_count"), await runs._ledger_basis(session, company_id, books)]
        return shown, {"item": item, "day": day, "basis": basis}

    async def apply(context, op_key):
        item = context["item"]
        je_id, movement_id = await _record(session, company_id, user_id, item, context["day"], operation, op_key)
        return ({"assets": [{"id": asset_id, "number": item["asset"]["number"]}], "journals": [je_id]},
                _effects(movements=[movement_id], journals=[je_id]))

    return await _run(session, company_id, user_id, body, op_type=operation, keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def revalue_class(session, company_id, user_id, class_id: str, body: dict) -> dict:
    """Revalue every asset in a class on one date; a class on the cost model moves to the
    revaluation model with it. The preview names any asset still without a fair value."""
    from celerp.services.company_lock import lock_projections

    async def plan(fa):
        day = _date(body.get("date"), "The revaluation date")
        cls = await setup.get_class(session, company_id, class_id)
        _needs_market(cls)
        rows = body.get("rows")
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            raise setup.unprocessable("Enter a fair value for each asset in the class.")
        members = dict(await class_assets(session, company_id, cls["id"]))
        ids = [str(r.get("asset_id") or "") for r in rows]
        if len(set(ids)) != len(ids):
            raise setup.unprocessable("Each asset is revalued once.")
        strangers = [i for i in ids if i not in members]
        if strangers:
            raise setup.unprocessable(f"Only assets in use in {cls['name']} are revalued with it.")
        missing = [s["number"] for eid, s in members.items() if eid not in ids]
        if missing and not body.get("preview"):
            raise setup.unprocessable(f"Every asset in {cls['name']} is revalued on the same date. Enter a fair value "
                                      f"for {', '.join(missing)}.")
        locked = await lock_projections(session, company_id, ids)
        books = await runs._books(session, company_id)
        currency = books_currency(books.settings)
        items = []
        for row in rows:
            asset_id = row["asset_id"]
            value = _money(row.get("fair_value"), f"fair value of {members[asset_id]['number']}", currency)
            items.append(_plan(books, asset_id, locked[asset_id].state, cls,
                               await asset_movements(session, company_id, asset_id), day, "revaluation", value,
                               _basis(row)))
        totals = {k: _cents(sum((_dec(i["shown"][field]) for i in items), _ZERO))
                  for k, field in (("carrying_amount", "carrying_amount"), ("fair_value", "measured"),
                                   ("surplus", "surplus"), ("profit_or_loss", "profit_or_loss"))}
        shown = {"date": day, "class": cls["name"], "missing": missing, "totals": totals,
                 "assets": [i["shown"] for i in items], "journals": [_journal_view(i["lines"]) for i in items]}
        basis = [[(i["asset"].get("version"), i["asset"].get("movement_count")) for i in items],
                 cls.get("version"), await runs._ledger_basis(session, company_id, books)]
        return shown, {"items": items, "day": day, "cls": cls, "totals": totals, "basis": basis}

    async def apply(context, op_key):
        cls = context["cls"]
        assets, journals, movements = [], [], []
        for item in context["items"]:
            je_id, movement_id = await _record(session, company_id, user_id, item, context["day"], "revaluation",
                                               op_key)
            assets.append({"id": item["asset_id"], "number": item["asset"]["number"]})
            journals.append(je_id)
            movements.append(movement_id)
        classes = []
        if setup.measurement(cls) != "revaluation":
            await setup._emit(session, company_id, user_id, cls["id"], "fa.class.updated",
                              {"changes": {"measurement_policy": "revaluation"}})
            classes.append({"id": cls["id"], "before": {"measurement_policy": setup.measurement(cls)}})
        return ({"assets": assets, "journals": journals, "totals": context["totals"]},
                _effects(movements=movements, journals=journals, classes=classes))

    return await _run(session, company_id, user_id, body, op_type="revaluation", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def revalued_numbers(session, company_id, class_id: str) -> list[str]:
    """The assets in use in a class that carry a revaluation."""
    out = []
    for asset_id, asset in await class_assets(session, company_id, class_id):
        if any(m.get("operation") == "revaluation"
               for m in projections.live(await asset_movements(session, company_id, asset_id))):
            out.append(asset["number"])
    return out


# ── Annual impairment review ─────────────────────────────────────────────────


def _reviewed_yearly(asset: dict, cls: dict | None) -> bool:
    """An intangible in use with an indefinite life is reviewed for impairment every year."""
    return (asset.get("status") == "active" and cls is not None and cls.get("kind") == "intangible"
            and (asset.get("policy") or {}).get("method") == "none")


def _a_year_after(day: str) -> str:
    d = date.fromisoformat(day)
    try:
        return d.replace(year=d.year + 1).isoformat()
    except ValueError:
        return d.replace(year=d.year + 1, day=28).isoformat()


async def reviews(session, company_id, asset_id: str) -> list[dict]:
    """An asset's active reviews, the oldest first."""
    from celerp.models.projections import ASSET_ID, Projection
    from sqlalchemy import select

    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_review", ASSET_ID == asset_id))).all()
    return sorted(({"id": eid, **s} for eid, s in rows if s.get("status") == "active"),
                  key=lambda r: (r["date"], r.get("recorded_at") or ""))


def _due(asset: dict, history: list[dict]) -> str | None:
    last = history[-1]["date"] if history else asset.get("available_date")
    return _a_year_after(last) if last else None


async def asset_facts(session, company_id, asset_id: str, asset: dict) -> dict:
    """What the asset page shows beside the asset's own fields: when its next impairment
    review is due, and whether it is still under construction."""
    cls = await _state(session, company_id, str(asset.get("class_id") or ""))
    due = _due(asset, await reviews(session, company_id, asset_id)) if _reviewed_yearly(asset, cls) else None
    return {"review_due": due,
            "under_construction": bool(cls and cls.get("construction")) and asset.get("status") == "active"}


async def due_reviews(session, company_id, today: date) -> list[dict]:
    """Reviews due within the next 30 days or overdue, the soonest first."""
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    horizon = (today + timedelta(days=30)).isoformat()
    out = []
    for asset_id, asset in await _entities(session, company_id, "fa_asset"):
        if not _reviewed_yearly(asset, classes.get(asset.get("class_id"))):
            continue
        due = _due(asset, await reviews(session, company_id, asset_id))
        if due and due <= horizon:
            out.append({"asset_id": asset_id, "number": asset["number"], "name": asset["name"], "due": due,
                        "overdue": due < today.isoformat()})
    return sorted(out, key=lambda r: (r["due"], r["number"]))


async def tell_reviews(session, company_id, today: date) -> None:
    due = [r for r in await due_reviews(session, company_id, today) if r["due"] <= today.isoformat()]
    if due:
        await notices.tell(session, company_id, notices.REVIEWS,
                           f"{len(due)} assets are due an impairment review.", action_url=REVIEWS_URL,
                           count=len(due))
    else:
        await notices.clear(session, company_id, notices.REVIEWS)


async def record_review(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Record an impairment review: no indication of impairment, or the recoverable amount
    found. A recoverable amount below the carrying amount returns the impairment to make."""
    from celerp.services.business_time import business_date_of
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await setup.require_started(session, company_id)
    settings = await setup.company_settings(session, company_id)
    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    asset = row.state
    if asset.get("status") != "active":
        raise HTTPException(status_code=409, detail=f"{asset['number']} is not in use, so it is not reviewed.")
    day = _date(body.get("date"), "The review date")
    today = business_date_of(None, settings.get("timezone"))
    if day > today:
        raise setup.unprocessable(f"The review date {day} is after today.")
    if day < (asset.get("available_date") or day):
        raise setup.unprocessable(f"The review date is before {asset['number']} was available for use.")
    outcome = body.get("outcome")
    if outcome not in REVIEW_OUTCOMES:
        raise setup.unprocessable("Choose what the review found: no indication of impairment, or a recoverable "
                                  "amount.")
    basis = _basis(body)
    if not basis:
        raise setup.unprocessable("Say what the review was based on.")
    currency = books_currency(settings)
    recoverable = (_money(body.get("recoverable_amount"), "recoverable amount", currency)
                   if outcome == "recoverable_amount" else None)
    review_id = f"fa:review:{asset_id.removeprefix('fa:asset:')}:{uuid.uuid4()}"
    data = {"asset_id": asset_id, "date": day, "outcome": outcome, "basis": basis,
            "recoverable_amount": None if recoverable is None else str(recoverable), "status": "active"}
    await _emit(session, company_id, user_id, review_id, "fa_review", "fa.review.recorded", data)
    await session.flush()
    carrying = _dec(asset.get("cost")) - _dec(asset.get(_AD)) - _dec(asset.get(_IMP))
    impairment = ({"date": day, "recoverable_amount": _cents(recoverable), "basis": basis}
                  if recoverable is not None and recoverable < carrying else None)
    found = await reviews(session, company_id, asset_id)
    return {"id": review_id, **data, "review_due": _due(asset, found), "impairment": impairment}


async def withdraw_review(session, company_id, user_id, asset_id: str, review_id: str) -> dict:
    """Withdraw an asset's latest review, entered in error."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await lock_projections(session, company_id, [asset_id])
    found = await reviews(session, company_id, asset_id)
    if not found or found[-1]["id"] != review_id:
        raise HTTPException(status_code=409, detail="Only the latest review can be withdrawn.")
    await _emit(session, company_id, user_id, review_id, "fa_review", "fa.review.status", {"status": "withdrawn"})
    return {"id": review_id, "status": "withdrawn"}
