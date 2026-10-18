# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed asset reports, read from the movements.

Every movement is dated as its journal posted it, so each period of the schedule and of
depreciation by period ties to the ledger. The schedule rolls each asset forward over a
period: what it stood at when the period opened, what moved it during the period by kind
of movement, and where it closed, with the depreciation runs still post for the period.
Assets are grouped under the class each movement belongs to, so a reclassification moves
the asset between classes in the transfers column. Depreciation by period lists each
month's posted charge, what a later change took back in the month it posted, the months
skipped with their reason, and the forecast left to post; disposals lists each asset
leaving the books with what it fetched.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from celerp.services.money import books_currency
from celerp_fixed_assets import projections, recon, runs, setup, taxbooks
from celerp_fixed_assets.calc.periods import month_index, period_label
from celerp_fixed_assets.projections import money
from celerp_fixed_assets.operations import _dec
from celerp_fixed_assets.sales import _credited, doc_number
from celerp_fixed_assets.store import _entities, _state, movements_by_asset

_ZERO = Decimal(0)
# The cost column each operation moves within the period; any other operation is an addition.
_COST_COLUMNS = {
    "improvement": "improvements", "replacement": "improvements",
    "construction_out": "transfers", "place_in_service": "transfers",
    "reclassify": "transfers",
    "revaluation": "revaluation",
    "disposal": "disposals", "sale_return": "disposals", "sale_reverse": "disposals",
}
COST_COLUMNS = ("cost_opening", "additions", "improvements", "transfers", "revaluation", "disposals", "cost_closing")
DEPRECIATION_COLUMNS = ("depreciation_opening", "depreciation_charge", "depreciation_disposals",
                        "depreciation_other", "depreciation_closing")
IMPAIRMENT_COLUMNS = ("impairment_opening", "impairment_charge", "impairment_disposals", "impairment_closing")
FIGURES = (*COST_COLUMNS, *DEPRECIATION_COLUMNS, *IMPAIRMENT_COLUMNS, "nbv_opening", "nbv_closing",
           "planned_not_posted")


def _day(value, field: str) -> date:
    return setup.day(value, field)


async def _range(session, company_id, date_from, date_to) -> tuple[dict, date, date]:
    """The settings and the period, refused when it starts on or before the cutover."""
    fa = await setup.require_managed(session, company_id)
    start, end = _day(date_from, "The start date"), _day(date_to, "The end date")
    if end < start:
        raise setup.unprocessable("The end date must be on or after the start date.")
    first = fa["first_managed_period"]
    if start.isoformat() < first:
        raise setup.unprocessable(f"Reports start on or after the first managed period, {first}. "
                                  "What the books held before it is the opening register.")
    return fa, start, end


async def _register(session, company_id) -> tuple[list[tuple[str, dict]], dict[str, list[dict]], dict]:
    assets = sorted(await _entities(session, company_id, "fa_asset"), key=lambda r: r[1].get("number") or "")
    movements = await movements_by_asset(session, company_id)
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    return assets, movements, classes


def _charge(mv: dict) -> Decimal:
    """The depreciation a movement charged: what it says it charged per month, or all of
    its accumulated depreciation for a run or a depreciation entry."""
    if mv.get("periods") is not None:
        return sum((_dec(a) for a in mv["periods"].values()), _ZERO)
    if mv.get("operation") in ("depreciation", "run"):
        return _dec((mv.get("amounts") or {}).get("accumulated_depreciation"))
    return _ZERO


