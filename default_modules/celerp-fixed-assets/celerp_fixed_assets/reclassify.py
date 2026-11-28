# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Reclassifying an asset: it moves to another class from a date, and depreciates on.

One journal, dated the reclassification date: depreciation on the old class brought up
to the month before, then the cost moved to the new class's asset account and the
accumulated depreciation (with any impairment) to its accumulated depreciation account.
Two movements record it: the balances leaving the old class and arriving in the new one,
the second carrying the new class's policy as a change in estimate, so the life left is
counted from the date. Undo reverses both while nothing has happened since.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from celerp_fixed_assets import disposal, projections, runs, setup
from celerp.accounting_roles import refused
from celerp.services.periods import month_index, period_label
from celerp.services.money import money_text, to_decimal
from celerp_fixed_assets.operations import (
    _date, _effects, _journal_view, _lines, _move, _post, _run, asset_movements, catch_up, in_use)
from celerp_fixed_assets.store import _emit
from ui.i18n import t

_AD, _IMP = "accumulated_depreciation", "accumulated_impairment"


def balance_moves(number: str, old: dict, new: dict, cost: Decimal, depreciation: Decimal) -> list[tuple[list, str]]:
    """The journals moving an asset's cost and accumulated depreciation (which carries
    the accumulated impairment too) from one class's accounts to another's, with memos."""
    out = []
    if new["asset_account"] != old["asset_account"] and cost:
        out.append((_lines(new["asset_account"], old["asset_account"], cost), f"{number} moved to {new['name']}"))
    old_ad, new_ad = old["accumulated_depreciation_account"], new["accumulated_depreciation_account"]
    if old_ad != new_ad and depreciation:
        out.append((_lines(old_ad, new_ad, depreciation), f"{number} depreciation moved to {new['name']}"))
    return out


async def reclassify(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Move an asset to another class from a date."""
    from celerp.services.company_lock import lock_projections
    from celerp_fixed_assets import sales

    async def plan(fa):
        row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
        if row is None or row.entity_type != "fa_asset":
            raise HTTPException(status_code=404, detail=t("fixed_assets.err_no_such_asset_2"))
        asset = row.state
        in_use(asset_id, asset, "reclassify")
        day = _date(body.get("date"), "The reclassification date")
        await sales.refuse_waiting(session, company_id, asset_id, asset)
        old = await setup.get_class(session, company_id, asset.get("class_id"))
        new = await setup.get_class(session, company_id, body.get("class_id"))
        if new["id"] == old["id"]:
            raise setup.refuse("fixed_assets.err_number_already_name", number=asset['number'], name=old['name'])
        if new.get("archived"):
            raise setup.refuse("fixed_assets.err_class_archived_choose_class_use")
        if setup.measurement(new) != setup.measurement(old):
            raise setup.refuse("fixed_assets.err_name_measured_measurement_model_name2_measurement2", name=new['name'], measurement=setup.measurement(new), name2=old['name'], measurement2=setup.measurement(old))
        books = await runs._books(session, company_id)
        month = month_index(date.fromisoformat(day))
        if books.last is not None and books.last >= month:
            raise refused(409, "fixed_assets.err_depreciation_posted_through_period_label_reclassify", period_label=period_label(books.last), period_label2=period_label(books.last + 1))
        movements = await asset_movements(session, company_id, asset_id)
        disposal.check_date(books, asset, projections.live(movements), day, what="reclassification")
        owed, _ = disposal.owed(books, asset, movements, month - 1, month - 1)
        caught_up = sum(owed.values(), Decimal(0))
        cost, ad, imp = to_decimal(asset.get("cost")), to_decimal(asset.get(_AD)), to_decimal(asset.get(_IMP))
        expense = asset.get("depreciation_account") or old["depreciation_account"]
        lines = _lines(expense, old["accumulated_depreciation_account"], caught_up) if caught_up else []
        for moved, _ in balance_moves(asset["number"], old, new, cost, ad + caught_up + imp):
            lines += moved
        shown = {"number": asset["number"], "date": day, "from_class": old["name"], "to_class": new["name"],
                 "depreciation": money_text(caught_up, books.currency),
                 "journals": [_journal_view(lines, books.currency)] if lines else []}
        return shown, {"asset": asset, "old": old, "new": new, "day": day, "lines": lines,
                       "periods": {period_label(k): str(v) for k, v in owed.items()},
                       "amounts": {"cost": cost, _AD: ad + caught_up, _IMP: imp},
                       "policy": setup.policy_of(new, fa),
                       "basis": [asset.get("version"), asset.get("movement_count"),
                                 await runs._ledger_basis(session, company_id, books)]}

    async def apply(context, op_key):
        asset, old, new, day = context["asset"], context["old"], context["new"], context["day"]
        short = asset_id.removeprefix("fa:asset:")
        journals = []
        if context["lines"]:
            journals.append(await _post(
                session, company_id, user_id, key=f"asset:{short}:reclassify:{int(asset.get('movement_count') or 0) + 1}",
                operation="reclassify", day=day, memo=f"{asset['number']} {asset['name']} moved to {new['name']}",
                lines=context["lines"], source_id=asset_id, href=f"/fixed-assets/assets/{asset_id}",
                label=asset["number"], op_key=op_key))
        amounts = context["amounts"]
        caught = await catch_up(session, company_id, user_id, asset_id, day=day, periods=context["periods"],
                                op_key=op_key, journal_id=journals[0] if journals else None)
        out = await _move(session, company_id, user_id, asset_id, kind="posted", operation="reclassify",
                          effective=day, posting=day, op_key=op_key, side="out", class_id=old["id"],
                          amounts={k: -v for k, v in amounts.items()}, **caught,
                          journal_id=journals[0] if journals else None)
        into = await _move(session, company_id, user_id, asset_id, kind="posted", operation="reclassify",
                           effective=day, posting=day, op_key=op_key, side="in", class_id=new["id"],
                           amounts=amounts, periods={}, policy=context["policy"], previous=asset["policy"],
                           journal_id=journals[0] if journals else None)
        before = {"class_id": old["id"], "policy": asset["policy"], "class_history": asset.get("class_history")}
        # The classes the asset was in, each until the date it left (celerp_fixed_assets.recon).
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {"changes": {
            "class_id": new["id"], "policy": context["policy"],
            "class_history": [*(asset.get("class_history") or []), {"until": day, "class_id": old["id"]}]}})
        return ({"assets": [{"id": asset_id, "number": asset["number"]}], "journals": journals},
                _effects(movements=[out, into], journals=journals, changes=[{"id": asset_id, "before": before}]))

    return await _run(session, company_id, user_id, body, op_type="reclassify", keys=("manage_accounting",),
                      plan=plan, apply=apply)
