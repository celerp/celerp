# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tax books: each asset's tax figures under each tax regime the company has chosen,
kept beside the books and never posted.

A regime (calc.rules) works a tax year out from what moved in it: the cost added, the
proceeds of what left, and whether the asset was gone by the year end. An asset regime
writes each asset down on its own; a pool regime adds assets into pools and writes each
pool down. Every year opens with the previous year's closing figures, so a book is
worked out from the first tax year any of its assets was available for use. A closed
year keeps the figures it was closed with (celerp_fixed_assets.yearend), and the years
after it open from those.

An asset's tax category is the one set on its tax book, else its class's category for
the regime. An asset with neither has no figures until it is given one.

Nothing here reads or writes the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from celerp_fixed_assets import setup
from celerp_fixed_assets.calc import rules
from celerp_fixed_assets.calc.tax import PoolInputs, TaxAssetInputs
from celerp_fixed_assets.projections import REMEASUREMENTS, _dec, live
from celerp_fixed_assets.store import _emit, _entities, _state, movements_by_asset

_ZERO = Decimal(0)
FIGURES = ("opening", "additions", "disposals", "allowance", "closing")
# The assets a tax book holds: in use, or sold or disposed of (for the year they left).
_IN_BOOK = ("active", "sold", "disposed")
# Movements whose cost is not a tax addition: remeasurements, and the asset leaving.
_NOT_ADDED = (*REMEASUREMENTS, "disposal")
_YEARS = range(1900, 2201)


def taxbook_id(asset_id: str, regime_key: str) -> str:
    return f"fa:taxbook:{asset_id.removeprefix('fa:asset:')}:{regime_key}"


def taxyear_id(regime_key: str, year: int) -> str:
    return f"fa:taxyear:{regime_key}:{year}"


def _cents(value) -> str | None:
    return None if value is None else f"{_dec(value):.2f}"


def tax_year_end(settings: dict | None) -> str:
    return setup.settings_of(settings).get("tax_year_end") or "12-31"


def check_year(year) -> int:
    if not isinstance(year, int) or isinstance(year, bool) or year not in _YEARS:
        raise setup.unprocessable("The tax year is a year, such as 2026.")
    return year


def regime_for(settings: dict | None, key: str):
    """The regime ``key``: 404 when no module provides it, 422 when the company has not chosen it."""
    from ui.i18n import t

    try:
        regime = rules.regime(key)
    except rules.RuleUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if key not in (setup.settings_of(settings).get("tax_regimes") or []):
        raise setup.unprocessable(f"{t(regime.label_key)} is not one of this company's tax regimes. "
                                  "Choose it in the fixed asset settings first.")
    return regime


def unavailable(settings: dict | None, key: str) -> str | None:
    """Why the company's book under ``key`` can be read but not worked out, or None."""
    if rules.is_available("tax_regime", key, settings):
        return None
    return rules.unavailable_reason("tax_regime", key)


# ── What the books hold ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Asset:
    id: str
    number: str
    name: str
    available: date
    category: str | None
    elections: dict
    movements: tuple  # live movements as (effective date, cost)
    gone: date | None  # the day it was wholly sold or disposed of
    sold: date | None
    proceeds: Decimal


