# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Adding to an asset: an improvement, a replacement part, and construction placed in service.

Each is paid for by a bill or journal line (claimed, and moved to the asset account when it
was posted elsewhere) or entered by hand against a chosen credit account.

- An improvement adds a layer to the asset from its date: over the life the asset has left
  (keep the end date), or over a new life.
- A replacement part is registered as a component of the asset, with its own life, and
  the part it replaces leaves the books as a disposal: a component picked from the list,
  or an estimated share of the asset's cost when the old part was never separate.
- Construction collects cost on an asset in a construction class and is not depreciated.
  Placing it in service moves the balance, in one step or several, to new assets that
  depreciate from that date; it is never allocated twice.

A repair or maintenance line is registered against the asset with the register's
maintenance mode (operations.register). The latest operation on an asset is undone with
Undo while its date is open; anything older is corrected instead.
"""
from __future__ import annotations

from decimal import Decimal

from celerp_fixed_assets import disposal, operations, projections, runs, setup, transfer
from celerp_fixed_assets.projections import money, said
from celerp_fixed_assets.operations import (
    _claim,
    _create_asset,
    _date,
    _dec,
    _effects,
    _journal_view,
    _move,
    _next_numbers,
    _open_line,
    _post,
    _reclass,
    _run,
    asset_movements,
    conflict,
    in_use,
)
from celerp_fixed_assets.store import _emit, _entities, _state
from fastapi import HTTPException

KINDS = ("improvement", "replacement")
# What Undo latest offers on the asset page; registration and disposal have their own Undo.
UNDOABLE = ("improvement", "replacement", "place_in_service", "construction_out", "revaluation", "impairment",
            "impairment_reversal", "reclassify")
_ZERO = Decimal(0)


def _life(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        months = int(str(value))
    except ValueError:
        raise setup.unprocessable("The life is a whole number of months.") from None
    if months < 1:
        raise setup.unprocessable("The life is at least 1 month.")
    return months


async def _locked_asset(session, company_id, asset_id: str) -> dict:
    from celerp.services.company_lock import lock_projections

    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    return row.state


async def _funding(session, company_id, body: dict, asset_account: str, currency: str) -> dict:
    """What pays for the addition: a source line, or an amount against a credit account."""
    from celerp.services.module_journals import JournalLine

    if body.get("source"):
        line = await _open_line(session, company_id, body["source"])
        if line["kind"] != "acquisition":
            raise setup.unprocessable("Only a line that adds to an asset account pays for an addition.")
        amount = _dec(line["amount"])
        lines = _reclass(line, asset_account, amount) if line["account"] != asset_account else []
        contributions = [{"journal_id": line["journal_id"], "line_index": line["line_index"], "amount": str(amount)}]
        return {"line": line, "amount": amount, "lines": lines, "contributions": contributions,
                "day": body.get("date") or line["date"], "generation": line["generation"]}
    amount = operations._money_in(body.get("amount"), "The amount", currency, positive=True)
    account = str(body.get("credit_account") or "")
    if not account:
        raise setup.unprocessable("Choose the account the addition is paid from, or pick its bill line.")
    return {"line": None, "amount": amount, "contributions": [], "day": body.get("date"), "generation": 0,
            "lines": [JournalLine(account=asset_account, debit=amount), JournalLine(account=account, credit=amount)]}


async def add(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Add an improvement or a replacement part to an asset."""

    async def plan(fa):
        asset = await _locked_asset(session, company_id, asset_id)
        in_use(asset_id, asset, "nothing can be added to it")
        kind = body.get("kind")
        if kind not in KINDS:
            raise setup.unprocessable("Choose what is added: an improvement or a replacement part.")
        cls = await setup.get_class(session, company_id, asset.get("class_id"))
        books = await runs._books(session, company_id)
        currency = books.currency
        funding = await _funding(session, company_id, body, cls["asset_account"], currency)
        day = _date(funding["day"], "The date")
        movements = await asset_movements(session, company_id, asset_id)
        disposal.check_date(books, asset, projections.live(movements), day, what="addition", verb="Add")
        life = _life(body.get("life_months"))
        shown = {"kind": kind, "date": day, "number": asset["number"], "amount": money(funding["amount"], currency),
                 "life_months": life}
        context = {"kind": kind, "asset": asset, "cls": cls, "day": day, "funding": funding, "life": life,
                   "generation": funding["generation"],
                   "basis": [asset.get("version"), asset.get("movement_count"),
                             await runs._ledger_basis(session, company_id, books)]}
        journals = [_journal_view(funding["lines"], currency)] if funding["lines"] else []
        if kind == "replacement":
            name = str(body.get("name") or "").strip()
            if not name:
                raise setup.unprocessable("Name the replacement part.")
            if life is None:
                raise setup.unprocessable("Enter the replacement part's life in months.")
            item = await _replaced(session, company_id, books, asset_id, asset, cls, movements, day,
                                   body.get("replaced"))
            journals.append(_journal_view(item["lines"], currency))
            context.update(name=name, item=item, number=(await _next_numbers(session, company_id, 1))[0])
            shown.update(name=name, number_of_part=context["number"], replaced=item["shown"])
        return {**shown, "journals": journals}, context

    async def apply(context, op_key):
        funding, asset, day = context["funding"], context["asset"], context["day"]
        if context["kind"] == "improvement":
            target, number, name = asset_id, asset["number"], asset["name"]
            created = []
        else:
            policy = {**asset["policy"], "life_months": context["life"]}
            target = await _create_asset(
                session, company_id, user_id, number=context["number"], name=context["name"],
                class_id=asset["class_id"], status="active", available_date=day, op_key=op_key,
                sources_=_sources(funding), policy=policy, parent_id=asset_id)
            number, name, created = context["number"], context["name"], [target]
        short = target.removeprefix("fa:asset:")
        count = int((await _state(session, company_id, target)).get("movement_count") or 0) + 1
        journals, movements, statuses = [], [], []
        je_id = None
        if funding["lines"]:
            je_id = await _post(session, company_id, user_id, key=f"asset:{short}:mv:{count}",
                                operation=context["kind"], day=day, memo=f"{number} {name} {context['kind']}",
                                lines=funding["lines"], source_id=target, href=f"/fixed-assets/assets/{target}",
                                label=number, op_key=op_key)
            journals.append(je_id)
        extra = {"life_months": context["life"]} if context["kind"] == "improvement" else {}
        movements.append(await _move(session, company_id, user_id, target, kind="posted",
                                     operation=context["kind"], effective=day,
                                     posting=funding["line"]["date"] if funding["line"] else day,
                                     amounts={"cost": funding["amount"]}, op_key=op_key,
                                     contributions=funding["contributions"], journal_id=je_id, **extra))
        if funding["line"]:
            await _claim(session, company_id, user_id, funding["line"], allocation=context["kind"], op_key=op_key,
                         asset_ids=[target], numbers=[number])
        if context["kind"] == "replacement":
            item = context["item"]
            je, mv = await disposal.record(session, company_id, user_id, item, day, "replaced", None, op_key)
            journals.append(je)
            movements.append(mv)
            if item["full"]:
                statuses.append({"id": item["asset_id"], "before": "active"})
        return ({"assets": [{"id": target, "number": number}], "journals": journals},
                _effects(assets=created, movements=movements, journals=journals, statuses=statuses,
                         claims=[funding["line"]["source"]] if funding["line"] else []))

    return await _run(session, company_id, user_id, body, op_type="addition", keys=("manage_accounting",),
                      plan=plan, apply=apply)