def _roll(movements: list[dict], start: str, end: str) -> dict[str, Decimal]:
    out = {k: _ZERO for k in FIGURES}
    for mv in projections.live(movements):
        amounts = mv.get("amounts") or {}
        cost, ad = _dec(amounts.get("cost")), _dec(amounts.get("accumulated_depreciation"))
        ai = _dec(amounts.get("accumulated_impairment"))
        op, day = mv.get("operation"), mv["posting_date"]
        if op in runs.OPENING_OPERATIONS or day < start:
            out["cost_opening"] += cost
            out["depreciation_opening"] += ad
            out["impairment_opening"] += ai
            continue
        if day > end:
            continue
        out[_COST_COLUMNS.get(op, "additions")] += cost
        charge = _charge(mv)
        out["depreciation_charge"] += charge
        out["depreciation_disposals" if _COST_COLUMNS.get(op) == "disposals" else "depreciation_other"] += ad - charge
        out["impairment_disposals" if _COST_COLUMNS.get(op) == "disposals" else "impairment_charge"] += ai
    out["cost_closing"] = sum((out[k] for k in COST_COLUMNS[:-1]), _ZERO)
    out["depreciation_closing"] = sum((out[k] for k in DEPRECIATION_COLUMNS[:-1]), _ZERO)
    out["impairment_closing"] = sum((out[k] for k in IMPAIRMENT_COLUMNS[:-1]), _ZERO)
    out["nbv_opening"] = out["cost_opening"] - out["depreciation_opening"] - out["impairment_opening"]
    out["nbv_closing"] = out["cost_closing"] - out["depreciation_closing"] - out["impairment_closing"]
    return out


def _by_class(movements: list[dict], closing: str | None) -> dict[str | None, list[dict]]:
    """An asset's live movements by the class they belong to. Each reclassification's
    movement out of a class names that class, which everything recorded before it belongs
    to; what follows the last one belongs to the class the period closes in."""
    out: dict[str | None, list[dict]] = {}
    current = closing
    for mv in reversed(projections.live(movements)):
        if mv.get("operation") == "reclassify" and mv.get("side") == "out":
            current = mv["class_id"]
        out.setdefault(current, []).insert(0, mv)
    return out


async def _owed(session, company_id, months: range) -> tuple[dict[str, dict[int, Decimal]], dict[str, Decimal]]:
    """What runs still post per asset for ``months``, where the next runs post it: a month
    not yet run nor locked in its own month, and what is owed for a month already run or
    locked once, caught up by the next run."""
    books = await runs._books(session, company_id)
    forecast: dict[str, dict[int, Decimal]] = {}
    caught_up: dict[str, Decimal] = {}
    for charge in await runs._charges(session, company_id, books, months[-1], books.next):
        for k in months:
            amount = charge.owed.get(k, _ZERO)
            if k >= books.next and not books.locked(k):
                forecast.setdefault(charge.asset_id, {})[k] = amount
            else:
                caught_up[charge.asset_id] = caught_up.get(charge.asset_id, _ZERO) + amount
    return forecast, caught_up


def _shown(figures: dict[str, Decimal], currency: str) -> dict[str, str]:
    return {k: money(v, currency) for k, v in figures.items()}


async def schedule(session, company_id, date_from, date_to) -> dict:
    _, start, end = await _range(session, company_id, date_from, date_to)
    currency = await setup.currency(session, company_id)
    assets, movements, classes = await _register(session, company_id)
    forecast, caught_up = await _owed(session, company_id, range(month_index(start), month_index(end) + 1))
    groups: dict[str | None, dict] = {}
    for asset_id, asset in assets:
        moved = movements.get(asset_id) or []
        if not projections.live(moved):
            continue
        closing = recon._class_at(asset, (end + timedelta(days=1)).isoformat())
        for class_id, held in _by_class(moved, closing).items():
            figures = _roll(held, start.isoformat(), end.isoformat())
            if class_id == closing:
                figures["planned_not_posted"] = sum((forecast.get(asset_id) or {}).values(),
                                                    caught_up.get(asset_id, _ZERO))
            if not any(figures.values()):
                continue
            group = groups.setdefault(class_id, {"id": class_id, "name": (classes.get(class_id) or {}).get("name"),
                                                 "figures": {k: _ZERO for k in FIGURES}, "assets": []})
            for k in FIGURES:
                group["figures"][k] += figures[k]
            group["assets"].append({"id": asset_id, "number": asset.get("number"), "name": asset.get("name"),
                                    **_shown(figures, currency)})
    totals = {k: sum((g["figures"][k] for g in groups.values()), _ZERO) for k in FIGURES}
    rows = sorted(groups.values(), key=lambda g: g["name"] or "")
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "currency": currency,
            "classes": [{"id": g["id"], "name": g["name"], **_shown(g["figures"], currency), "assets": g["assets"]}
                        for g in rows],
            "totals": _shown(totals, currency)}


