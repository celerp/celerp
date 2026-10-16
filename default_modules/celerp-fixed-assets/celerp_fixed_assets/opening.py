# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Opening balances: the assets brought in at the cutover, and moving the cutover.

An opening asset posts nothing. Its values are what the ledger should already hold at the
cutover; the reconciliation (recon) shows any difference between the two, and Finish
opening balances waits for that difference to be zero or to be kept open on the record.

The cutover moves only while nothing depends on it: no run has posted, no registered line
or recorded movement is dated on or before the new cutover, and no opening asset is dated
after it. Opening assets then move to the new cutover by a reversal of their opening
movement and a new one, so movements stay as recorded.
"""
from __future__ import annotations

import uuid
from datetime import date

from celerp_fixed_assets import projections, runs, setup, sources
from celerp_fixed_assets.operations import _dec, _move, conflict
from celerp_fixed_assets.store import _entities, movements_by_asset

OPENING_STATES = ("finished", "kept_open")


async def move_cutover(session, company_id, user_id, fa: dict, new_cut: str) -> None:
    """Refuse a move something depends on, else re-date the opening assets. The caller
    holds the company lock and writes the new first managed period."""
    old_cut = setup.cutover(fa)
    books = await runs._books(session, company_id)
    if books.last is not None:
        raise conflict(f"Depreciation for {runs.period_label(books.last)} has posted, so the first managed period "
                       "can no longer move. Reverse the runs back to the first one to move it.")
    for line in await sources.lines(session, company_id):
        if line["claim"] and line["date"] <= new_cut:
            raise conflict(f"{line['description']} ({line['date']}) is registered, and the new cutover {new_cut} "
                           "would put it in the opening balances. Undo it first, or choose a later period.")
    assets = dict(await _entities(session, company_id, "fa_asset"))
    movements = await movements_by_asset(session, company_id)
    redate = []
    for asset_id, moved in sorted(movements.items()):
        number = assets.get(asset_id, {}).get("number") or asset_id
        for mv in projections.live(moved):
            if mv["operation"] == "opening":
                if (assets[asset_id].get("available_date") or "") > new_cut:
                    raise conflict(f"{number} is an opening asset available from "
                                   f"{assets[asset_id]['available_date']}, after the new cutover {new_cut}.")
                redate.append(mv)
            elif mv["operation"] not in runs.OPENING_OPERATIONS and mv["posting_date"] <= new_cut:
                raise conflict(f"{number} has a change dated {mv['posting_date']}, on or before the new cutover "
                               f"{new_cut}. Choose a later period.")
    op_key = f"cutover.{uuid.uuid4().hex[:12]}"
    for mv in redate:
        amounts = mv.get("amounts") or {}
        await _move(session, company_id, user_id, mv["asset_id"], kind="reversal", operation="cutover_moved",
                    effective=mv["effective_date"], posting=old_cut, op_key=op_key,
                    amounts={k: -_dec(v) for k, v in amounts.items()}, quantity=str(-_dec(mv.get("quantity"))),
                    reverses=mv["id"])
        await _move(session, company_id, user_id, mv["asset_id"], kind="opening", operation="opening",
                    effective=mv["effective_date"], posting=new_cut, op_key=op_key, amounts=amounts,
                    quantity=mv.get("quantity") or 0, provenance=mv.get("provenance") or "form")


async def view(session, company_id) -> dict:
    """Each class account at the cutover: the ledger, the opening register and their difference."""
    from celerp_fixed_assets import recon

    fa = await setup.require_managed(session, company_id)
    at_cut = await recon.report(session, company_id, setup.cutover(fa))
    return {"cutover": at_cut["cutover"], "state": at_cut["opening"],
            "accounts": [{"account": a["account"], "name": a["name"], "ledger": a["ledger"],
                          "registered": a["registered"],
                          "difference": str(_dec(a["ledger"]) - _dec(a["registered"]))}
                         for a in at_cut["accounts"]]}


async def finish(session, company_id, user_id, body: dict) -> dict:
    """Finish opening balances once the ledger and the register agree at the cutover, or
    keep the differences open on the record. Reopening takes the state back."""
    from celerp.services.company_lock import locked_company
    from celerp.services.permissions import locked_authority

    await locked_authority(session, company_id, user_id, ("manage_accounting",))
    company = await locked_company(session, company_id)
    reopen, keep_open = bool(body.get("reopen")), bool(body.get("keep_open"))
    shown = await view(session, company_id)
    if not reopen and not keep_open:
        apart = [f"{a['account']} {a['name']} ({a['difference']})" for a in shown["accounts"] if _dec(a["difference"])]
        if apart:
            raise conflict("The ledger and the register differ at the cutover on " + ", ".join(apart)
                           + ". Record the opening journal or the missing opening assets, or keep the differences open.")
    fa = setup.settings_of(company.settings)
    state = None if reopen else OPENING_STATES[1] if keep_open else OPENING_STATES[0]
    fa["opening"] = {"state": state, "by": str(user_id), "on": date.today().isoformat()} if state else None
    company.settings = {**(company.settings or {}), setup.SETTINGS_KEY: fa}
    await session.flush()
    return {**shown, "state": state}
