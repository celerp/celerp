# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asset operations: registering source lines, adding assets by hand, and undoing both.

Every write follows one contract. The caller takes the company lock and its authority,
then the operation is planned from what the books hold now. A preview returns the plan
with a fingerprint of it; a confirm plans again under the lock and refuses when the
fingerprint differs, so nothing is posted that the user did not see. A confirmed
operation is recorded under its operation key (``fa:op:<key>``) with a hash of the
request: the same key and request returns the recorded result, the same key with a
different request is refused.

An asset's financial figures change only by movements (``fa:mv:<asset>:<n>``), so the
asset always equals the fold of its movements. Undo reverses an operation's journals,
movements and claims as one more operation.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp_fixed_assets import setup, sources
from celerp_fixed_assets.projections import BUCKETS

MODULE = "celerp-fixed-assets"
REGISTER_MODES = ("separate", "quantity", "expense", "maintenance")
DESCRIPTIVE_FIELDS = ("name", "description", "serial", "make", "model", "notes")
_OP_KEY = re.compile(r"[A-Za-z0-9._-]{1,64}")
_NOT_INTENT = ("preview", "fingerprint", "op_key")


def conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


# ── Shared pieces ────────────────────────────────────────────────────────────


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def _cents(value) -> str:
    return f"{_dec(value):.2f}"


def _amount(value, field: str, *, positive: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation:
        raise setup.unprocessable(f"{field} must be a number.") from None
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise setup.unprocessable(f"{field} must be more than zero." if positive else f"{field} must be zero or more.")
    return amount


def _date(value, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise setup.unprocessable(f"{field} is a date.") from None


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def _emit(session, company_id, user_id, entity_id: str, entity_type: str, event_type: str, data: dict,
                key: str | None = None):
    from celerp.events.engine import emit_event

    return await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                            event_type=event_type, data=data, actor_id=user_id, location_id=None,
                            source="fixed_assets", idempotency_key=key or str(uuid.uuid4()))


async def _state(session, company_id, entity_id: str) -> dict | None:
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id},
                            populate_existing=True)
    return row.state if row is not None else None


async def _entities(session, company_id, entity_type: str) -> list[tuple[str, dict]]:
    return list((await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == entity_type))).all())


def movements_query(company_id, asset_id: str):
    """An asset's movements, read through the index on the asset a projection belongs to."""
    from celerp.models.projections import ASSET_ID

    return select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_movement", ASSET_ID == asset_id)


async def asset_movements(session, company_id, asset_id: str) -> list[dict]:
    """An asset's movements in the order they were recorded."""
    rows = (await session.execute(movements_query(company_id, asset_id))).all()
    return sorted(({"id": eid, **s} for eid, s in rows), key=lambda m: int(m["id"].rsplit(":", 1)[1]))


async def _next_numbers(session, company_id, count: int) -> list[str]:
    """The next ``count`` asset numbers. Numbers are never reused, undone assets included."""
    used = [int(s["number"][3:]) for _, s in await _entities(session, company_id, "fa_asset")
            if str(s.get("number") or "").startswith("FA-") and s["number"][3:].isdigit()]
    start = max(used, default=0) + 1
    return [f"FA-{n:04d}" for n in range(start, start + count)]


def _lines(debit_account: str, credit_account: str, amount: Decimal):
    """Two lines moving ``amount`` from ``credit_account`` to ``debit_account``, flipped when negative."""
    from celerp.services.module_journals import JournalLine

    if amount < 0:
        debit_account, credit_account, amount = credit_account, debit_account, -amount
    return [JournalLine(account=debit_account, debit=amount), JournalLine(account=credit_account, credit=amount)]


def _journal_view(lines) -> dict:
    return {"lines": [{"account": ln.account, "debit": _cents(ln.debit), "credit": _cents(ln.credit)}
                      for ln in lines]}


