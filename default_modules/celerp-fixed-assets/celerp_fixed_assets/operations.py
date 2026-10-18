# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asset operations: registering source lines, adding assets by hand, and undoing both.

Every write follows one contract. The caller takes the company lock and its authority,
then the operation is planned from what the books hold now. A preview applies the plan,
takes it back, and returns it with a fingerprint of it, so it refuses whatever a confirm
would; a confirm plans again under the lock and refuses when the
fingerprint differs, so nothing is posted that the user did not see. A confirmed
operation is recorded under its operation key (``fa:op:<key>``) with a hash of the
request: the same key and request returns the recorded result, the same key with a
different request is refused.

An asset's financial figures change only by movements (``fa:mv:<asset>:<n>``), so the
asset always equals the fold of its movements. Undo reverses an operation's journals,
movements and claims as one more operation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import date
from decimal import Decimal

from celerp.models.projections import Projection
from celerp.services.money import books_currency, round_money
from celerp_fixed_assets import setup, sources
from celerp_fixed_assets.projections import BUCKETS, _dec, money, said
from celerp_fixed_assets.store import _emit, _entities, _state, recorded
from fastapi import HTTPException
from sqlalchemy import select

MODULE = "celerp-fixed-assets"
REGISTER_MODES = ("separate", "quantity", "expense", "maintenance")
DESCRIPTIVE_FIELDS = ("name", "tag", "description", "serial", "make", "model", "notes")
# The member who hears about problems a custodian reports on the asset.
RESPONSIBLE = "responsible_user_id"
_OP_KEY = re.compile(r"[A-Za-z0-9._-]{1,64}")
_NOT_INTENT = ("preview", "fingerprint", "op_key")


def conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


# ── Shared pieces ────────────────────────────────────────────────────────────


def _amount(value, field: str, *, positive: bool = False) -> Decimal:
    amount = setup.number(value if value not in (None, "") else "0", field)
    if amount < 0 or (positive and amount == 0):
        raise setup.unprocessable(f"{field} must be more than zero." if positive else f"{field} must be zero or more.")
    return amount


def _money_in(value, field: str, currency: str, *, positive: bool = False) -> Decimal:
    """A money amount the user entered, rounded once to the precision of ``currency``."""
    amount = round_money(_amount(value, field, positive=positive), currency)
    if positive and amount == 0:
        raise setup.unprocessable(f"{field} must be more than zero.")
    return amount


def _date(value, field: str) -> str:
    return setup.day(value, field).isoformat()


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def movements_query(company_id, asset_id: str):
    """An asset's movements, read through the index on the asset a projection belongs to."""
    from celerp.models.projections import ASSET_ID

    return select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_movement", ASSET_ID == asset_id)


async def asset_movements(session, company_id, asset_id: str) -> list[dict]:
    """An asset's movements in the order they were recorded."""
    return recorded((await session.execute(movements_query(company_id, asset_id))).all())


async def _next_numbers(session, company_id, count: int, taken=()) -> list[str]:
    """The next ``count`` asset numbers after the company's and the ``taken`` ones about to
    be added with them. Numbers are never reused, undone assets included."""
    numbers = [str(s.get("number") or "") for _, s in await _entities(session, company_id, "fa_asset")]
    used = [int(n[3:]) for n in [*numbers, *taken] if n.startswith("FA-") and n[3:].isdigit()]
    start = max(used, default=0) + 1
    return [f"FA-{n:04d}" for n in range(start, start + count)]


def _lines(debit_account: str, credit_account: str, amount: Decimal, roles: tuple = (None, None)):
    """Two lines moving ``amount`` from ``credit_account`` to ``debit_account``, flipped when
    negative. ``roles`` are the posting roles the two accounts serve, in the same order."""
    from celerp.services.module_journals import JournalLine

    debit_role, credit_role = roles
    if amount < 0:
        debit_account, credit_account, amount = credit_account, debit_account, -amount
        debit_role, credit_role = credit_role, debit_role
    return [JournalLine(account=debit_account, debit=amount, role=debit_role),
            JournalLine(account=credit_account, credit=amount, role=credit_role)]