def _sources(funding: dict) -> list[dict]:
    line = funding["line"]
    if line is None:
        return []
    return [{"source": line["source"], "ref": line.get("document_ref"), "amount": str(funding["amount"])}]


async def _replaced(session, company_id, books, asset_id: str, asset: dict, cls: dict, movements: list[dict],
                    day: str, replaced) -> dict:
    """The disposal of the part a replacement replaces: a component picked from the list,
    whole, or an estimated share of the asset when the old part was never separate."""
    if not isinstance(replaced, dict) or bool(replaced.get("asset_id")) == bool(replaced.get("estimate")):
        raise setup.unprocessable("Pick the component being replaced, or estimate what the old part cost.")
    roles = disposal.accounts(books)
    if replaced.get("asset_id"):
        part_id = str(replaced["asset_id"])
        part = await _locked_asset(session, company_id, part_id)
        if part.get("parent_id") != asset_id:
            raise setup.unprocessable(f"{part['number']} is not a component of {asset['number']}.")
        part_cls = await setup.get_class(session, company_id, part.get("class_id"))
        return disposal._plan_asset(books, part_id, part, part_cls,
                                    await asset_movements(session, company_id, part_id), day, Decimal(1),
                                    _dec(part.get("quantity")), roles)
    from celerp.services.money import round_money

    estimate = round_money(setup.number(replaced["estimate"], "The estimated cost of the old part"), books.currency)
    cost = _dec(asset.get("cost"))
    if estimate <= 0 or estimate >= cost:
        raise setup.unprocessable(f"The estimated cost of the old part is more than 0 and less than "
                                  f"{asset['number']}'s cost of {said(cost, books.currency)}.")
    return disposal._plan_asset(books, asset_id, asset, cls, movements, day, estimate / cost, _ZERO, roles)