async def _assets(session, company_id, regime) -> list[_Asset]:
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    books = {s["asset_id"]: s for _, s in await _entities(session, company_id, "fa_taxbook")
             if s.get("regime") == regime.key}
    movements = await movements_by_asset(session, company_id)
    categories = {c.key for c in regime.categories}
    out = []
    for asset_id, asset in sorted(await _entities(session, company_id, "fa_asset"),
                                  key=lambda r: r[1].get("number") or ""):
        cls = classes.get(asset.get("class_id")) or {}
        if asset.get("status") not in _IN_BOOK or not asset.get("available_date") or cls.get("construction"):
            continue
        book = books.get(asset_id) or {}
        category = book.get("category") or (cls.get("tax_categories") or {}).get(regime.key)
        left = asset.get("disposal") or {}
        gone = left.get("date") if asset.get("status") in ("sold", "disposed") else None
        sold, proceeds = None, _ZERO
        if asset.get("status") == "sold":
            sold, proceeds = (asset.get("sale") or {}).get("date"), _dec((asset.get("sale") or {}).get("proceeds"))
        elif gone and left.get("reason") == "transferred":
            # Transferred to a related company: it leaves at the value the regime carries across.
            sold, proceeds = gone, _dec((left.get("tax_values") or {}).get(regime.key, left.get("price")))
        # Transferred in from a related company: it comes in at the value carried across.
        carried_in = asset.get("tax_values") or {}
        out.append(_Asset(
            id=asset_id, number=asset.get("number") or "", name=asset.get("name") or "",
            available=date.fromisoformat(asset["available_date"]),
            category=category if category in categories else None, elections=dict(book.get("elections") or {}),
            movements=tuple((date.fromisoformat(m["effective_date"]),
                             _dec(carried_in[regime.key]) if m.get("operation") == "transfer_in" and regime.key
                             in carried_in else _dec((m.get("amounts") or {}).get("cost")))
                            for m in live(movements.get(asset_id, [])) if m.get("operation") not in _NOT_ADDED),
            gone=date.fromisoformat(gone) if gone else None, sold=date.fromisoformat(sold) if sold else None,
            proceeds=proceeds))
    return out


async def _frozen(session, company_id, regime_key: str) -> dict[int, dict]:
    return {int(s["year"]): s for _, s in await _entities(session, company_id, "fa_taxyear")
            if s.get("regime") == regime_key}


# ── Working the years out ────────────────────────────────────────────────────


def _year_of(regime, end_setting: str, day: date) -> int:
    year = day.year
    start, end = regime.tax_year(end_setting, year)
    return year + 1 if day > end else year - 1 if day < start else year


def _failed(exc: Exception) -> str:
    return f"The tax regime could not work this out ({type(exc).__name__})."


def _row(a: _Asset, status: str, **figures) -> dict:
    return {"asset_id": a.id, "number": a.number, "name": a.name, "key": a.number, "category": a.category,
            "status": status, "elections": a.elections, **{k: _cents(figures.get(k)) for k in FIGURES},
            "reason": figures.get("reason")}


def _years(regime, end_setting: str, assets: list[_Asset], frozen: dict[int, dict], through: int,
           seen: dict | None = None) -> dict[int, dict]:
    """Every tax year of the book through ``through``: its asset rows, and its pools on a
    pool basis. A closed year is taken as it was closed. ``seen``, when given, collects
    each open year's inputs by (year, asset)."""
    if not assets:
        return {}
    first = min(_year_of(regime, end_setting, a.available) for a in assets)
    carried: dict[str, Decimal] = {}  # asset or pool -> closing written-down value
    counted: dict[str, date] = {}     # asset -> the last day its additions were counted through
    out: dict[int, dict] = {}
    for year in range(first, through + 1):
        start, end = regime.tax_year(end_setting, year)
        closed = frozen.get(year) if (frozen.get(year) or {}).get("status") == "closed" else None
        if closed is not None:
            for row in closed["rows"]:
                if row.get("closing") is not None:
                    carried[row["asset_id"]] = _dec(row["closing"])
                    counted[row["asset_id"]] = end
            for pool in closed.get("pools") or []:
                carried[f"pool:{pool['key']}"] = _dec(pool["closing"])
            out[year] = closed
            continue
        moved = []
        for a in assets:
            if a.available > end or (a.gone is not None and a.gone < start):
                continue
            since = counted.get(a.id)
            additions = sum((cost for day, cost in a.movements if day <= end and (since is None or day > since)),
                            _ZERO)
            disposals = a.proceeds if a.sold is not None and start <= a.sold <= end else _ZERO
            inputs = None if a.category is None else TaxAssetInputs(
                asset_id=a.id, category=a.category, elections=a.elections, year=year, start=start, end=end,
                available_date=a.available, opening=carried.get(a.id, _ZERO), additions=additions,
                disposals=disposals, disposed=a.gone is not None and a.gone <= end)
            if seen is not None and inputs is not None:
                seen[(year, a.id)] = inputs
            moved.append((a, inputs))
        out[year] = (_asset_year if regime.basis == "asset" else _pool_year)(regime, year, moved, carried, counted, end)
    return out