def _journal_view(lines, currency: str) -> dict:
    return {"lines": [{"account": ln.account, "debit": money(ln.debit, currency), "credit": money(ln.credit, currency)}
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
                posting: str, amounts: dict, op_key: str, contributions=(), quantity=0,
                surplus_account: str | None = None, **extra) -> str:
    """Record one movement on an asset and mirror it onto the asset. ``surplus_account`` is
    where a movement recognizing revaluation surplus posted it; the asset keeps it there."""
    asset = await _state(session, company_id, asset_id)
    movement_id = f"fa:mv:{asset_id.removeprefix('fa:asset:')}:{int(asset.get('movement_count') or 0) + 1}"
    amounts = {k: str(v) for k, v in amounts.items() if k in BUCKETS}
    await _emit(session, company_id, user_id, movement_id, "fa_movement", "fa.movement.recorded", {
        "asset_id": asset_id, "kind": kind, "operation": operation, "effective_date": effective,
        "posting_date": posting, "amounts": amounts, "contributions": list(contributions),
        "quantity": quantity, "op_key": op_key, **extra}, key=f"{movement_id}:rec")
    moved = {"movement_id": movement_id, "amounts": amounts, "quantity": quantity}
    if surplus_account:
        moved["surplus_account"] = surplus_account
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.moved", moved, key=f"{movement_id}:mv")
    return movement_id


async def catch_up(session, company_id, user_id, asset_id: str, *, day: str, periods: dict, op_key: str,
                   journal_id: str | None) -> dict:
    """Record the depreciation an operation brings up to date before it (``periods``, by
    month) as a Depreciation movement of its own on the operation's journal. Returns what
    the operation's movement carries: that it charges no month itself, and the catch-up's id
    when one was owed."""
    if not periods:
        return {"periods": {}}
    charged = sum((_dec(v) for v in periods.values()), Decimal(0))
    movement_id = await _move(session, company_id, user_id, asset_id, kind="posted", operation="depreciation",
                              effective=day, posting=day, amounts={"accumulated_depreciation": charged},
                              op_key=op_key, periods=periods, journal_id=journal_id)
    return {"periods": {}, "catch_up": movement_id}


async def _continued(session, company_id, lines: list) -> list:
    """``lines``, each account posted for a role checked it can still take that role's
    postings: a balance keeps moving on the account it was recognized on, so one that no
    longer can is refused rather than split onto another."""
    from celerp.services.account_roles import continue_role

    for line in lines:
        if line.role:
            await continue_role(session, company_id, line.role, line.account)
    return lines


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
        "line_index": line["line_index"], "account": line["account"], "kind": line["kind"], "date": line["date"],
        **extra}, key=f"{claim}:claim:{line['generation']}")
    if getattr(event, "was_deduped", False):
        raise conflict(f"{line['description']} was already registered.")


# ── The operation contract ───────────────────────────────────────────────────


def _op_key(body: dict) -> str:
    key = body.get("op_key")
    if not isinstance(key, str) or not _OP_KEY.fullmatch(key):
        raise setup.unprocessable("Every change carries an operation key from its form.")
    return key


def _intent_hash(op_type: str, body: dict) -> str:
    """What an operation's form asked for, without how it was sent."""
    return _hash({"type": op_type, **{k: v for k, v in body.items() if k not in _NOT_INTENT}})


def _same_intent(done: dict, intent_hash: str) -> None:
    """Refuse an operation key already used with other details."""
    if done.get("intent_hash") != intent_hash:
        raise conflict("This change was already made with different details. Reload and start again.")