async def _post(session, company_id, user_id, *, key: str, operation: str, day: str, memo: str, lines,
                source_id: str, href: str, label: str, op_key: str, intent: dict | None = None,
                tags: dict | None = None) -> str:
    """Post one module journal. Its intent is the operation key unless ``intent`` names
    what the journal is for (a run month's journal is the same whichever operation posts it).
    ``tags`` are the markers a report reads back from the journal's source."""
    from celerp.services.module_journals import JournalSource, post_journal

    posted = await post_journal(session, company_id=company_id, user_id=user_id, module=MODULE, key=key,
                                operation=operation, date=day, memo=memo, lines=lines,
                                source=JournalSource(source_id, href, label, tags), intent=intent or {"op_key": op_key})
    return posted.je_id


async def _move(session, company_id, user_id, asset_id: str, *, kind: str, operation: str, effective: str,
                posting: str, amounts: dict, op_key: str, contributions=(), quantity=0, **extra) -> str:
    """Record one movement on an asset and mirror it onto the asset."""
    asset = await _state(session, company_id, asset_id)
    movement_id = f"fa:mv:{asset_id.removeprefix('fa:asset:')}:{int(asset.get('movement_count') or 0) + 1}"
    amounts = {k: str(v) for k, v in amounts.items() if k in BUCKETS}
    await _emit(session, company_id, user_id, movement_id, "fa_movement", "fa.movement.recorded", {
        "asset_id": asset_id, "kind": kind, "operation": operation, "effective_date": effective,
        "posting_date": posting, "amounts": amounts, "contributions": list(contributions),
        "quantity": quantity, "op_key": op_key, **extra}, key=f"{movement_id}:rec")
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.moved", {
        "movement_id": movement_id, "amounts": amounts, "quantity": quantity}, key=f"{movement_id}:mv")
    return movement_id


async def _create_asset(session, company_id, user_id, *, number: str, name: str, class_id, status: str,
                        available_date, op_key: str, sources_=(), **fields) -> str:
    asset_id = f"fa:asset:{uuid.uuid4()}"
    data = {"number": number, "name": name, "class_id": class_id, "status": status,
            "available_date": available_date, "serial": None, "description": None, "make": None,
            "model": None, "notes": None, "sources": list(sources_), "op_key": op_key, **fields}
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.created", data)
    return asset_id


async def _claim(session, company_id, user_id, line: dict, *, allocation: str, op_key: str, **extra) -> None:
    claim = sources.claim_id(line["source"])
    event = await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.claimed", {
        "source": line["source"], "allocation": allocation, "generation": line["generation"], "op_key": op_key,
        "description": line["description"], "amount": line["amount"], "journal_id": line["journal_id"],
        "line_index": line["line_index"], **extra}, key=f"{claim}:claim:{line['generation']}")
    if getattr(event, "was_deduped", False):
        raise conflict(f"{line['description']} was already registered.")


# ── The operation contract ───────────────────────────────────────────────────


def _op_key(body: dict) -> str:
    key = body.get("op_key")
    if not isinstance(key, str) or not _OP_KEY.fullmatch(key):
        raise setup.unprocessable("Every change carries an operation key from its form.")
    return key


