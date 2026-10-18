# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Year end: what stands between a tax year and its close, and the close itself.

The items are the months of the year whose depreciation is not posted, the assets a run
skipped, the reconciliation differences still open, the sales waiting on their invoice,
the corrections still waiting for approval, the impairment reviews due by the year end,
and every asset or pool a chosen tax regime cannot work out. Only items dated on or before
the year end belong to it: a later sale or difference waits on the year it falls in, and a
correction with no effective date can restate any year, so it waits on every one. Each
can be noted, with a reason, or accepted, with a reason and its effect on the figures,
except a tax figure that is missing: it can only be noted, because closing keeps the
year's figures for every later year to start from. An accepted item holds for the amount
it was accepted at: when the amount changes, the item is open again. A tax year closes
once every other item is accepted and the regime worked out every asset and pool for it,
and closing keeps its tax figures as they stand (celerp_fixed_assets.taxbooks). A closed year reopens
only with a reason, and every close and reopen stays in the year's history.

Nothing here reads or writes the ledger.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException

from celerp.services.business_time import business_today, company_today

from celerp_fixed_assets import recon, remeasure, runs, setup, taxbooks
from celerp_fixed_assets.calc.periods import month_index, period_label
from celerp_fixed_assets.projections import money
from celerp_fixed_assets.operations import conflict
from celerp_fixed_assets.store import _emit, _entities, _state

MARK_STATES = ("open", "noted", "accepted")


def yearend_id(year: int) -> str:
    return f"fa:yearend:{year}"


def dates(settings: dict | None, year: int) -> tuple[date, date]:
    """The company's tax year ``year``: the day after the previous year end to its own."""
    month, day = (int(p) for p in taxbooks.tax_year_end(settings).split("-"))
    return date(year - 1, month, day) + timedelta(days=1), date(year, month, day)


async def _runs(session, company_id, start: date, end: date) -> list[dict]:
    books = await runs._books_if_started(session, company_id)
    if books is None:
        return []
    first = max(month_index(start), books.first)
    if books.last is not None:
        first = max(first, books.last + 1)
    through = month_index(end)
    if first > through:
        return []
    charges = await runs._charges(session, company_id, books, through, first)
    out = []
    for k in range(first, through + 1):
        amount = sum((c.owed.get(k, Decimal(0)) for c in charges), Decimal(0))
        if amount:
            out.append({"key": f"run:{period_label(k)}", "kind": "run", "date": None, "amount": money(amount, books.currency),
                        "label": ("fixed_assets.ye_run", {"month": runs._long(k)}), "href": "/fixed-assets/runs"})
    return out


async def _exceptions(session, company_id) -> list[dict]:
    currency = await setup.currency(session, company_id)
    return [{"key": f"exception:{a['asset_id']}", "kind": "exception", "date": f"{a['exception']['first_month']}-01",
             "amount": None if a["exception"].get("amount") is None
             else money(Decimal(a["exception"]["amount"]), currency),
             "label": ("fixed_assets.ye_exception", {"asset": a["number"], "month": a["exception"]["first_month"]}),
             "href": f"/fixed-assets/assets/{a['asset_id']}"} for a in await runs.attention(session, company_id)]


async def open_differences(session, company_id) -> list[dict]:
    from ui.i18n import t

    try:
        found = await recon.report(session, company_id)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return []
    return [{"key": f"difference:{d['key']}", "kind": "difference", "amount": d["amount"], "date": d["date"],
             "label": ("fixed_assets.ye_difference", {"account": a["name"], "cause": t(f"fixed_assets.recon_rule.{d['rule']}")}),
             "href": "/fixed-assets/reconciliation"}
            for a in found["accounts"] for d in a["differences"] if d["state"] != "accepted"]


async def pending_sales(session, company_id) -> list[dict]:
    waiting = sorted(((i, s) for i, s in await _entities(session, company_id, "fa_asset")
                      if (s.get("sale") or {}).get("state") == "pending"), key=lambda r: r[1]["number"])
    return [{"key": f"sale:{asset_id}", "kind": "sale", "amount": None, "date": a["sale"].get("date"),
             "label": ("fixed_assets.ye_sale", {"asset": a["number"]}),
             "href": f"/fixed-assets/assets/{asset_id}"} for asset_id, a in waiting]


async def _reviews(session, company_id, end: date) -> list[dict]:
    return [{"key": f"review:{r['asset_id']}", "kind": "review", "amount": None, "date": None,
             "label": ("fixed_assets.ye_review", {"asset": r["number"], "due": r["due"]}),
             "href": f"/fixed-assets/assets/{r['asset_id']}"}
            for r in await remeasure.due_reviews(session, company_id, end) if r["due"] <= end.isoformat()]