async def _run(session, company_id, user_id, body: dict, *, op_type: str, keys: tuple[str, ...], plan, apply,
               managed: bool = True):
    """Preview or confirm one operation. ``plan(fa)`` returns ``(shown, context)``: what
    the preview shows, which the fingerprint covers, and what ``apply`` needs to do it.
    ``context["generation"]`` (a source line's claim generation) names a stale form, and
    ``context["basis"]``, when given, is what the plan was read from that the preview does
    not show (the fingerprint covers it too). ``apply`` finds the intent's hash in
    ``context["intent_hash"]``. Only a physical record, which carries no
    money, is recorded before the first managed period is confirmed (``managed=False``)."""
    from celerp.services.permissions import locked_authority

    op_key = _op_key(body)
    await locked_authority(session, company_id, user_id, keys)
    fa = await (setup.require_managed if managed else setup.require_started)(session, company_id)
    intent_hash = _intent_hash(op_type, body)
    if not body.get("preview"):
        done = await _state(session, company_id, f"fa:op:{op_key}")
        if done is not None:
            _same_intent(done, intent_hash)
            return done["result"]
    shown, context = await plan(fa)
    generation = context.get("generation", 0)
    fingerprint = f"{generation}.{_hash({'shown': shown, 'basis': context.get('basis')})}"
    if body.get("preview"):
        result = copy.deepcopy({**shown, "fingerprint": fingerprint})
        await label_lines(session, company_id, result)
        context["intent_hash"] = intent_hash
        # Review refuses what Confirm would: the operation is applied and taken back.
        savepoint = await session.begin_nested()
        try:
            await _applied(session, company_id, apply, context, op_key)
        finally:
            await savepoint.rollback()
        return result
    previewed = str(body.get("fingerprint") or "")
    if previewed != fingerprint:
        if previewed.split(".", 1)[0] != str(generation):
            raise conflict("Line was registered and released since you opened this form. Review again.")
        raise conflict("Assets, accounts or postings changed since preview. Review again.")
    context["intent_hash"] = intent_hash
    result, effects = await _applied(session, company_id, apply, context, op_key)
    await _emit(session, company_id, user_id, f"fa:op:{op_key}", "fa_op", "fa.op.recorded", {
        "type": op_type, "intent_hash": intent_hash, "result": result, "effects": effects},
        key=f"fa:op:{op_key}")
    return result


def _journal_lines(value) -> list[dict]:
    """Every journal line shown anywhere in ``value``."""
    if isinstance(value, list):
        return [ln for v in value for ln in _journal_lines(v)]
    if not isinstance(value, dict):
        return []
    lines = [ln for ln in value.get("lines") or [] if isinstance(ln, dict) and "account" in ln] \
        if isinstance(value.get("lines"), list) else []
    return [*lines, *(ln for k, v in value.items() if k != "lines" for ln in _journal_lines(v))]


async def label_lines(session, company_id, preview: dict) -> None:
    """Each previewed journal line names its account as the user knows it. A line already
    labelled (another company's, in a transfer) keeps its label."""
    lines = [ln for ln in _journal_lines(preview) if "label" not in ln]
    labels = await setup.account_labels(session, company_id, [ln["account"] for ln in lines if ln["account"]])
    for ln in lines:
        ln["label"] = labels.get(ln["account"]) or ln["account"]


async def _applied(session, company_id, apply, context: dict, op_key: str):
    """``apply``, a refusal on an account a class posts to said as the class's to fix."""
    from celerp_fixed_assets.fixaccount import class_refusal

    try:
        return await apply(context, op_key)
    except HTTPException as e:
        raise await class_refusal(session, company_id, e) from e


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
        currency = await setup.currency(session, company_id)
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
            return {"journals": [_journal_view(journal, currency)], "assets": []}, context
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
            costs = allocate_pro_rata(amount, [Decimal(1)] * count, currency)
            each = [(cost, 1) for cost in costs]
        else:
            each = [(amount, line["quantity"])]
        numbers = await _next_numbers(session, company_id, len(each))
        journals = ([_reclass(line, cls["asset_account"], cost) for cost, _ in each]
                    if cls["asset_account"] != line["account"] else [])
        context.update(cls=cls, name=name, available=available, each=each, numbers=numbers, journals=journals,
                       policy=setup.policy_of(cls, fa))
        return {"journals": [_journal_view(j, currency) for j in journals],
                "assets": [{"number": n, "name": name, "cost": money(c, currency)} for n, (c, _) in zip(numbers, each)],
                "registers": _registers(cls, available, context["policy"])}, context

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
                    "bill_ref": line.get("document_ref"), "work": line["description"], "date": line["date"], "journal_id": je_id, "status": "active", "op_key": op_key})
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