def _posted(movements: list[dict]) -> dict[tuple[int, bool], Decimal]:
    """The depreciation each journal posted on the asset, by the month of the journal and
    whether it was charged or taken back: a later change dated back takes back what runs
    after its date charged, on its own journal."""
    out: dict[tuple[int, bool], Decimal] = {}
    for mv in projections.live(movements):
        amount = _charge(mv)
        if amount and mv.get("operation") not in runs.OPENING_OPERATIONS:
            key = (month_index(date.fromisoformat(mv["posting_date"])), amount < 0)
            out[key] = out.get(key, _ZERO) + amount
    return out


async def depreciation(session, company_id, date_from, date_to) -> dict:
    """Each month's depreciation as its journals posted it per asset, what a later change
    took back in the month it posted, the months skipped, and the forecast: what runs still
    post, each month not yet run in its month and the rest once, caught up by the next run.
    Newest month first."""
    _, start, end = await _range(session, company_id, date_from, date_to)
    currency = await setup.currency(session, company_id)
    assets, movements, _ = await _register(session, company_id)
    months = range(month_index(start), month_index(end) + 1)
    forecast, caught_up = await _owed(session, company_id, months)
    out = {k: {"month": period_label(k), "posted": _ZERO, "forecast": _ZERO, "assets": [], "skipped": []}
           for k in months}
    owing = []
    for asset_id, asset in assets:
        named = {"id": asset_id, "number": asset.get("number"), "name": asset.get("name")}
        posted = _posted(movements.get(asset_id) or [])
        ahead = forecast.get(asset_id) or {}
        skips = asset.get("skips") or {}
        for k in months:
            if period_label(k) in skips:
                out[k]["skipped"].append({**named, "reason": skips[period_label(k)]})
            charged, taken_back, due = posted.get((k, False), _ZERO), posted.get((k, True), _ZERO), ahead.get(k, _ZERO)
            if charged or due:
                out[k]["assets"].append({**named, "taken_back": False, "posted": money(charged, currency),
                                         "forecast": money(due, currency)})
            if taken_back:
                out[k]["assets"].append({**named, "taken_back": True, "posted": money(taken_back, currency),
                                         "forecast": money(_ZERO, currency)})
            out[k]["posted"] += charged + taken_back
            out[k]["forecast"] += due
        if caught_up.get(asset_id):
            owing.append({**named, "amount": caught_up[asset_id]})
    rows = [out[k] for k in reversed(months)]
    behind = sum((a["amount"] for a in owing), _ZERO)
    return {"date_from": start.isoformat(), "date_to": end.isoformat(),
            "months": [{**m, "posted": money(m["posted"], currency), "forecast": money(m["forecast"], currency)}
                       for m in rows],
            "caught_up": [{**a, "amount": money(a["amount"], currency)} for a in owing],
            "totals": {"posted": money(sum((m["posted"] for m in rows), _ZERO), currency),
                       "caught_up_next_run": money(behind, currency),
                       "forecast": money(sum((m["forecast"] for m in rows), behind), currency)}}


def _carrying(mv: dict) -> Decimal:
    """The carrying amount that left the books: the cost removed less the depreciation and
    impairment removed with it (the movement also carries the charge to the disposal date)."""
    amounts = mv.get("amounts") or {}
    removed_ad = _charge(mv) - _dec(amounts.get("accumulated_depreciation"))
    return -_dec(amounts.get("cost")) - removed_ad + _dec(amounts.get("accumulated_impairment"))