def _asset_year(regime, year, moved, carried, counted, end) -> dict:
    rows = []
    for a, inputs in moved:
        if inputs is None:
            rows.append(_row(a, "no_category"))
            continue
        try:
            result = regime.asset_year(inputs)
        except Exception as exc:  # a module's own code: named on the row, never fatal
            rows.append(_row(a, "error", reason=_failed(exc)))
            continue
        carried[a.id], counted[a.id] = result.closing, end
        rows.append(_row(a, "ok", **{k: getattr(result, k) for k in FIGURES}))
    return {"rows": rows, "pools": []}


def _pool_year(regime, year, moved, carried, counted, end) -> dict:
    rows, pools = [], {}
    for a, inputs in moved:
        if inputs is None:
            rows.append(_row(a, "no_category"))
            continue
        try:
            key = str(regime.pool_key(inputs))
        except Exception as exc:  # a module's own code: named on the row, never fatal
            rows.append(_row(a, "error", reason=_failed(exc)))
            continue
        counted[a.id] = end
        pool = pools.setdefault(key, {"category": a.category, "additions": _ZERO, "disposals": _ZERO, "assets": []})
        pool["additions"] += inputs.additions
        pool["disposals"] += inputs.disposals
        pool["assets"].append(a.id)
        rows.append({**_row(a, "ok", additions=inputs.additions, disposals=inputs.disposals), "pool": key})
    out = []
    for key in sorted(set(pools) | {k.removeprefix("pool:") for k in carried if k.startswith("pool:")}):
        pool = pools.get(key) or {"category": None, "additions": _ZERO, "disposals": _ZERO, "assets": []}
        opening = carried.get(f"pool:{key}", _ZERO)
        if not (opening or pool["assets"]):
            continue
        inputs = PoolInputs(key=key, category=pool["category"] or key, year=year, opening=opening,
                            additions=pool["additions"], disposals=pool["disposals"], assets=tuple(pool["assets"]))
        try:
            result = regime.pool_year(inputs)
        except Exception as exc:  # a module's own code: named on the pool, never fatal
            out.append({"key": key, "category": pool["category"], "status": "error", "reason": _failed(exc),
                        "assets": pool["assets"], **dict.fromkeys(FIGURES)})
            continue
        carried[f"pool:{key}"] = result.closing
        out.append({"key": key, "category": pool["category"], "status": "ok", "reason": None,
                    "assets": pool["assets"], **{k: _cents(getattr(result, k)) for k in FIGURES}})
    return {"rows": rows, "pools": out}


async def transfer_values(session, company_id, settings: dict, asset_id: str, day: date) -> dict[str, str]:
    """Per tax regime the company keeps, the written-down value ``asset_id`` carries to a
    related company on ``day``, as its regime's ``related_party_transfer`` works it out
    from the asset's tax year so far. A regime that is unavailable, has the asset in a
    closed year or no category, or fails gives none: the transfer price stands for it."""
    out = {}
    end_setting = tax_year_end(settings)
    for key in setup.settings_of(settings).get("tax_regimes") or []:
        if unavailable(settings, key):
            continue
        regime = rules.regime(key)
        year = _year_of(regime, end_setting, day)
        seen: dict = {}
        _years(regime, end_setting, await _assets(session, company_id, regime),
               await _frozen(session, company_id, key), year, seen)
        inputs = seen.get((year, asset_id))
        if inputs is None:
            continue
        try:
            result = regime.related_party_transfer(inputs)
        except Exception:  # a module's own code: the transfer price stands instead
            continue
        out[key] = _cents(result.written_down_value)
    return out


# ── The book of one year ─────────────────────────────────────────────────────


async def years(session, company_id, settings: dict, regime, through: int) -> dict[int, dict]:
    """Every year of the company's book under ``regime`` through ``through``."""
    return _years(regime, tax_year_end(settings), await _assets(session, company_id, regime),
                  await _frozen(session, company_id, regime.key), through)