# What a manual line is marked as when it is not registered: the reason it needs, and the
# lines it applies to.
_DISPOSITIONS = {
    "not_acquisition": ("A reason is required to mark a line as not an acquisition.",
                        "Only a manual journal line debiting an asset account can be marked as not an acquisition.",
                        ("acquisition",)),
    "keep": ("A reason is required to keep a depreciation journal as booked.",
             "Only a manual journal line crediting accumulated depreciation can be kept as booked.",
             ("depreciation",)),
}


async def disposition(session, company_id, user_id, body: dict, allocation: str) -> dict:
    """Mark a manual journal line as not an acquisition, or keep a depreciation journal as
    booked without allocating it to assets, with the reason. Posts nothing; the line leaves
    intake and the reconciliation lists it as noted with the reason."""
    no_reason, wrong_line, kinds = _DISPOSITIONS[allocation]

    async def plan(fa):
        reason = str(body.get("reason") or "").strip()
        if not reason:
            raise setup.unprocessable(no_reason)
        line = await _open_line(session, company_id, body.get("source"))
        if not line["journal_id"].startswith("je:manual:") or line["kind"] not in kinds:
            raise setup.unprocessable(wrong_line)
        return ({"journals": [], "assets": [], "reason": reason},
                {"line": line, "reason": reason, "generation": line["generation"]})

    async def apply(context, op_key):
        line = context["line"]
        await _claim(session, company_id, user_id, line, allocation=allocation, op_key=op_key,
                     reason=context["reason"])
        return {"assets": [], "journals": []}, _effects(claims=[line["source"]])

    return await _run(session, company_id, user_id, body, op_type=allocation, keys=("manage_accounting",),
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
        currency = await setup.currency(session, company_id)
        shares = []
        for a in allocations:
            asset_id = str((a or {}).get("asset_id") or "")
            asset = await _state(session, company_id, asset_id)
            if asset is None or not asset_id.startswith("fa:asset:") or asset.get("status") != "active":
                raise setup.unprocessable("Choose active assets to link the depreciation to.")
            shares.append((asset_id, asset["number"], _money_in(a.get("amount"), "Each amount", currency, positive=True)))
        total = sum((s for _, _, s in shares), Decimal(0))
        if total != round_money(_dec(line["amount"]), currency):
            raise setup.unprocessable(f"The amounts add up to {said(total, currency)}, not {said(line['amount'], currency)}.")
        return ({"journals": [], "assets": [{"number": n, "accumulated_depreciation": money(s, currency)} for _, n, s in shares]},
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


WRITTEN_OFF_OVER_COST = "Accumulated depreciation and impairment cannot be more than the cost."


def after_cutover(cut: str) -> str:
    """Why an opening asset cannot be dated after the cutover, and what to do instead."""
    return f"Dated after the cutover {cut}. Enter the bill, or use Contributed or acquired without a bill."


def _registers(cls: dict, available, policy: dict) -> dict:
    """What a review shows an asset will be registered as: its class, date and depreciation."""
    return {"class": cls["name"], "available_date": str(available), "policy": policy}


async def _financials(session, company_id, fa: dict, body: dict) -> dict:
    """The checked financial details of an opening or contributed asset."""
    kind = body.get("kind")
    cls = await setup.get_class(session, company_id, body.get("class_id"))
    available = _date(body.get("available_date"), "The available-for-use date")
    currency = await setup.currency(session, company_id)
    cost = _money_in(body.get("cost"), "Cost", currency, positive=True)
    out = {"kind": kind, "cls": cls, "available": available, "cost": cost, "currency": currency,
           "policy": setup.policy_of(cls, fa)}
    cut = setup.cutover(fa)
    if kind == "opening":
        if cut and available > cut:
            raise setup.unprocessable(after_cutover(cut))
        dep = _money_in(body.get("accumulated_depreciation"), "Accumulated depreciation", currency)
        imp = _money_in(body.get("accumulated_impairment"), "Accumulated impairment", currency)
        if dep + imp > cost:
            raise setup.unprocessable(WRITTEN_OFF_OVER_COST)
        out.update(dep=dep, imp=imp, posting=cut or available)
    elif kind == "contributed":
        credit = str(body.get("credit_account") or "")
        if not credit:
            raise setup.unprocessable("Choose the account the asset was contributed from.")
        lock = (await setup.company_settings(session, company_id)).get("lock_date")
        setup.require_open(date.fromisoformat(lock) if lock else None, available)
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
    return [lines], [_journal_view(lines, details["currency"])]


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
        return ({"journals": shown, "assets": [{"number": number, "name": name, "cost": money(details["cost"], details["currency"])}],
                 "registers": _registers(details["cls"], details["available"], details["policy"])},
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
                      keys=("manage_fixed_assets",) if physical else ("manage_accounting",), plan=plan, apply=apply,
                      managed=not physical)


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
                                                "cost": money(details["cost"], details["currency"])}],
                 "registers": _registers(details["cls"], details["available"], details["policy"])},
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