async def _run(session, company_id, user_id, body: dict, *, op_type: str, keys: tuple[str, ...], plan, apply):
    """Preview or confirm one operation. ``plan(fa)`` returns ``(shown, context)``: what
    the preview shows, which the fingerprint covers, and what ``apply`` needs to do it.
    ``context["generation"]`` (a source line's claim generation) names a stale form, and
    ``context["basis"]``, when given, is what the plan was read from that the preview does
    not show (the fingerprint covers it too)."""
    from celerp.services.permissions import locked_authority

    op_key = _op_key(body)
    await locked_authority(session, company_id, user_id, keys)
    fa = await setup.require_started(session, company_id)
    intent_hash = _hash({"type": op_type, **{k: v for k, v in body.items() if k not in _NOT_INTENT}})
    if not body.get("preview"):
        done = await _state(session, company_id, f"fa:op:{op_key}")
        if done is not None:
            if done.get("intent_hash") != intent_hash:
                raise conflict("This change was already made with different details. Reload and start again.")
            return done["result"]
    shown, context = await plan(fa)
    generation = context.get("generation", 0)
    fingerprint = f"{generation}.{_hash({'shown': shown, 'basis': context.get('basis')})}"
    if body.get("preview"):
        return {**shown, "fingerprint": fingerprint}
    previewed = str(body.get("fingerprint") or "")
    if previewed != fingerprint:
        if previewed.split(".", 1)[0] != str(generation):
            raise conflict("Line was registered and released since you opened this form. Review again.")
        raise conflict("Assets, accounts or postings changed since preview. Review again.")
    result, effects = await apply(context, op_key)
    await _emit(session, company_id, user_id, f"fa:op:{op_key}", "fa_op", "fa.op.recorded", {
        "type": op_type, "intent_hash": intent_hash, "result": result, "effects": effects},
        key=f"fa:op:{op_key}")
    return result


def _effects(**kw) -> dict:
    return {"assets": [], "movements": [], "claims": [], "journals": [], "maint": [], **kw}


# ── Registering a source line ────────────────────────────────────────────────


async def _open_line(session, company_id, source) -> dict:
    line = await sources.line(session, company_id, str(source or ""))
    if line["before_cutover"]:
        raise setup.unprocessable("This line is dated on or before the cutover, so it belongs to the opening "
                                  "register. Add it as an opening asset instead.")
    claim = line["claim"]
    if claim:
        numbers = claim.get("numbers") or []
        raise conflict(f"{line['description']} is already registered as {', '.join(numbers)}." if numbers
                       else f"{line['description']} was already dealt with.")
    return line


def _reclass(line: dict, account: str, amount: Decimal):
    from celerp.services.module_journals import JournalLine

    return [JournalLine(account=account, debit=amount),
            JournalLine(account=line["account"], credit=amount, moves_recognized=True,
                        recognized_in=line["journal_id"])]