async def disposals(session, company_id, date_from, date_to) -> dict:
    """The assets that left the books in the period, newest first."""
    _, start, end = await _range(session, company_id, date_from, date_to)
    currency = await setup.currency(session, company_id)
    assets, movements, _ = await _register(session, company_id)
    states = dict(assets)
    items = []
    for asset_id, moved in movements.items():
        asset = states.get(asset_id) or {}
        for mv in projections.live(moved):
            if mv.get("operation") != "disposal" or not start.isoformat() <= mv["effective_date"] <= end.isoformat():
                continue
            carrying = _carrying(mv)
            gain = _dec(mv.get("gain_or_loss"))
            sale = asset.get("sale") if mv.get("invoice_id") else None
            items.append({
                "date": mv["effective_date"], "asset_id": asset_id, "number": asset.get("number"),
                "name": asset.get("name"), "share": mv.get("share") or "1", "reason": mv.get("reason"),
                "proceeds": money(carrying + gain, currency),
                "credited": money(_credited(sale) if sale else _ZERO, currency),
                "carrying_amount": money(carrying, currency), "gain_or_loss": money(gain, currency),
                "invoice_id": mv.get("invoice_id"),
                "invoice_number": doc_number(await _state(session, company_id, mv["invoice_id"]), mv["invoice_id"])
                if mv.get("invoice_id") else None,
                "credit_notes": [{"id": cn_id, "number": doc_number(await _state(session, company_id, cn_id), cn_id)}
                                 for cn_id in sorted((sale or {}).get("credits") or {})],
                "journal_id": mv.get("journal_id")})
    items.sort(key=lambda r: (r["date"], r["number"] or ""), reverse=True)
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "items": items,
            "totals": {k: money(sum((_dec(r[k]) for r in items), _ZERO), currency)
                       for k in ("proceeds", "credited", "carrying_amount", "gain_or_loss")}}


# ── Tax ──────────────────────────────────────────────────────────────────────


def _tax_lines(book: dict) -> list[dict]:
    """What a book holds a written-down value for: its pools, or its assets worked out."""
    if book["basis"] == "pool":
        return [p for p in book["pools"] if p["status"] == "ok"]
    return [r for r in book["rows"] if r["status"] == "ok"]


async def book_tax(session, company_id, regime_key: str, year) -> dict:
    """The book carrying amount beside the tax written-down value at the tax year end, per
    pool or asset, with the deferred tax on the difference when the regime reports it."""
    from celerp_fixed_assets.calc.tax import DeferredTaxInputs

    settings = await setup.company_settings(session, company_id)
    book = await taxbooks.book(session, company_id, regime_key, year, settings)
    regime = taxbooks.regime_for(settings, regime_key)
    currency = books_currency(settings)
    _, movements, _ = await _register(session, company_id)
    carrying = {asset_id: _roll(moved, "0000", book["end"])["nbv_closing"] for asset_id, moved in movements.items()}
    rows = []
    for line in _tax_lines(book):
        assets = line.get("assets") or [line["asset_id"]]
        book_value = sum((carrying.get(a, _ZERO) for a in assets), _ZERO)
        tax_value = _dec(line["closing"])
        row = {"key": line.get("number") or line["key"], "name": line.get("name"),
               "category_label": line.get("category_label"), "book_carrying": money(book_value, currency),
               "tax_written_down_value": money(tax_value, currency),
               "difference": money(book_value - tax_value, currency)}
        if regime.deferred_tax is not None:
            row["deferred_tax"] = money(regime.deferred_tax(DeferredTaxInputs(
                year=book["year"], book_carrying=book_value, tax_written_down_value=tax_value)).amount, currency)
        rows.append(row)
    totals = {k: money(sum((_dec(r[k]) for r in rows), _ZERO), currency)
              for k in ("book_carrying", "tax_written_down_value", "difference", "deferred_tax")
              if book["deferred_tax"] or k != "deferred_tax"}
    return {**{k: book[k] for k in ("regime", "label", "basis", "year", "start", "end", "status", "read_only",
                                    "read_only_reason", "deferred_tax")},
            "currency": currency, "rows": rows, "totals": totals}


async def statutory(session, company_id, report_key: str, year) -> dict:
    """A country form, drawn by its module from its regime's book for the tax year."""
    from celerp_fixed_assets.calc import rules
    from fastapi import HTTPException
    from ui.i18n import t

    settings = await setup.company_settings(session, company_id)
    try:
        form = rules.statutory_report(report_key)
    except rules.RuleUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if not rules.is_available("statutory_report", report_key, settings):
        raise HTTPException(status_code=404, detail=rules.unavailable_reason("statutory_report", report_key))
    book = await taxbooks.book(session, company_id, form.regime, year, settings)
    rendered = form.render(_tax_lines(book), book["year"])
    return {"key": report_key, "label": t(form.label_key), "regime": form.regime, "regime_label": book["label"],
            "year": book["year"], "start": book["start"], "end": book["end"], "status": book["status"],
            "columns": [t(c) for c in rendered["columns"]],
            "rows": [[str(v) if v is not None else None for v in row] for row in rendered["rows"]]}