def by_tag(assets: list[tuple[str, dict]], tag: str) -> tuple[str, dict] | None:
    """The asset carrying ``tag``, matched without case, else the asset numbered ``tag``: a
    tag is what is scanned, and an asset without one is scanned by its number."""
    needle = tag.strip().casefold()
    live = [(eid, s) for eid, s in assets if s.get("status") != "undone"]
    return (next(((eid, s) for eid, s in live if (s.get("tag") or "").casefold() == needle), None)
            or next(((eid, s) for eid, s in live if (s.get("number") or "").casefold() == needle), None))


def tag_taken(assets: list[tuple[str, dict]], asset_id: str, tag: str) -> str | None:
    """Why ``tag`` cannot go on ``asset_id``: another asset carries it, or it is another
    asset's number. None when it is free."""
    held = by_tag(assets, tag)
    if held is None or held[0] == asset_id:
        return None
    number = held[1].get("number")
    if (held[1].get("tag") or "").casefold() == tag.strip().casefold():
        return f"Tag {held[1]['tag']} is already on {number}."
    return f"{number} is another asset's number, so it cannot be a tag here."


async def _tag_free(session, company_id, asset_id: str, tag: str) -> None:
    """Refuse a tag that is taken (tag_taken). The caller holds the company lock
    (lock_projections), so two assets never claim one tag."""
    problem = tag_taken(await _entities(session, company_id, "fa_asset"), asset_id, tag)
    if problem:
        raise setup.unprocessable(problem)


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
    unknown = set(changes) - {*DESCRIPTIVE_FIELDS, RESPONSIBLE, "depreciation_account"}
    if unknown:
        raise setup.unprocessable(f"{sorted(unknown)[0]} cannot be edited here.")
    if any(row.state.get(k) != v for k, v in previous.items()):
        raise conflict("Changed by someone else, reload to see it.")
    changes = {k: (str(v).strip() or None) if v is not None else None for k, v in changes.items()}
    if "name" in changes and not changes["name"]:
        raise setup.unprocessable("An asset needs a name.")
    if changes.get("tag"):
        await _tag_free(session, company_id, asset_id, changes["tag"])
    if changes.get(RESPONSIBLE):
        from celerp_fixed_assets.moves import member_name

        if not await member_name(session, company_id, changes[RESPONSIBLE]):
            raise setup.unprocessable("The responsible person is not a member of this company. Choose a member.")
    if account and changes["depreciation_account"]:
        await require_destinations(session, company_id, {changes["depreciation_account"]})
    changes = {k: v for k, v in changes.items() if row.state.get(k) != v}
    if changes:
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated", {"changes": changes})
    return {**row.state, **changes, "id": asset_id}


# ── Undo ─────────────────────────────────────────────────────────────────────


def _reversed_lines(je: dict, currency: str) -> dict:
    return {"lines": [{"account": e.get("account"), "debit": money(e.get("credit"), currency), "credit": money(e.get("debit"), currency)}
                      for e in je.get("entries") or []]}


