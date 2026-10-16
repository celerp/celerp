# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Transferring an asset to another company the user keeps books for.

One operation in both companies and one transaction. In this company the asset leaves
the books as a disposal at the transfer price, against the account the user picks for
the related company (an asset account); in the other it comes in as a new asset at that
price, at the entered rate when the currencies differ, against the account picked there
(a liability account). Neither posts a clearing line. The new asset depreciates over the
life this one had left, carries its history (read only), and, per tax regime, the
written-down value the regime carries across a related-party transfer. A taxable sale
between the companies goes through Sell and a bill instead. Undo reverses both while
nothing has happened to the new asset since.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from celerp_fixed_assets import disposal, projections, runs, setup, taxbooks
from celerp_fixed_assets.calc.periods import month_index
from celerp_fixed_assets.operations import (
    MODULE, _amount, _cents, _create_asset, _date, _dec, _effects, _journal_view, _lines, _move, _next_numbers,
    _post, _reversed_lines, _run, asset_movements, conflict, reverse_move)
from celerp_fixed_assets.store import _emit, _state

# The rights a transfer needs in each company.
KEYS = ("manage_accounting", "manage_fixed_assets")
_HISTORY = ("effective_date", "operation", "amounts", "quantity")


def _uses(settings: dict | None) -> bool:
    """The company uses fixed assets, with the chart it posts to, and has its first managed period."""
    from celerp.modules.registry import uses_module
    from celerp.services.journal_accounts import CHART_MODULE

    fa = setup.settings_of(settings)
    return (uses_module(settings, MODULE) and uses_module(settings, CHART_MODULE) and bool(fa.get("started"))
            and bool(fa.get("first_managed_period")))


async def targets(session, company_id, user_id) -> list[dict]:
    """The other companies the user may transfer an asset to: a member there with both
    rights, in a company that uses fixed assets."""
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company
    from celerp.services.permissions import role_has_permission

    rows = (await session.execute(select(Company, UserCompany.role).join(
        UserCompany, UserCompany.company_id == Company.id).where(
        UserCompany.user_id == user_id, UserCompany.is_active.is_(True), Company.id != company_id))).all()
    return sorted(({"company_id": str(c.id), "name": c.name} for c, role in rows
                   if _uses(c.settings) and all(role_has_permission(c.settings, role, k) for k in KEYS)),
                  key=lambda t: t["name"])