async def register(session, company_id, user_id, body: dict) -> dict:
    """Register a source line: as separate assets, one record with a quantity, an expense,
    or maintenance on an existing asset."""

    async def plan(fa):
        line = await _open_line(session, company_id, body.get("source"))
        if line["kind"] == "reduction":
            raise setup.unprocessable("This line reduces an asset account. Use Correct on the asset it belongs "
                                      "to and link the line there.")
        if line["kind"] != "acquisition":
            raise setup.unprocessable("This line records depreciation. Link it to the assets it belongs to.")
        mode = body.get("mode") or "separate"
        if mode not in REGISTER_MODES:
            raise setup.unprocessable("Choose how to register the line.")
        amount = _dec(line["amount"])
        context = {"line": line, "mode": mode, "generation": line["generation"]}
        if mode in ("expense", "maintenance"):
            account = str(body.get("expense_account") or "")
            if not account:
                raise setup.unprocessable("Choose the expense account.")
            if mode == "maintenance":
                asset_id = str(body.get("asset_id") or "")
                asset = await _state(session, company_id, asset_id)
                if asset is None or not asset_id.startswith("fa:asset:") or asset.get("status") != "active":
                    raise setup.unprocessable("Choose the asset this maintenance was on.")
                context["asset"] = (asset_id, asset)
            journal = _reclass(line, account, amount)
            context["journal"] = journal
            return {"journals": [_journal_view(journal)], "assets": []}, context
        cls = await setup.get_class(session, company_id, body.get("class_id"))
        name = str(body.get("name") or line["description"]).strip()
        if not name:
            raise setup.unprocessable("An asset needs a name.")
        available = _date(body.get("available_date") or line["date"], "The available-for-use date")
        quantity = _dec(line["quantity"])
        if mode == "separate":
            if quantity != quantity.to_integral_value():
                raise setup.unprocessable("Separate assets need a whole quantity. Register one record instead.")
            from celerp.services.money import allocate_pro_rata

            count = int(quantity)
            currency = (await setup.company_settings(session, company_id)).get("currency") or "USD"
            costs = allocate_pro_rata(amount, [Decimal(1)] * count, currency)
            each = [(cost, 1) for cost in costs]
        else:
            each = [(amount, line["quantity"])]
        numbers = await _next_numbers(session, company_id, len(each))
        journals = ([_reclass(line, cls["asset_account"], cost) for cost, _ in each]
                    if cls["asset_account"] != line["account"] else [])
        context.update(cls=cls, name=name, available=available, each=each, numbers=numbers, journals=journals,
                       policy=setup.policy_of(cls, fa))
        return {"journals": [_journal_view(j) for j in journals],
                "assets": [{"number": n, "name": name, "cost": _cents(c)} for n, (c, _) in zip(numbers, each)]}, context

    async def apply(context, op_key):
        line, mode = context["line"], context["mode"]
        if mode in ("expense", "maintenance"):
            maint = []
            if mode == "maintenance":
                asset_id, _ = context["asset"]
                maint_id = f"fa:maint:{asset_id.removeprefix('fa:asset:')}:{uuid.uuid4()}"
                maint.append(maint_id)
            await _claim(session, company_id, user_id, line, allocation=mode, op_key=op_key,
                         asset_ids=[context["asset"][0]] if maint else [])
            je_id = await _post(session, company_id, user_id, key=f"src:{line['source']}:{mode}:{line['generation']}",
                                operation=mode, day=line["date"], memo=line["description"],
                                lines=context["journal"], source_id=sources.claim_id(line["source"]),
                                href=line["href"], label=line["description"], op_key=op_key)
            if maint:
                await _emit(session, company_id, user_id, maint[0], "fa_maint", "fa.maint.recorded", {
                    "asset_id": context["asset"][0], "source": line["source"], "amount": line["amount"],
                    "date": line["date"], "journal_id": je_id, "status": "active", "op_key": op_key})
            return ({"assets": [], "journals": [je_id]},
                    _effects(claims=[line["source"]], journals=[je_id], maint=maint))
        assets, journals, movements = [], [], []
        source_ref = {"source": line["source"], "ref": line.get("document_ref"), "amount": line["amount"]}
        for number, (cost, quantity), reclass in zip(context["numbers"], context["each"],
                                                    context["journals"] or [None] * len(context["each"])):
            asset_id = await _create_asset(
                session, company_id, user_id, number=number, name=context["name"], class_id=context["cls"]["id"],
                status="active", available_date=context["available"], op_key=op_key,
                sources_=[{**source_ref, "amount": str(cost)}], policy=context["policy"])
            contributions = [{"journal_id": line["journal_id"], "line_index": line["line_index"], "amount": str(cost)}]
            if reclass:
                je_id = await _post(session, company_id, user_id, key=f"asset:{asset_id.removeprefix('fa:asset:')}:reg:1",
                                    operation="registration", day=line["date"], memo=f"{number} {context['name']}",
                                    lines=reclass, source_id=asset_id,
                                    href=f"/fixed-assets/assets/{asset_id}", label=number, op_key=op_key)
                journals.append(je_id)
            movements.append(await _move(session, company_id, user_id, asset_id, kind="posted",
                                         operation="registration", effective=context["available"],
                                         posting=line["date"], amounts={"cost": cost}, op_key=op_key,
                                         contributions=contributions, quantity=quantity, provenance="source"))
            assets.append({"id": asset_id, "number": number})
        await _claim(session, company_id, user_id, line, allocation=mode, op_key=op_key,
                     asset_ids=[a["id"] for a in assets], numbers=[a["number"] for a in assets])
        return ({"assets": assets, "journals": journals},
                _effects(assets=[a["id"] for a in assets], movements=movements, claims=[line["source"]],
                         journals=journals))

    return await _run(session, company_id, user_id, body, op_type="registration", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def not_acquisition(session, company_id, user_id, body: dict) -> dict:
    """Mark a manual journal line as not an asset acquisition, with the reason."""

    async def plan(fa):
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise setup.unprocessable("A reason is required to mark a line as not an acquisition.")
        line = await _open_line(session, company_id, body.get("source"))
        if not line["journal_id"].startswith("je:manual:"):
            raise setup.unprocessable("Only a manual journal line can be marked as not an acquisition.")
        return ({"journals": [], "assets": [], "reason": reason},
                {"line": line, "reason": reason, "generation": line["generation"]})

    async def apply(context, op_key):
        line = context["line"]
        await _claim(session, company_id, user_id, line, allocation="not_acquisition", op_key=op_key,
                     reason=context["reason"])
        return {"assets": [], "journals": []}, _effects(claims=[line["source"]])

    return await _run(session, company_id, user_id, body, op_type="not_acquisition", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def link_depreciation(session, company_id, user_id, body: dict) -> dict:
    """Link a manual depreciation line to the assets it depreciated. Posts nothing: the
    journal already holds it; the assets take their share as accumulated depreciation."""

    async def plan(fa):
        line = await _open_line(session, company_id, body.get("source"))
        if line["kind"] != "depreciation":
            raise setup.unprocessable("Only a line crediting accumulated depreciation can be linked to assets.")
        allocations = body.get("allocations")
        if not isinstance(allocations, list) or not allocations:
            raise setup.unprocessable("Choose the assets this depreciation belongs to.")
        shares = []
        for a in allocations:
            asset_id = str((a or {}).get("asset_id") or "")
            asset = await _state(session, company_id, asset_id)
            if asset is None or not asset_id.startswith("fa:asset:") or asset.get("status") != "active":
                raise setup.unprocessable("Choose active assets to link the depreciation to.")
            shares.append((asset_id, asset["number"], _amount(a.get("amount"), "Each amount", positive=True)))
        total = sum((s for _, _, s in shares), Decimal(0))
        if total != _dec(line["amount"]):
            raise setup.unprocessable(f"The amounts add up to {_cents(total)}, not {_cents(line['amount'])}.")
        return ({"journals": [], "assets": [{"number": n, "accumulated_depreciation": _cents(s)} for _, n, s in shares]},
                {"line": line, "shares": shares, "generation": line["generation"]})

    async def apply(context, op_key):
        line = context["line"]
        movements = []
        for asset_id, _, share in context["shares"]:
            movements.append(await _move(
                session, company_id, user_id, asset_id, kind="posted", operation="link_depreciation",
                effective=line["date"], posting=line["date"], amounts={"accumulated_depreciation": share},
                op_key=op_key, contributions=[{"journal_id": line["journal_id"], "line_index": line["line_index"],
                                               "amount": str(share)}]))
        await _claim(session, company_id, user_id, line, allocation="link_depreciation", op_key=op_key,
                     asset_ids=[a for a, _, _ in context["shares"]])
        return {"assets": [], "journals": []}, _effects(movements=movements, claims=[line["source"]])

    return await _run(session, company_id, user_id, body, op_type="link_depreciation", keys=("manage_accounting",),
                      plan=plan, apply=apply)


# ── Assets added by hand ─────────────────────────────────────────────────────


async def _financials(session, company_id, fa: dict, body: dict) -> dict:
    """The checked financial details of an opening or contributed asset."""
    kind = body.get("kind")
    cls = await setup.get_class(session, company_id, body.get("class_id"))
    available = _date(body.get("available_date"), "The available-for-use date")
    cost = _amount(body.get("cost"), "Cost", positive=True)
    out = {"kind": kind, "cls": cls, "available": available, "cost": cost, "policy": setup.policy_of(cls, fa)}
    cut = setup.cutover(fa)
    if kind == "opening":
        if cut and available > cut:
            raise setup.unprocessable(f"Dated after the cutover {cut}. Enter the bill, or use Contributed or "
                                      "acquired without a bill.")
        dep = _amount(body.get("accumulated_depreciation"), "Accumulated depreciation")
        imp = _amount(body.get("accumulated_impairment"), "Accumulated impairment")
        if dep + imp > cost:
            raise setup.unprocessable("Accumulated depreciation and impairment cannot be more than the cost.")
        out.update(dep=dep, imp=imp, posting=cut or available)
    elif kind == "contributed":
        credit = str(body.get("credit_account") or "")
        if not credit:
            raise setup.unprocessable("Choose the account the asset was contributed from.")
        out.update(credit=credit, posting=available)
    else:
        raise setup.unprocessable("Choose how the asset came in: opening, contributed or a physical record.")
    return out


def _financials_shown(details: dict) -> tuple[list, list]:
    from celerp.services.module_journals import JournalLine

    if details["kind"] == "opening":
        return [], []
    lines = [JournalLine(account=details["cls"]["asset_account"], debit=details["cost"]),
             JournalLine(account=details["credit"], credit=details["cost"])]
    return [lines], [_journal_view(lines)]


async def _book_financials(session, company_id, user_id, asset_id: str, number: str, name: str, details: dict,
                           op_key: str) -> tuple[list, list]:
    """Post a hand-added asset's acquisition (contributed) and record its movement."""
    journals = []
    if details["kind"] == "opening":
        amounts = {"cost": details["cost"], "accumulated_depreciation": details["dep"],
                   "accumulated_impairment": details["imp"]}
        contributions = []
    else:
        (lines,), _ = _financials_shown(details)
        je_id = await _post(session, company_id, user_id, key=f"asset:{asset_id.removeprefix('fa:asset:')}:acq:1",
                            operation="acquisition", day=details["available"], memo=f"{number} {name}",
                            lines=lines, source_id=asset_id, href=f"/fixed-assets/assets/{asset_id}", label=number,
                            op_key=op_key)
        journals.append(je_id)
        amounts = {"cost": details["cost"]}
        contributions = [{"journal_id": je_id, "line_index": 0, "amount": str(details["cost"])}]
    movement = await _move(session, company_id, user_id, asset_id, kind=details["kind"], operation=details["kind"],
                           effective=details["available"], posting=details["posting"], amounts=amounts,
                           op_key=op_key, contributions=contributions, quantity=1, provenance="form")
    return journals, [movement]


async def create_asset(session, company_id, user_id, body: dict) -> dict:
    """Add an asset by hand: an opening asset, a contributed one, or a physical record."""
    kind = body.get("kind")
    physical = kind == "physical"

    async def plan(fa):
        name = str(body.get("name") or "").strip()
        if not name:
            raise setup.unprocessable("An asset needs a name.")
        (number,) = await _next_numbers(session, company_id, 1)
        if physical:
            return {"journals": [], "assets": [{"number": number, "name": name, "cost": None}]}, {
                "name": name, "number": number}
        details = await _financials(session, company_id, fa, body)
        _, shown = _financials_shown(details)
        return ({"journals": shown, "assets": [{"number": number, "name": name, "cost": _cents(details["cost"])}]},
                {"name": name, "number": number, "details": details})

    async def apply(context, op_key):
        number, name = context["number"], context["name"]
        if physical:
            asset_id = await _create_asset(session, company_id, user_id, number=number, name=name, class_id=None,
                                           status="physical", available_date=None, op_key=op_key, cost=None)
            return {"assets": [{"id": asset_id, "number": number}], "journals": []}, _effects(assets=[asset_id])
        details = context["details"]
        asset_id = await _create_asset(session, company_id, user_id, number=number, name=name,
                                       class_id=details["cls"]["id"], status="active",
                                       available_date=details["available"], op_key=op_key, policy=details["policy"])
        journals, movements = await _book_financials(session, company_id, user_id, asset_id, number, name, details,
                                                     op_key)
        return ({"assets": [{"id": asset_id, "number": number}], "journals": journals},
                _effects(assets=[asset_id], movements=movements, journals=journals))

    return await _run(session, company_id, user_id, body, op_type=f"asset:{kind}",
                      keys=("manage_fixed_assets",) if physical else ("manage_accounting",), plan=plan, apply=apply)


async def add_financial_details(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Give a physical record its class, date and cost, making it an active asset."""
    from celerp.services.company_lock import lock_projections

    async def plan(fa):
        row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
        if row is None or row.entity_type != "fa_asset":
            raise HTTPException(status_code=404, detail="No such asset.")
        if row.state.get("status") != "physical":
            raise conflict("This asset already has its financial details.")
        details = await _financials(session, company_id, fa, body)
        _, shown = _financials_shown(details)
        return ({"journals": shown, "assets": [{"number": row.state["number"], "name": row.state["name"],
                                                "cost": _cents(details["cost"])}]},
                {"asset": row.state, "details": details})

    async def apply(context, op_key):
        asset, details = context["asset"], context["details"]
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.financials", {
            "class_id": details["cls"]["id"], "status": "active", "available_date": details["available"],
            "policy": details["policy"], "financials_op_key": op_key})
        journals, movements = await _book_financials(session, company_id, user_id, asset_id, asset["number"],
                                                     asset["name"], details, op_key)
        return ({"assets": [{"id": asset_id, "number": asset["number"]}], "journals": journals},
                _effects(movements=movements, journals=journals))

    return await _run(session, company_id, user_id, body, op_type="financial_details", keys=("manage_accounting",),
                      plan=plan, apply=apply)


# ── Descriptive edits ────────────────────────────────────────────────────────


async def update_asset(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Change an asset's descriptive fields, or the account its depreciation is charged to
    (which takes the right to post journals), refused when ``previous`` no longer holds."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.journal_accounts import require_destinations
    from celerp.services.permissions import locked_authority

    changes, previous = body.get("changes"), body.get("previous")
    if not isinstance(changes, dict) or not isinstance(previous, dict):
        raise setup.unprocessable("Send the changes and the values they replace.")
    account = "depreciation_account" in changes
    await locked_authority(session, company_id, user_id, ("manage_accounting",) if account else ("manage_fixed_assets",))
    row = (await lock_projections(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    if set(changes) & {*BUCKETS, "quantity"}:
        raise setup.unprocessable(
            "This cost comes from its source lines. Use Replace source to change it." if row.state.get("sources")
            else "Use Correct to change the cost.")
    unknown = set(changes) - {*DESCRIPTIVE_FIELDS, "depreciation_account"}
    if unknown:
        raise setup.unprocessable(f"{sorted(unknown)[0]} cannot be edited here.")
    if any(row.state.get(k) != v for k, v in previous.items()):
        raise conflict("Changed by someone else, reload to see it.")
    changes = {k: (str(v).strip() or None) if v is not None else None for k, v in changes.items()}
    if "name" in changes and not changes["name"]:
        raise setup.unprocessable("An asset needs a name.")
    if account and changes["depreciation_account"]:
        await require_destinations(session, company_id, {changes["depreciation_account"]})
    changes = {k: v for k, v in changes.items() if row.state.get(k) != v}
    if changes:
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {"changes": changes})
    return {**row.state, **changes, "id": asset_id}


# ── Undo ─────────────────────────────────────────────────────────────────────


def _reversed_lines(je: dict) -> dict:
    return {"lines": [{"account": e.get("account"), "debit": _cents(e.get("credit")), "credit": _cents(e.get("debit"))}
                      for e in je.get("entries") or []]}


async def _undo(session, company_id, user_id, target_key: str, body: dict) -> dict:
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import reverse_journal

    async def plan(fa):
        op = await _state(session, company_id, f"fa:op:{target_key}")
        if op is None or op.get("type") in (None, "undo"):
            raise setup.unprocessable("There is nothing to undo here.")
        if op.get("undone_by"):
            raise conflict("This was already undone.")
        effects = op.get("effects") or {}
        claims = [sources.claim_id(s) for s in effects.get("claims") or []]
        rows = await lock_projections(session, company_id, [*effects.get("assets", []), *claims])
        assets = [rows[a].state for a in effects.get("assets", [])]
        for a in assets:
            if int(a.get("movement_count") or 0) > 1:
                raise conflict(f"{a['number']} has changed since it was registered. Use Correct instead.")
        movements = {m: await _state(session, company_id, m) for m in effects.get("movements", [])}
        later = [m for m, s in movements.items() if s and s.get("asset_id") not in effects.get("assets", [])
                 and (await _state(session, company_id, s["asset_id"]) or {}).get("movement_count", 0)
                 > int(m.rsplit(":", 1)[1])]
        if later:
            raise conflict("These assets have changed since. Use Correct instead.")
        lock = (await setup.company_settings(session, company_id)).get("lock_date")
        locked = sorted(s["posting_date"] for s in movements.values() if s and lock and s["posting_date"] <= lock)
        if locked:
            raise conflict(f"{locked[0]} is in a locked period, so this can no longer be undone. Use Correct instead.")
        journals = {j: await _state(session, company_id, j) for j in effects.get("journals", [])}
        shown = {"assets": [{"number": a["number"]} for a in assets],
                 "journals": [_reversed_lines(je) for je in journals.values() if je and je.get("status") == "posted"]}
        return shown, {"op": op, "effects": effects, "movements": movements, "claims": claims}

    async def apply(context, op_key):
        effects = context["effects"]
        for je_id in effects.get("journals", []):
            await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                                  key=je_id.removeprefix(f"je:mod:{MODULE}:"), operation="undo",
                                  reason="Undone")
        for movement_id, mv in context["movements"].items():
            await _move(session, company_id, user_id, mv["asset_id"], kind="reversal", operation="undo",
                        effective=mv["effective_date"], posting=mv["posting_date"],
                        amounts={k: -_dec(v) for k, v in (mv.get("amounts") or {}).items()}, op_key=op_key,
                        contributions=[{**c, "amount": str(-_dec(c.get("amount")))} for c in mv.get("contributions") or []],
                        quantity=str(-_dec(mv.get("quantity"))), reverses=movement_id)
        for claim in context["claims"]:
            state = await _state(session, company_id, claim)
            await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.released", {
                "generation": int(state.get("generation") or 0), "op_key": op_key},
                key=f"{claim}:release:{int(state.get('generation') or 0)}")
        for asset_id in effects.get("assets", []):
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status", {"status": "undone"})
        for maint_id in effects.get("maint", []):
            await _emit(session, company_id, user_id, maint_id, "fa_maint", "fa.maint.status", {"status": "undone"})
        # An asset the operation disposed of or placed in service, and a class it moved to
        # another model, go back to what they were.
        for status in effects.get("statuses", []):
            await _emit(session, company_id, user_id, status["id"], "fa_asset", "fa.asset.status",
                        {"status": status["before"], "disposal": None})
        for cls in effects.get("classes", []):
            await _emit(session, company_id, user_id, cls["id"], "fa_class", "fa.class.updated",
                        {"changes": cls["before"]})
        await _emit(session, company_id, user_id, f"fa:op:{target_key}", "fa_op", "fa.op.undone", {"undo_key": op_key})
        return {"undone": target_key, "assets": [], "journals": []}, _effects()

    return await _run(session, company_id, user_id, body, op_type="undo", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def undo_asset(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Undo the operation that created an asset (and every asset it created with it)."""
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset_id.startswith("fa:asset:"):
        raise HTTPException(status_code=404, detail="No such asset.")
    return await _undo(session, company_id, user_id, asset["op_key"], body)


async def undo_source(session, company_id, user_id, body: dict) -> dict:
    """Undo whatever was done with a source line, releasing it back to intake."""
    claim = await _state(session, company_id, sources.claim_id(str(body.get("source") or "")))
    if claim is None or claim.get("status") != "claimed":
        raise setup.unprocessable("This line is not registered.")
    return await _undo(session, company_id, user_id, claim["op_key"], body)
