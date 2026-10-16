# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Disposing of an asset that is not sold: scrapped, lost, donated or otherwise gone.

One journal per asset, dated the disposal date: depreciation brought up to that date
(negative when runs already charged months after it), then the cost, accumulated
depreciation and impairment of what leaves the books taken off, any revaluation surplus
moved to retained earnings, and the balance to the loss (or gain) on disposal. A partial
disposal takes a percentage of a single asset, or a quantity of a record holding
several, and the rest depreciates on. Undo reverses the latest disposal while nothing has
happened to the asset since and its date is open.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from celerp_fixed_assets import runs, setup
from celerp_fixed_assets.calc.periods import month_index, parse_period, period_label
from celerp_fixed_assets.operations import (
    MODULE, _cents, _date, _dec, _effects, _emit, _journal_view, _lines, _move, _post, _reversed_lines, _run,
    _state, asset_movements, conflict)

REASONS = ("scrapped", "lost", "donated", "other")
_AD, _IMP, _SURPLUS = "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus"
# Movements that are depreciation charged, which a disposal may come after or before.
_CHARGES = ("run", "depreciation", "link_depreciation")


def _number(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise setup.unprocessable(f"{field} must be a number.") from None
    if not number.is_finite():
        raise setup.unprocessable(f"{field} must be a number.")
    return number


def _share(asset: dict, body: dict) -> tuple[Decimal, Decimal]:
    """The share of the asset disposed of, and the quantity that leaves with it."""
    quantity = _dec(asset.get("quantity"))
    if quantity > 1:
        if body.get("percent") not in (None, ""):
            raise setup.unprocessable("This record holds a quantity, so it is disposed of by quantity.")
        if body.get("quantity") in (None, ""):
            return Decimal(1), quantity
        out = _number(body["quantity"], "The quantity disposed of")
        if out <= 0 or out > quantity:
            raise setup.unprocessable(f"The quantity disposed of is more than 0 and at most {quantity}.")
        return out / quantity, out
    if body.get("quantity") not in (None, "") and _number(body["quantity"], "The quantity disposed of") != quantity:
        raise setup.unprocessable(f"This asset has a quantity of {quantity}. Dispose of a percentage of it instead.")
    if body.get("percent") in (None, ""):
        return Decimal(1), quantity
    percent = _number(body["percent"], "The percent disposed of")
    if percent <= 0 or percent > 100:
        raise setup.unprocessable("The percent disposed of is more than 0 and at most 100.")
    share = percent / 100
    return share, quantity if share == 1 else Decimal(0)


def _role(books: runs.Books, role: str, label: str) -> str:
    from celerp.services.account_roles import role_map

    account = role_map(books.settings).get(role)
    if not account:
        raise setup.unprocessable(f"Choose the {label} account in Posting accounts first.")
    return account


def _split(amount: Decimal, share: Decimal, currency: str) -> Decimal:
    """The part of ``amount`` that leaves with ``share``."""
    from celerp.services.money import allocate_pro_rata

    if share == 1 or not amount:
        return amount
    return allocate_pro_rata(amount, [share, 1 - share], currency)[0]


def _check_date(books: runs.Books, asset: dict, live: list[dict], day: str) -> None:
    from celerp.services.business_time import business_date_of

    if day < (asset.get("available_date") or day):
        raise setup.unprocessable(f"The disposal date is before {asset['number']} was available for use "
                                  f"({asset['available_date']}).")
    if day > business_date_of(None, books.settings.get("timezone")):
        raise setup.unprocessable(f"The disposal date {day} is after today.")
    if books.lock is not None and date.fromisoformat(day) <= books.lock:
        raise setup.unprocessable(f"{day} is in a locked period. Dispose on or after "
                                  f"{(books.lock + timedelta(days=1)).isoformat()}.")
    changed = max((m["effective_date"] for m in live if m.get("operation") not in _CHARGES), default=day)
    if changed > day:
        raise setup.unprocessable(f"{asset['number']} changed on {changed}. Dispose on or after that date.")


def _plan_asset(books: runs.Books, asset_id: str, asset: dict, cls: dict, movements: list[dict], day: str,
                share: Decimal, quantity: Decimal, accounts: dict) -> dict:
    """What disposing of ``share`` of one asset on ``day`` posts and moves."""
    currency = books.settings.get("currency") or "USD"
    if asset.get("status") == "disposed":
        raise conflict(f"{asset['number']}: Disposed on {asset['disposal']['date']}.")
    if asset.get("status") != "active":
        raise conflict(f"{asset['number']} is not in use, so it cannot be disposed of.")
    live = runs.live(movements)
    _check_date(books, asset, live, day)
    cost = _dec(asset.get("cost"))
    gone_cost = _split(cost, share, currency)
    month = month_index(date.fromisoformat(day))
    through = max(month, books.last if books.last is not None else month)
    leaving = {"id": "fa:mv:disposal", "operation": "disposal", "effective_date": day, "share": str(share),
               "amounts": {"cost": str(-gone_cost)}}
    restated: dict[int, Decimal] = {}
    parted = Decimal(0)
    if asset["policy"]["method"] != "none":
        planned = runs.full_schedule(asset, [*movements, leaving], books.settings, through)
        missing = [p.period for p in planned.periods if p.exception is not None and parse_period(p.period) <= month]
        if missing:
            raise setup.unprocessable(f"Enter the reading for {missing[0]} on {asset['number']} first.")
        restated = {parse_period(p.period): p.amount for p in planned.periods}
        parted = planned.parted[-1] if share < 1 else Decimal(0)
    charged = runs.charged_by_month(movements)
    diff = {k: restated.get(k, Decimal(0)) - charged.get(k, Decimal(0))
            for k in sorted({*restated, *charged}) if k <= through}
    diff = {k: v for k, v in diff.items() if v}
    depreciation = sum(diff.values(), Decimal(0))
    if share == 1:
        gone_ad = _dec(asset.get(_AD)) + depreciation
    else:
        from celerp.services.money import round_money

        opening = sum((_dec((m.get("amounts") or {}).get(_AD)) for m in live
                       if m.get("operation") in runs.OPENING_OPERATIONS), Decimal(0))
        gone_ad = round_money(parted + opening * share, currency)
    gone_imp = _split(_dec(asset.get(_IMP)), share, currency)
    gone_surplus = _split(_dec(asset.get(_SURPLUS)), share, currency)
    carrying = gone_cost - gone_ad - gone_imp
    gain = -carrying
    expense = asset.get("depreciation_account") or cls["depreciation_account"]
    accumulated = cls["accumulated_depreciation_account"]
    from celerp.services.module_journals import JournalLine

    lines = _lines(expense, accumulated, depreciation) if depreciation else []
    if gone_ad + gone_imp:
        lines.append(JournalLine(account=accumulated, debit=gone_ad + gone_imp))
    lines.append(JournalLine(account=cls["asset_account"], credit=gone_cost))
    if gone_surplus:
        lines += _lines(accounts["surplus"](), accounts["retained"](), gone_surplus)
    if gain > 0:
        lines.append(JournalLine(account=accounts["gain"](), credit=gain))
    elif gain < 0:
        lines.append(JournalLine(account=accounts["loss"](), debit=-gain))
    shown = {"number": asset["number"], "name": asset["name"], "share": str(share), "cost": _cents(gone_cost),
             "depreciation": _cents(depreciation), "accumulated": _cents(gone_ad + gone_imp),
             "carrying_amount": _cents(carrying), "gain_or_loss": _cents(gain)}
    return {"asset_id": asset_id, "asset": asset, "shown": shown, "lines": lines, "full": share == 1,
            "quantity": quantity, "gain": gain,
            "amounts": {"cost": -gone_cost, _AD: depreciation - gone_ad, _IMP: -gone_imp, _SURPLUS: -gone_surplus},
            "periods": {period_label(k): str(v) for k, v in diff.items()}, "share": share}


async def dispose(session, company_id, user_id, body: dict, asset_ids: list[str] | None = None) -> dict:
    """Dispose of one asset (in whole or part), or of several in whole."""
    from celerp.services.company_lock import lock_projections

    bulk = asset_ids is None
    if bulk:
        asset_ids = body.get("asset_ids")
        if not isinstance(asset_ids, list) or not asset_ids or not all(isinstance(a, str) for a in asset_ids):
            raise setup.unprocessable("Choose the assets to dispose of.")
        if body.get("percent") not in (None, "") or body.get("quantity") not in (None, ""):
            raise setup.unprocessable("Several assets are disposed of in whole. Dispose of part of one asset "
                                      "from its page.")
        asset_ids = list(dict.fromkeys(asset_ids))

    async def plan(fa):
        day = _date(body.get("date"), "The disposal date")
        reason = body.get("reason")
        if reason not in REASONS:
            raise setup.unprocessable("Choose why the asset is disposed of: scrapped, lost, donated or other.")
        note = str(body.get("note") or "").strip() or None
        if reason == "other" and not note:
            raise setup.unprocessable("Say why the asset is disposed of.")
        rows = await lock_projections(session, company_id, asset_ids)
        books = await runs._books(session, company_id)
        accounts = {"gain": lambda: _role(books, "disposal_gain", "gain on disposal"),
                    "loss": lambda: _role(books, "disposal_loss", "loss on disposal"),
                    "surplus": lambda: _role(books, "revaluation_surplus", "revaluation surplus"),
                    "retained": lambda: _role(books, "retained_earnings", "retained earnings")}
        items = []
        for asset_id in asset_ids:
            row = rows.get(asset_id)
            if row is None or row.entity_type != "fa_asset":
                raise HTTPException(status_code=404, detail="No such asset.")
            cls = await setup.get_class(session, company_id, row.state.get("class_id"))
            share, quantity = (Decimal(1), _dec(row.state.get("quantity"))) if bulk else _share(row.state, body)
            items.append(_plan_asset(books, asset_id, row.state, cls,
                                     await asset_movements(session, company_id, asset_id), day, share, quantity,
                                     accounts))
        totals = {k: _cents(sum((_dec(i["shown"][k]) for i in items), Decimal(0)))
                  for k in ("depreciation", "carrying_amount", "gain_or_loss")}
        shown = {"date": day, "reason": reason, "note": note, **totals, "assets": [i["shown"] for i in items],
                 "journals": [_journal_view(i["lines"]) for i in items]}
        basis = [[(i["asset"].get("version"), i["asset"].get("movement_count")) for i in items],
                 await runs._ledger_basis(session, company_id, books)]
        return shown, {"items": items, "day": day, "reason": reason, "note": note, "basis": basis}

    async def apply(context, op_key):
        day, reason, note = context["day"], context["reason"], context["note"]
        assets, journals, movements = [], [], []
        for item in context["items"]:
            asset_id, asset = item["asset_id"], item["asset"]
            short = asset_id.removeprefix("fa:asset:")
            je_id = await _post(session, company_id, user_id,
                                key=f"asset:{short}:disposal:{int(asset.get('movement_count') or 0) + 1}",
                                operation="disposal", day=day, memo=f"{asset['number']} {asset['name']} disposed of",
                                lines=item["lines"], source_id=asset_id, href=f"/fixed-assets/assets/{asset_id}",
                                label=asset["number"], op_key=op_key)
            movement_id = await _move(session, company_id, user_id, asset_id, kind="posted", operation="disposal",
                                      effective=day, posting=day, amounts=item["amounts"], op_key=op_key,
                                      quantity=str(-item["quantity"]), periods=item["periods"],
                                      share=str(item["share"]), reason=reason, note=note, journal_id=je_id,
                                      gain_or_loss=_cents(item["gain"]))
            if item["full"]:
                await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status", {
                    "status": "disposed", "exception": None,
                    "disposal": {"date": day, "reason": reason, "note": note, "op_key": op_key,
                                 "movement_id": movement_id, "journal_id": je_id,
                                 "gain_or_loss": _cents(item["gain"])}})
            assets.append({"id": asset_id, "number": asset["number"]})
            journals.append(je_id)
            movements.append(movement_id)
        return {"assets": assets, "journals": journals}, _effects(movements=movements, journals=journals)

    return await _run(session, company_id, user_id, body, op_type="disposal", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def undo(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Undo an asset's latest disposal while nothing has happened to it since and its date is open."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import reverse_journal

    async def plan(fa):
        row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
        if row is None or row.entity_type != "fa_asset":
            raise HTTPException(status_code=404, detail="No such asset.")
        asset = row.state
        movements = await asset_movements(session, company_id, asset_id)
        disposals = [m for m in runs.live(movements) if m.get("operation") == "disposal"]
        if not disposals:
            raise setup.unprocessable(f"{asset['number']} has no disposal to undo.")
        latest = disposals[-1]
        if int(latest["id"].rsplit(":", 1)[1]) != int(asset.get("movement_count") or 0):
            raise conflict(f"{asset['number']} has changed since it was disposed of, so the disposal can no longer "
                           "be undone.")
        books = await runs._books(session, company_id)
        day = latest["effective_date"]
        if books.lock is not None and date.fromisoformat(day) <= books.lock:
            raise conflict(f"{day} is locked, so this disposal can no longer be undone.")
        je = await _state(session, company_id, latest["journal_id"])
        shown = {"number": asset["number"], "date": day,
                 "journals": [_reversed_lines(je)] if je and je.get("status") == "posted" else []}
        return shown, {"asset": asset, "movement": latest,
                       "basis": [asset.get("version"), asset.get("movement_count"),
                                 await runs._ledger_basis(session, company_id, books)]}

    async def apply(context, op_key):
        mv, asset = context["movement"], context["asset"]
        await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                              key=mv["journal_id"].removeprefix(f"je:mod:{MODULE}:"), operation="undo",
                              reason="Disposal undone")
        await _move(session, company_id, user_id, asset_id, kind="reversal", operation="undo",
                    effective=mv["effective_date"], posting=mv["posting_date"],
                    amounts={k: -_dec(v) for k, v in (mv.get("amounts") or {}).items()}, op_key=op_key,
                    quantity=str(-_dec(mv.get("quantity"))), reverses=mv["id"])
        if asset.get("status") == "disposed":
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status",
                        {"status": "active", "disposal": None})
        return {"undone": mv["id"], "assets": [{"id": asset_id, "number": asset["number"]}], "journals": []}, _effects()

    return await _run(session, company_id, user_id, body, op_type="disposal_undo", keys=("manage_accounting",),
                      plan=plan, apply=apply)