async def book(session, company_id, regime_key: str, year: int, settings: dict | None = None) -> dict:
    """The book under ``regime_key`` for tax ``year``: open years worked out now, a closed
    year as it was closed. While the regime's module is off the book is read only: a closed
    year still shows, an open one has no figures until the module is back."""
    from ui.i18n import t

    settings = settings if settings is not None else await setup.company_settings(session, company_id)
    regime = regime_for(settings, regime_key)
    year = check_year(year)
    start, end = regime.tax_year(tax_year_end(settings), year)
    saved = await _state(session, company_id, taxyear_id(regime_key, year)) or {}
    closed = saved.get("status") == "closed"
    reason = unavailable(settings, regime_key)
    if closed:
        figures = {"rows": saved["rows"], "pools": saved.get("pools") or []}
    elif reason:
        figures = {"rows": [], "pools": []}
    else:
        figures = (await years(session, company_id, settings, regime, year)).get(year) or {"rows": [], "pools": []}
    labels = {c.key: t(c.label_key) for c in regime.categories}
    return {"regime": regime_key, "label": t(regime.label_key), "country": regime.country, "basis": regime.basis,
            "year": year, "start": start.isoformat(), "end": end.isoformat(), "status": "closed" if closed else "open",
            "read_only": reason is not None, "read_only_reason": reason, "deferred_tax": regime.deferred_tax is not None,
            "rows": [{**r, "category_label": labels.get(r.get("category"))} for r in figures["rows"]],
            "pools": [{**p, "category_label": labels.get(p.get("category"))} for p in figures["pools"]],
            "categories": [{"key": k, "label": v} for k, v in labels.items()],
            "elections_schema": dict(regime.elections_schema),
            "election_labels": {n: t(f"{regime.label_key}.{n}") for n in regime.elections_schema},
            "history": list(saved.get("history") or [])}


# ── An asset's tax book ──────────────────────────────────────────────────────


def _elections(regime, label: str, value) -> dict:
    if not isinstance(value, dict):
        raise setup.unprocessable("Elections are a set of named choices.")
    out = {}
    for name, chosen in value.items():
        kind = regime.elections_schema.get(name)
        if kind is None:
            raise setup.unprocessable(f"{name} is not an election of {label}.")
        if kind == "bool":
            if not isinstance(chosen, bool):
                raise setup.unprocessable(f"{name} is true or false.")
        elif kind == "decimal":
            try:
                chosen = str(Decimal(str(chosen)))
            except InvalidOperation:
                raise setup.unprocessable(f"{name} is a number.") from None
        elif not isinstance(chosen, str):
            raise setup.unprocessable(f"{name} is text.")
        out[name] = chosen
    return out


async def set_book(session, company_id, user_id, asset_id: str, regime_key: str, body: dict) -> dict:
    """Set an asset's tax category (None for its class's) and elections under one regime.
    Changes the open years only; a closed year keeps what it was closed with."""
    from celerp.services.permissions import locked_authority
    from ui.i18n import t

    _, settings = await locked_authority(session, company_id, user_id, ("manage_accounting",))
    regime = regime_for(settings, regime_key)
    reason = unavailable(settings, regime_key)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset_id.startswith("fa:asset:"):
        raise HTTPException(status_code=404, detail="No such asset.")
    unknown = set(body) - {"category", "elections"}
    if unknown:
        raise setup.unprocessable(f"Unknown tax book field: {sorted(unknown)[0]}.")
    label = t(regime.label_key)
    current = await _state(session, company_id, taxbook_id(asset_id, regime_key)) or {}
    category, elections = current.get("category"), dict(current.get("elections") or {})
    if "category" in body:
        category = body["category"] or None
        if category is not None and category not in {c.key for c in regime.categories}:
            raise setup.unprocessable(f"{category} is not a category of {label}.")
    if "elections" in body:
        elections = _elections(regime, label, body["elections"])
    data = {"asset_id": asset_id, "regime": regime_key, "category": category, "elections": elections}
    await _emit(session, company_id, user_id, taxbook_id(asset_id, regime_key), "fa_taxbook", "fa.taxbook.recorded",
                data)
    return data