def _company_id(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise setup.unprocessable("Choose the company to transfer the asset to.") from None


async def _lock_in_order(session, ours, theirs) -> None:
    """Take the other company's lock first when its id sorts first: every transfer takes
    the two locks in id order, so two between the same companies never wait on each other."""
    from celerp.services.company_lock import locked_company

    if theirs < uuid.UUID(str(ours)):
        await locked_company(session, theirs)


async def _name(session, company_id) -> str:
    from celerp.models.company import Company

    return (await session.execute(select(Company.name).where(Company.id == company_id))).scalar_one()


async def _related_account(session, company_id, name: str, code: str, role) -> None:
    """``code`` can take a new posting in the company called ``name``, of the type ``role`` takes."""
    from celerp.accounting_roles import ROLE_TYPES
    from celerp.services.journal_accounts import require_destinations

    if not code:
        raise setup.unprocessable(f"Choose the account for the related company in {name}.")
    try:
        accounts = await require_destinations(session, company_id, {code})
    except HTTPException as exc:
        detail = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
        raise HTTPException(status_code=exc.status_code, detail=f"{name}: {detail}") from None
    (wanted,) = ROLE_TYPES[role]
    found = ((accounts or {}).get(code) or {}).get("account_type")
    if accounts is not None and found != wanted:
        article = "an" if wanted[0] in "aeiou" else "a"
        raise setup.unprocessable(f"Account {code} in {name} is of type {found}. The related company's account "
                                  f"there must be {article} {wanted} account.")


def _rate(body: dict, ours: str, theirs: str, our_name: str, their_name: str) -> Decimal:
    if ours == theirs:
        if body.get("rate") not in (None, ""):
            raise setup.unprocessable(f"Both companies keep their books in {ours}, so no exchange rate is needed.")
        return Decimal(1)
    if body.get("rate") in (None, ""):
        raise setup.unprocessable(f"{our_name} keeps its books in {ours} and {their_name} in {theirs}. Enter the "
                                  f"exchange rate from {ours} to {theirs}.")
    rate = disposal._number(body["rate"], "The exchange rate")
    if rate <= 0:
        raise setup.unprocessable("The exchange rate is more than zero.")
    return rate


def _life_left(asset: dict, policy: dict, day: str) -> dict:
    """The target class's policy over the months of life the asset has left, counting the
    transfer month as used here (it is charged here, up to the transfer date)."""
    life = asset["policy"].get("life_months")
    if not life or not policy.get("life_months"):
        return policy
    used = month_index(date.fromisoformat(day)) - month_index(date.fromisoformat(asset["available_date"])) + 1
    return {**policy, "life_months": max(int(life) - used, 1)}


async def transfer(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Transfer an asset, in whole, to another company the user keeps books for."""
    from celerp.accounting_roles import AccountRole
    from celerp.services.company_lock import lock_projections
    from celerp.services.money import round_money
    from celerp.services.permissions import locked_authority
    from celerp_fixed_assets import sales

    theirs = _company_id(body.get("target_company_id"))
    if theirs == uuid.UUID(str(company_id)):
        raise setup.unprocessable("Choose another company to transfer the asset to.")
    await _lock_in_order(session, company_id, theirs)

    async def plan(fa):
        day = _date(body.get("date"), "The transfer date")
        price = _amount(body.get("price"), "The transfer price")
        _, their_settings = await locked_authority(session, theirs, user_id, KEYS)
        our_name, their_name = await _name(session, company_id), await _name(session, theirs)
        if not _uses(their_settings):
            raise conflict(f"{their_name} does not use fixed assets. Turn the module on there and confirm its "
                           "first managed period first.")
        their_fa = setup.settings_of(their_settings)
        if day < their_fa["first_managed_period"]:
            raise setup.unprocessable(f"{their_name} manages fixed assets from {their_fa['first_managed_period']}. "
                                      "Date the transfer on or after it.")
        their_books = await runs._books(session, theirs)
        if their_books.lock is not None and date.fromisoformat(day) <= their_books.lock:
            raise conflict(f"{day} is in a locked period in {their_name}. Transfer on or after "
                           f"{(their_books.lock + timedelta(days=1)).isoformat()}.")
        row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
        if row is None or row.entity_type != "fa_asset":
            raise HTTPException(status_code=404, detail="No such asset.")
        asset = row.state
        await sales.refuse_waiting(session, company_id, asset_id, asset)
        receivable = str(body.get("receivable_account") or "")
        payable = str(body.get("payable_account") or "")
        await _related_account(session, company_id, our_name, receivable, AccountRole.RECEIVABLE)
        await _related_account(session, theirs, their_name, payable, AccountRole.PAYABLE)
        books = await runs._books(session, company_id)
        ours_in = books.settings.get("currency") or "USD"
        theirs_in = their_settings.get("currency") or "USD"
        rate = _rate(body, ours_in, theirs_in, our_name, their_name)
        cls = await setup.get_class(session, company_id, asset.get("class_id"))
        their_cls = await setup.get_class(session, theirs, body.get("class_id"))
        if their_cls.get("archived"):
            raise setup.unprocessable(f"That class is archived in {their_name}. Choose a class in use.")
        movements = await asset_movements(session, company_id, asset_id)
        item = disposal._plan_asset(books, asset_id, asset, cls, movements, day, Decimal(1),
                                    _dec(asset.get("quantity")), disposal.accounts(books), proceeds=price,
                                    clearing=receivable)
        cost = round_money(price * rate, theirs_in)
        their_lines = _lines(their_cls["asset_account"], payable, cost) if cost else []
        (number,) = await _next_numbers(session, theirs, 1)
        values = await taxbooks.transfer_values(session, company_id, books.settings, asset_id,
                                                date.fromisoformat(day))
        shown = {**item["shown"], "date": day, "company": their_name, "price": _cents(price),
                 "rate": str(rate) if rate != 1 else None, "their_number": number, "their_cost": _cents(cost),
                 "their_class": their_cls["name"],
                 "journals": [_journal_view(item["lines"]), _journal_view(their_lines)]}
        return shown, {
            "item": item, "day": day, "price": price, "cost": cost, "their_cls": their_cls,
            "their_lines": their_lines, "number": number, "our_name": our_name, "their_name": their_name,
            "policy": _life_left(asset, setup.policy_of(their_cls, their_fa), day),
            "tax_values": values, "their_tax_values": {k: _cents(round_money(_dec(v) * rate, theirs_in))
                                                       for k, v in values.items()},
            "history": [{k: m.get(k) for k in _HISTORY} for m in projections.live(movements)],
            "basis": [asset.get("version"), asset.get("movement_count"),
                      await runs._ledger_basis(session, company_id, books),
                      await runs._ledger_basis(session, theirs, their_books)]}

    async def apply(context, op_key):
        item, day = context["item"], context["day"]
        asset = item["asset"]
        source = {"company_id": str(company_id), "company_name": context["our_name"], "asset_id": asset_id,
                  "number": asset["number"], "name": asset["name"]}
        their_id = await _create_asset(
            session, theirs, user_id, number=context["number"], name=asset["name"],
            class_id=context["their_cls"]["id"], status="active", available_date=day, op_key=op_key,
            policy=context["policy"], transferred_from={**source, "history": context["history"]},
            tax_values=context["their_tax_values"],
            **{k: asset.get(k) for k in ("serial", "description", "make", "model", "notes")})
        target = {"company_id": str(theirs), "company_name": context["their_name"], "asset_id": their_id,
                  "number": context["number"]}
        je_id, movement_id = await disposal.record(
            session, company_id, user_id, item, day, "transferred", None, op_key, key="ict",
            operation="transfer_out", memo=f"transferred to {context['their_name']}", target=target,
            price=_cents(context["price"]), tax_values=context["tax_values"])
        their_journals, contributions = [], []
        if context["their_lines"]:
            their_journals.append(await _post(
                session, theirs, user_id, key=f"asset:{their_id.removeprefix('fa:asset:')}:ict:1",
                operation="transfer_in", day=day,
                memo=f"{context['number']} {asset['name']} transferred from {context['our_name']}",
                lines=context["their_lines"], source_id=their_id, href=f"/fixed-assets/assets/{their_id}",
                label=context["number"], op_key=op_key))
            contributions = [{"journal_id": their_journals[0], "line_index": 0, "amount": str(context["cost"])}]
        their_movement = await _move(
            session, theirs, user_id, their_id, kind="posted", operation="transfer_in", effective=day, posting=day,
            amounts={"cost": context["cost"]}, op_key=op_key, contributions=contributions,
            quantity=str(_dec(asset.get("quantity")) or 1), provenance="transfer", source=source,
            journal_id=their_journals[0] if their_journals else None)
        their_result = {"assets": [{"id": their_id, "number": context["number"]}], "journals": their_journals,
                        "source": source}
        await _emit(session, theirs, user_id, f"fa:op:{op_key}", "fa_op", "fa.op.recorded", {
            "type": "transfer_in", "intent_hash": context["intent_hash"], "result": their_result,
            "effects": _effects(assets=[their_id], movements=[their_movement], journals=their_journals)},
            key=f"fa:op:{op_key}")
        return ({"assets": [{"id": asset_id, "number": asset["number"]}], "journals": [je_id],
                 "target": {"company_id": str(theirs), "asset_id": their_id}},
                _effects(movements=[movement_id], journals=[je_id]))

    return await _run(session, company_id, user_id, body, op_type="transfer", keys=KEYS, plan=plan, apply=apply)


async def undo(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Undo a transfer in both companies while it is the asset's latest change here,
    nothing has happened to the new asset there, and the date is open in both."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import reverse_journal
    from celerp.services.permissions import locked_authority

    latest = projections.live(await asset_movements(session, company_id, asset_id))[-1]
    target = latest["target"]
    theirs = uuid.UUID(target["company_id"])
    await _lock_in_order(session, company_id, theirs)

    async def plan(fa):
        await locked_authority(session, theirs, user_id, KEYS)
        asset = (await lock_projections(session, company_id, [asset_id]))[asset_id].state
        if int(latest["id"].rsplit(":", 1)[1]) != int(asset.get("movement_count") or 0):
            raise conflict(f"{asset['number']} has changed since it was transferred, so the transfer can no "
                           "longer be undone.")
        day, name = latest["effective_date"], target["company_name"]
        books = await runs._books(session, company_id)
        if books.lock is not None and date.fromisoformat(day) <= books.lock:
            raise conflict(f"{day} is locked, so this transfer can no longer be undone.")
        their = (await lock_projections(session, theirs, [target["asset_id"]]))[target["asset_id"]].state
        if their.get("status") != "active" or int(their.get("movement_count") or 0) > 1:
            raise conflict(f"{their['number']} has changed in {name} since it was transferred, so the transfer "
                           "can no longer be undone. Use Correct in each company instead.")
        their_books = await runs._books(session, theirs)
        if their_books.lock is not None and date.fromisoformat(day) <= their_books.lock:
            raise conflict(f"{day} is locked in {name}, so this transfer can no longer be undone.")
        (their_movement,) = await asset_movements(session, theirs, target["asset_id"])
        journals = [(company_id, latest["journal_id"]), (theirs, their_movement.get("journal_id"))]
        states = [await _state(session, c, j) for c, j in journals if j]
        shown = {"number": asset["number"], "date": day, "company": name,
                 "journals": [_reversed_lines(je) for je in states if je and je.get("status") == "posted"]}
        return shown, {"asset": asset, "their_movement": their_movement, "journals": journals,
                       "basis": [asset.get("version"), asset.get("movement_count"), their.get("version"),
                                 await runs._ledger_basis(session, company_id, books),
                                 await runs._ledger_basis(session, theirs, their_books)]}

    async def apply(context, op_key):
        for company, je_id in context["journals"]:
            if je_id:
                await reverse_journal(session, company_id=company, user_id=user_id, module=MODULE,
                                      key=je_id.removeprefix(f"je:mod:{MODULE}:"), operation="undo",
                                      reason="Transfer undone")
        await reverse_move(session, company_id, user_id, latest["id"], latest, op_key)
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status",
                    {"status": "active", "disposal": None})
        their_movement = context["their_movement"]
        await reverse_move(session, theirs, user_id, their_movement["id"], their_movement, op_key)
        await _emit(session, theirs, user_id, target["asset_id"], "fa_asset", "fa.asset.status", {"status": "undone"})
        for company in (company_id, theirs):
            await _emit(session, company, user_id, f"fa:op:{latest['op_key']}", "fa_op", "fa.op.undone",
                        {"undo_key": op_key})
        return ({"undone": latest["id"], "assets": [{"id": asset_id, "number": context["asset"]["number"]}],
                 "journals": []}, _effects())

    return await _run(session, company_id, user_id, body, op_type="transfer_undo", keys=("manage_accounting",),
                      plan=plan, apply=apply)