async def waiting_corrections(session, company_id) -> list[dict]:
    waiting = sorted(((i, s) for i, s in await _entities(session, company_id, "fa_correction")
                      if s.get("state") == "requested"), key=lambda r: r[0])
    out = []
    for correction_id, c in waiting:
        asset = await _state(session, company_id, c.get("asset_id")) or {}
        out.append({"key": f"correction:{correction_id}", "kind": "correction", "amount": None,
                    "date": c.get("effective_date") or None,
                    "label": ("fixed_assets.ye_correction", {"asset": asset.get("number") or ""}),
                    "href": f"/fixed-assets/corrections/{correction_id}"})
    return out


def _remedy(found: dict) -> str:
    """What sets a missing tax figure right."""
    if found["status"] == "no_category":
        return "Set its tax category on its class or its tax book."
    if found["status"] == "error":
        return f"{found.get('reason') or ''} Check the regime's module, then work the year out again.".strip()
    return found.get("reason") or ""


def _unworked(figures: dict) -> list[tuple[str, str, dict]]:
    """The year's assets and pools with no tax figure: (item key suffix, name, row)."""
    return [*((r["asset_id"], r["number"], r) for r in figures.get("rows") or [] if r.get("status") != "ok"),
            *((f"pool:{p['key']}", f"pool {p['key']}", p) for p in figures.get("pools") or []
              if p.get("status") != "ok")]


async def _tax(session, company_id, settings: dict, year: int) -> list[dict]:
    out = []
    for key in setup.settings_of(settings).get("tax_regimes") or []:
        if taxbooks.unavailable(settings, key):
            continue
        book = await taxbooks.book(session, company_id, key, year, settings)
        if book["status"] == "closed":
            continue
        for suffix, name, found in _unworked(book):
            out.append({"key": f"tax:{key}:{suffix}", "kind": "tax", "amount": None, "date": None,
                        "label": (f"fixed_assets.ye_tax_{found['status']}", {"asset": name, "regime": book["label"]}),
                        "remedy": _remedy(found), "href": f"/fixed-assets/tax-books?regime={key}&year={year}"})
    return out


async def report(session, company_id, year) -> dict:
    """The year's items, each with where it stands, and whether the year can close."""
    from ui.i18n import t

    year = taxbooks.check_year(year)
    settings = await setup.company_settings(session, company_id)
    start, end = dates(settings, year)
    cut = setup.cutover(setup.settings_of(settings))
    marks = (await _state(session, company_id, yearend_id(year)) or {}).get("marks") or {}
    items = []
    found = [*await _runs(session, company_id, start, end), *await _exceptions(session, company_id),
             *await open_differences(session, company_id), *await pending_sales(session, company_id),
             *await waiting_corrections(session, company_id), *await _reviews(session, company_id, end),
             *await _tax(session, company_id, settings, year)]
    for item in found:
        day = item.pop("date")
        if day is not None and day > end.isoformat():
            continue
        key, params = item.pop("label")
        mark = marks.get(item["key"])
        lapsed = mark is not None and mark["state"] != "open" and mark.get("amount") != item["amount"]
        held = mark if mark is not None and not lapsed else None
        if held is not None and item["kind"] == "tax" and held["state"] == "accepted":
            held = None  # accepted before a missing tax figure could only be noted
        items.append({**item, "label": t(key, **params), "state": held["state"] if held else "open",
                      "reason": held.get("reason") if held else None, "effect": held.get("effect") if held else None,
                      "accepted_amount": held.get("amount") if held and held["state"] == "accepted" else None,
                      "by": held.get("user") if held else None, "at": held.get("at") if held else None,
                      "lapsed": lapsed})
    # The register keeps a year once its cutover is confirmed and falls before the year end.
    register = "unconfirmed" if cut is None else "before" if end.isoformat() <= cut else "kept"
    return {"year": year, "start": start.isoformat(), "end": end.isoformat(), "items": items, "register": register,
            "cutover": cut, "ready": register == "kept" and all(i["state"] == "accepted" for i in items)}