async def components(session, company_id, asset_id: str) -> list[dict]:
    """An asset's components, by number, undone ones left out."""
    return sorted(({"id": eid, **s} for eid, s in await _entities(session, company_id, "fa_asset")
                   if s.get("parent_id") == asset_id and s.get("status") != "undone"),
                  key=lambda c: c.get("number") or "")


# ── Construction ─────────────────────────────────────────────────────────────


async def place_in_service(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Move construction cost to the assets it built, on their available-for-use date.
    What is not allocated stays under construction."""
    from celerp.services.module_journals import JournalLine

    async def plan(fa):
        site = await _locked_asset(session, company_id, asset_id)
        cls = await setup.get_class(session, company_id, site.get("class_id"))
        if not cls.get("construction"):
            raise setup.unprocessable(f"{site['number']} is not under construction.")
        if site.get("status") == "placed_in_service":
            raise conflict(f"{site['number']} is already placed in service in full.")
        if site.get("status") != "active":
            raise conflict(f"{site['number']} is not in use, so it cannot be placed in service.")
        day = _date(body.get("date"), "The available-for-use date")
        books = await runs._books(session, company_id)
        currency = books.currency
        disposal.check_date(books, site, projections.live(await asset_movements(session, company_id, asset_id)), day,
                            what="available-for-use", verb="Place in service")
        rows = body.get("allocations")
        if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
            raise setup.unprocessable("Allocate the cost to at least one asset.")
        balance = _dec(site.get("cost"))
        allocations = []
        for row in rows:
            target = await setup.get_class(session, company_id, row.get("class_id"))
            if target.get("construction"):
                raise setup.unprocessable("Place construction cost in a class that is in service.")
            name = str(row.get("name") or "").strip()
            if not name:
                raise setup.unprocessable("Name each asset placed in service.")
            amount = operations._money_in(row.get("amount"), f"The amount for {name}", currency, positive=True)
            life = _life(row.get("life_months"))
            policy = setup.policy_of(target, fa)
            allocations.append({"cls": target, "name": name, "amount": amount,
                                "policy": {**policy, "life_months": life} if life else policy})
        total = sum((a["amount"] for a in allocations), _ZERO)
        if total > balance:
            raise setup.unprocessable(f"The allocations total {said(total, currency)}, more than the {said(balance, currency)} "
                                      f"{site['number']} holds.")
        debits: dict[str, Decimal] = {}
        for a in allocations:
            debits[a["cls"]["asset_account"]] = debits.get(a["cls"]["asset_account"], _ZERO) + a["amount"]
        lines = [JournalLine(account=k, debit=v) for k, v in debits.items()]
        lines.append(JournalLine(account=cls["asset_account"], credit=total))
        numbers = await _next_numbers(session, company_id, len(allocations))
        shown = {"date": day, "number": site["number"], "balance": money(balance, currency),
                 "remainder": money(balance - total, currency), "journals": [_journal_view(lines, currency)],
                 "assets": [{"number": n, "name": a["name"], "class": a["cls"]["name"], "cost": money(a["amount"], currency)}
                            for n, a in zip(numbers, allocations)]}
        return shown, {"site": site, "day": day, "allocations": allocations, "numbers": numbers, "lines": lines,
                       "total": total, "full": total == balance,
                       "basis": [site.get("version"), site.get("movement_count"),
                                 await runs._ledger_basis(session, company_id, books)]}

    async def apply(context, op_key):
        site, day = context["site"], context["day"]
        je_id = await _post(session, company_id, user_id,
                            key=f"asset:{asset_id.removeprefix('fa:asset:')}:mv:{int(site.get('movement_count') or 0) + 1}",
                            operation="place_in_service", day=day, memo=f"{site['number']} {site['name']} placed in service",
                            lines=context["lines"], source_id=asset_id, href=f"/fixed-assets/assets/{asset_id}",
                            label=site["number"], op_key=op_key)
        movements = [await _move(session, company_id, user_id, asset_id, kind="posted", operation="construction_out",
                                 effective=day, posting=day, amounts={"cost": -context["total"]}, op_key=op_key,
                                 journal_id=je_id)]
        assets = []
        for number, a in zip(context["numbers"], context["allocations"]):
            new = await _create_asset(session, company_id, user_id, number=number, name=a["name"],
                                      class_id=a["cls"]["id"], status="active", available_date=day, op_key=op_key,
                                      policy=a["policy"], placed_from=asset_id)
            movements.append(await _move(session, company_id, user_id, new, kind="posted",
                                         operation="place_in_service", effective=day, posting=day,
                                         amounts={"cost": a["amount"]}, op_key=op_key, quantity=1,
                                         journal_id=je_id))
            assets.append({"id": new, "number": number})
        statuses = []
        if context["full"]:
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status",
                        {"status": "placed_in_service"})
            statuses.append({"id": asset_id, "before": "active"})
        return ({"assets": assets, "journals": [je_id]},
                _effects(assets=[a["id"] for a in assets], movements=movements, journals=[je_id],
                         statuses=statuses))

    return await _run(session, company_id, user_id, body, op_type="place_in_service", keys=("manage_accounting",),
                      plan=plan, apply=apply)


# ── Undo the latest ──────────────────────────────────────────────────────────


def _latest(movements: list[dict]) -> dict | None:
    live = projections.live(movements)
    return live[-1] if live else None


async def undoable(session, company_id, asset_id: str) -> str | None:
    """The change Undo latest undoes from the asset page, while it is the asset's latest
    movement: an addition, a placement in service, a remeasurement, a reclassification or a
    transfer to another company."""
    latest = _latest(await asset_movements(session, company_id, asset_id))
    if latest is None:
        return None
    if latest["operation"] == "disposal" and latest.get("reason") in ("replaced", "transferred"):
        return {"replaced": "replacement", "transferred": "transfer"}[latest["reason"]]
    return latest["operation"] if latest["operation"] in UNDOABLE else None


async def undo_latest(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Undo the operation behind the asset's latest movement other than depreciation, while
    nothing has been charged or changed on it since and its date is open."""
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset_id.startswith("fa:asset:"):
        raise HTTPException(status_code=404, detail="No such asset.")
    latest = _latest(await asset_movements(session, company_id, asset_id))
    if latest is None:
        raise setup.unprocessable(f"{asset['number']} has nothing to undo.")
    if latest.get("operation") in disposal._CHARGES:
        raise conflict(f"Depreciation was charged on {asset['number']} after its latest change. Use Correct "
                       "instead.")
    op = await _state(session, company_id, f"fa:op:{latest['op_key']}") or {}
    if op.get("type") in ("disposal", "sale"):
        return await disposal.undo(session, company_id, user_id, asset_id, body)
    if op.get("type") == "transfer":
        return await transfer.undo(session, company_id, user_id, asset_id, body)
    return await operations._undo(session, company_id, user_id, [latest["op_key"]], body)