async def reverse_move(session, company_id, user_id, movement_id: str, mv: dict, op_key: str,
                       operation: str = "undo") -> str:
    """Record the movement that undoes ``mv``: every amount, contribution and quantity negated,
    and then the same for the depreciation it brought up to date (:func:`catch_up`)."""
    reversed_id = await _move(session, company_id, user_id, mv["asset_id"], kind="reversal", operation=operation,
                              effective=mv["effective_date"], posting=mv["posting_date"],
                              amounts={k: -_dec(v) for k, v in (mv.get("amounts") or {}).items()}, op_key=op_key,
                              contributions=[{**c, "amount": str(-_dec(c.get("amount")))}
                                             for c in mv.get("contributions") or []],
                              quantity=str(-_dec(mv.get("quantity"))), reverses=movement_id)
    if mv.get("catch_up"):
        await reverse_move(session, company_id, user_id, mv["catch_up"],
                           await _state(session, company_id, mv["catch_up"]), op_key, operation)
    return reversed_id


def changes(movements: list[dict]) -> str:
    """What ``movements`` did, named as the asset's history names them ("Impairment, Improvement")."""
    from ui.i18n import t

    return ", ".join(dict.fromkeys(t(f"fixed_assets.movement.{m['operation']}") for m in movements))


async def way_back(session, company_id, asset: dict, changed: str, correct: str) -> str:
    """Why ``asset`` cannot be taken back because it ``changed`` since, and the way back: for a
    sold or disposed asset, ending that first; otherwise ``correct``, the Correct an active
    asset offers, said as it reads where the message is shown."""
    number = asset["number"]
    sale = asset.get("sale") or {}
    if asset.get("status") == "sold" and sale.get("invoice_id"):
        invoice = await _state(session, company_id, sale["invoice_id"]) or {}
        ref = invoice.get("doc_number") or invoice.get("ref_id") or sale["invoice_id"].removeprefix("doc:")
        return (f"{number} was sold on invoice {ref}. Void that invoice and cancel the sale on {number}'s page, "
                "then use Correct there.")
    if asset.get("status") in ("sold", "disposed"):
        return f"{number} was disposed of. Undo the disposal on its page, then use Correct there."
    return f"{number} {changed}. {correct}"