async def mark(session, company_id, user_id, year, body: dict) -> dict:
    """Note an item, accept it with its reason and effect, or open it again."""
    from celerp.services.company_lock import lock_company
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await lock_company(session, company_id)
    key, state = str(body.get("key") or ""), body.get("state")
    if state not in MARK_STATES:
        raise setup.unprocessable("Choose open, noted or accepted.")
    reason = str(body.get("reason") or "").strip()
    effect = str(body.get("effect") or "").strip()
    if state != "open" and not reason:
        raise setup.unprocessable("A reason is required to note or accept a year-end item.")
    if state == "accepted" and not effect:
        raise setup.unprocessable("Describe the effect of accepting this item on the figures.")
    found = next((i for i in (await report(session, company_id, year))["items"] if i["key"] == key), None)
    if found is None:
        raise HTTPException(status_code=404, detail="There is no such year-end item. Reload the year end.")
    if state == "accepted" and found["kind"] == "tax":
        raise conflict(f"{found['label']}. A tax year keeps its figures for the years after it, so a missing "
                       f"figure cannot be accepted. {found['remedy']} Note it while it waits.")
    saved = await _state(session, company_id, yearend_id(year)) or {}
    marks = dict(saved.get("marks") or {})
    if state == "open":
        marks.pop(key, None)
    else:
        marks[key] = {"state": state, "reason": reason, "effect": effect or None, "amount": found["amount"],
                      "at": (await company_today(session, company_id)).isoformat(), "user": str(user_id)}
    await _emit(session, company_id, user_id, yearend_id(year), "fa_yearend", "fa.yearend.recorded",
                {"year": year, "marks": marks})
    return {**found, "state": state, "reason": reason or None, "effect": effect or None}


# ── Closing and reopening a tax year ─────────────────────────────────────────


def _history(saved: dict, settings: dict, action: str, user_id, reason: str | None = None) -> list:
    return [{"action": action, "reason": reason, "at": business_today(settings.get("timezone")).isoformat(), "user": str(user_id)},
            *(saved.get("history") or [])]


async def _year(session, company_id, user_id, regime_key: str, year):
    from celerp.services.company_lock import lock_company
    from celerp.services.permissions import locked_authority

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    await lock_company(session, company_id)
    regime = taxbooks.regime_for(settings, regime_key)
    year = taxbooks.check_year(year)
    reason = taxbooks.unavailable(settings, regime_key)
    if reason:
        raise conflict(reason)
    saved = await _state(session, company_id, taxbooks.taxyear_id(regime_key, year)) or {}
    return settings, regime, year, saved


async def close(session, company_id, user_id, regime_key: str, year) -> dict:
    """Close tax ``year`` under the regime, keeping its figures as they stand."""
    settings, regime, year, saved = await _year(session, company_id, user_id, regime_key, year)
    if saved.get("status") == "closed":
        raise conflict(f"Tax year {year} is already closed.")
    start, end = regime.tax_year(taxbooks.tax_year_end(settings), year)
    if end >= business_today(settings.get("timezone")):
        raise conflict(f"Tax year {year} ends on {end.isoformat()}, so it cannot close yet.")
    years = await taxbooks.years(session, company_id, settings, regime, year)
    earlier = [y for y, f in sorted(years.items()) if y < year and f.get("status") != "closed"
               and (f.get("rows") or f.get("pools"))]
    if earlier:
        raise conflict(f"Close tax year {earlier[0]} first.")
    figures = years.get(year) or {"rows": [], "pools": []}
    unworked = _unworked(figures)
    # Another regime's missing figures wait on that regime's own close.
    waiting = [i["key"] for i in (await report(session, company_id, year))["items"]
               if i["state"] != "accepted" and i["kind"] != "tax"]
    refusals = [*([f"Tax year {year} cannot close while it has no tax figure for "
                   + "; ".join(f"{name} ({_remedy(found)})" for _, name, found in unworked)] if unworked else []),
                *([f"These year-end items are not accepted: {', '.join(waiting)}."] if waiting else [])]
    if refusals:
        raise conflict(" ".join(refusals))
    data = {"regime": regime_key, "year": year, "status": "closed", "start": start.isoformat(),
            "end": end.isoformat(), "rows": figures["rows"], "pools": figures["pools"],
            "history": _history(saved, settings, "closed", user_id)}
    await _emit(session, company_id, user_id, taxbooks.taxyear_id(regime_key, year), "fa_taxyear",
                "fa.taxyear.recorded", data)
    return data


async def reopen(session, company_id, user_id, regime_key: str, year, body: dict) -> dict:
    """Reopen a closed tax year, with the reason. Its figures are worked out afresh."""
    settings, regime, year, saved = await _year(session, company_id, user_id, regime_key, year)
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise setup.unprocessable("A reason is required to reopen a tax year.")
    if saved.get("status") != "closed":
        raise conflict(f"Tax year {year} is not closed.")
    later = sorted(int(s["year"]) for _, s in await _entities(session, company_id, "fa_taxyear")
                   if s.get("regime") == regime_key and s.get("status") == "closed" and int(s["year"]) > year)
    if later:
        raise conflict(f"Reopen tax year {later[-1]} first.")
    data = {**saved, "status": "open", "rows": [], "pools": [],
            "history": _history(saved, settings, "reopened", user_id, reason)}
    await _emit(session, company_id, user_id, taxbooks.taxyear_id(regime_key, year), "fa_taxyear",
                "fa.taxyear.recorded", data)
    return data