async def _undo(session, company_id, user_id, target_keys: list[str], body: dict, *, subject: str | None = None,
                done=None) -> dict:
    """Undo the operations ``target_keys`` together, as one operation. ``subject`` is what the
    result names as undone (the one operation by default); ``done(op_key)`` records anything
    else the undo changes, such as the import the operations belong to."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.module_journals import reverse_journal

    async def plan(fa):
        ops = [await _state(session, company_id, f"fa:op:{key}") for key in target_keys]
        if not ops or any(op is None or op.get("type") in (None, "undo") for op in ops):
            raise setup.unprocessable("There is nothing to undo here.")
        if any(op.get("undone_by") for op in ops):
            raise conflict("This was already undone.")
        transfer = next((op for op in ops if op.get("type") == "transfer_in"), None)
        if transfer:
            source = transfer["result"]["source"]
            raise conflict(f"This asset was transferred in from {source['company_name']}. Undo the transfer on "
                           f"{source['number']} there instead.")
        effects = _effects(**{k: [x for op in ops for x in (op.get("effects") or {}).get(k) or []]
                              for k in ("assets", "movements", "claims", "journals", "maint", "statuses",
                                        "changes", "classes")})
        effects["unchanged"] = {k: v for op in ops
                                for k, v in ((op.get("effects") or {}).get("unchanged") or {}).items()}
        claims = [sources.claim_id(s) for s in effects["claims"]]
        rows = await lock_projections(session, company_id, [*effects["assets"], *claims])
        assets = [rows[a].state for a in effects["assets"]]
        moved = [a for a in assets if int(a.get("movement_count") or 0) > 1]
        changed = [a["number"] for a in moved]
        if len(changed) == 1:
            raise conflict(await way_back(session, company_id, moved[0], "has changed since it was registered",
                                          "Use Correct instead."))
        if changed:
            raise conflict(f"These assets have changed since they were added: {', '.join(changed)}. "
                           "Use Correct on each instead.")
        movements = {m: await _state(session, company_id, m) for m in effects["movements"]}
        # Per asset it did not create, the operation's last movement is still the asset's latest.
        last: dict[str, int] = {}
        for m, s in movements.items():
            if s and s.get("asset_id") not in effects.get("assets", []):
                last[s["asset_id"]] = max(last.get(s["asset_id"], 0), int(m.rsplit(":", 1)[1]))
        later = [a for a, n in last.items() if (await _state(session, company_id, a) or {}).get("movement_count", 0) > n]
        if later:
            raise conflict("These assets have changed since. Use Correct instead.")
        # What the operation left as it was (an asset's movements, a class's version) is still so.
        for entity_id, fields in (effects.get("unchanged") or {}).items():
            now = await _state(session, company_id, entity_id) or {}
            if any(int(now.get(k) or 0) > v for k, v in fields.items()):
                raise conflict("This has changed since, so it can no longer be undone.")
        settings = await setup.company_settings(session, company_id)
        lock = settings.get("lock_date")
        locked = sorted(s["posting_date"] for s in movements.values() if s and lock and s["posting_date"] <= lock)
        if locked:
            raise conflict(f"{locked[0]} is in a locked period, so this can no longer be undone. Use Correct instead.")
        journals = {j: await _state(session, company_id, j) for j in effects.get("journals", [])}
        shown = {"assets": [{"number": a["number"]} for a in assets],
                 "journals": [_reversed_lines(je, books_currency(settings)) for je in journals.values() if je and je.get("status") == "posted"]}
        return shown, {"effects": effects, "movements": movements, "claims": claims}

    async def apply(context, op_key):
        effects = context["effects"]
        for je_id in effects.get("journals", []):
            await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                                  key=je_id.removeprefix(f"je:mod:{MODULE}:"), operation="undo",
                                  reason="Undone")
        for movement_id, mv in context["movements"].items():
            await reverse_move(session, company_id, user_id, movement_id, mv, op_key)
        for claim in context["claims"]:
            state = await _state(session, company_id, claim)
            await _emit(session, company_id, user_id, claim, "fa_src", "fa.src.released", {
                "generation": int(state.get("generation") or 0), "op_key": op_key},
                key=f"{claim}:release:{int(state.get('generation') or 0)}")
        for asset_id in effects.get("assets", []):
            await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status", {"status": "undone"})
        for maint_id in effects.get("maint", []):
            await _emit(session, company_id, user_id, maint_id, "fa_maint", "fa.maint.status", {"status": "undone"})
        # An asset the operation disposed of, placed in service or reclassified, and a class
        # it moved to another model, go back to what they were.
        for status in effects.get("statuses", []):
            await _emit(session, company_id, user_id, status["id"], "fa_asset", "fa.asset.status",
                        {"status": status["before"], "disposal": None})
        for asset in effects.get("changes", []):
            await _emit(session, company_id, user_id, asset["id"], "fa_asset", "fa.asset.updated",
                        {"changes": asset["before"]})
        for cls in effects.get("classes", []):
            await _emit(session, company_id, user_id, cls["id"], "fa_class", "fa.class.updated",
                        {"changes": cls["before"]})
        for key in target_keys:
            await _emit(session, company_id, user_id, f"fa:op:{key}", "fa_op", "fa.op.undone", {"undo_key": op_key})
        if done is not None:
            await done(op_key)
        return {"undone": subject or target_keys[0], "assets": [], "journals": []}, _effects()

    return await _run(session, company_id, user_id, body, op_type="undo", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def undo_asset(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Undo the operation that created an asset (and every asset it created with it)."""
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset_id.startswith("fa:asset:"):
        raise HTTPException(status_code=404, detail="No such asset.")
    if asset.get("import_batch"):
        from celerp_fixed_assets import imports

        return await imports.undo_import(session, company_id, user_id, asset["import_batch"], body)
    return await _undo(session, company_id, user_id, [asset["op_key"]], body)


async def undo_source(session, company_id, user_id, body: dict) -> dict:
    """Undo whatever was done with a source line, releasing it back to intake."""
    claim = await _state(session, company_id, sources.claim_id(str(body.get("source") or "")))
    if claim is None or claim.get("status") != "claimed":
        raise setup.unprocessable("This line is not registered.")
    return await _undo(session, company_id, user_id, [claim["op_key"]], body)
