# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Selling an asset through an invoice, and every change to the sale after it.

Sell drafts an invoice with a line naming the asset; the asset waits on it (sale
"pending"). The invoice's entry credits the sale clearing account with the line's share
of the net, and finalizing it disposes of the asset in the same transaction, debiting
clearing with the same amount, so clearing nets to zero per sale. Voiding the invoice or
reverting it to draft reverses the disposal and the asset waits again; unvoiding or
finalizing again completes it from what the restored entry posted.

A credit note on the invoice either credits part of the price (the gain or loss is worked
out again from what is left of the price, each on its own account) or, with
the line marked returned, takes the asset back at what is left of the sale: the asset
returns to the books as it stood on the sale date, with the depreciation brought up to
then kept. Voiding a credit note undoes what it did.

Core hands each of those document changes to on_asset_lines (the doc_asset_lines slot);
a refusal here refuses the document action.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from celerp.models.projections import Projection
from celerp.services.account_roles import line_has_role, line_roles
from celerp.services.money import currency_dp, round_money
from celerp_fixed_assets import disposal, runs, setup
from celerp_fixed_assets.projections import money, said
from celerp_fixed_assets.operations import (
    MODULE,
    _continued,
    _date,
    _dec,
    _effects,
    _journal_view,
    _money_in,
    _move,
    _post,
    _run,
    asset_movements,
    catch_up,
    conflict,
    in_use,
    label_lines,
    reverse_move,
)
from celerp_fixed_assets.store import _emit, _state
from fastapi import HTTPException
from sqlalchemy import select

# The permissions a person needs to post or reverse a document that sells or credits an
# asset, and to write one.
SALE_KEYS = ("finalize_documents", "manage_fixed_assets")
_WRITE_KEYS = ("edit_documents", "manage_fixed_assets")
_AD, _IMP, _SURPLUS = "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus"
_TAX_FIELDS = ("tax_rate", "taxes")
_REVERSES = ("void", "revert")


def doc_number(state: dict | None, doc_id: str) -> str:
    """What a document is called: its number, else its draft reference (a deleted draft's id
    carries that reference)."""
    state = state or {}
    return str(state.get("doc_number") or state.get("ref_id") or doc_id.removeprefix("doc:"))


def _href(asset_id: str) -> str:
    return f"/fixed-assets/assets/{asset_id}"


def _carries(doc: dict | None, asset_id: str) -> dict | None:
    """The document's line for ``asset_id``."""
    from celerp.services.doc_asset_lines import asset_lines

    return next((li for li in asset_lines(doc or {}) if li["asset_id"] == asset_id), None)


def _credited(sale: dict) -> Decimal:
    """What the live credit notes crediting part of the price took off the proceeds."""
    return sum((_dec(c["amount"]) for c in (sale.get("credits") or {}).values()
                if c.get("live") and c.get("kind") == "adjust"), Decimal(0))


_GAIN, _LOSS, _CLEARING = "disposal_gain", "disposal_loss", "asset_disposal_clearing"
_RESULT_ROLES = {_GAIN, _LOSS}
_CARRIED_ROLES = {_SURPLUS, "retained_earnings"}


async def _cleared(session, company_id, settings: dict, doc_id: str, asset_id: str, number: str
                   ) -> tuple[str | None, Decimal]:
    """The clearing account a posted document's entries moved for ``asset_id``, and debit
    less credit on it, read from the lines posted for the clearing role."""
    rows = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(f"je:auto:{doc_id}:", autoescape=True)))).scalars().all()
    net: dict[str, Decimal] = {}
    for state in rows:
        if state.get("status") != "posted":
            continue
        for e in state.get("entries") or []:
            if e.get("asset_id") == asset_id and line_has_role(settings, e, _CLEARING):
                net[e["account"]] = net.get(e["account"], Decimal(0)) + _dec(e.get("debit")) - _dec(e.get("credit"))
    used = [(a, v) for a, v in net.items() if v]
    if len(used) > 1:
        raise conflict(f"{number}: the document cleared its line through more than one account "
                       f"({', '.join(a for a, _ in used)}).")
    return used[0] if used else (None, Decimal(0))


async def _held(session, company_id, settings: dict, sale: dict) -> dict[str, list]:
    """The gain and loss the sale's own posted journals hold, by account in the order they
    were first posted: ``{account: [roles it served, credit less debit]}``."""
    held: dict[str, list] = {}
    for je_id in sale.get("journals") or ():
        je = await _state(session, company_id, je_id) or {}
        if je.get("status") != "posted":
            continue
        for e in je.get("entries") or []:
            roles = set(line_roles(settings, e)) & _RESULT_ROLES
            if roles:
                slot = held.setdefault(e["account"], [set(), Decimal(0)])
                slot[0] |= roles
                slot[1] += _dec(e.get("credit")) - _dec(e.get("debit"))
    return held


def _total(held: dict) -> Decimal:
    """The sale's gain (positive) or loss (negative) across the accounts holding it."""
    return sum((amount for _, amount in held.values()), Decimal(0))


def _homes(accounts: dict, held: dict) -> dict:
    """``accounts`` with the gain and loss kept where the sale already holds them; today's
    account (asked for only when needed) where it holds none."""
    def home(role, key):
        found = next((a for a, (roles, _) in held.items() if role in roles), None)
        return (lambda: found) if found else accounts[key]

    return {**accounts, "gain": home(_GAIN, "gain"), "loss": home(_LOSS, "loss")}


def _restate(accounts: dict, held: dict, result: Decimal) -> tuple[list, Decimal]:
    """Lines taking the gain and loss ``held`` to ``result`` (a gain on the gain account, a
    loss on the loss account), and the credit they add, which the caller balances."""
    from celerp.services.module_journals import JournalLine

    homes = _homes(accounts, held)
    target = {}
    if result:
        role = _GAIN if result > 0 else _LOSS
        target[homes["gain" if result > 0 else "loss"]()] = (role, result)
    lines, added = [], Decimal(0)
    for account in dict.fromkeys([*held, *target]):
        roles, amount = held.get(account, (set(), Decimal(0)))
        role, wanted = target.get(account, (None, Decimal(0)))
        change = wanted - amount
        if not change:
            continue
        role = role or (_GAIN if _GAIN in roles and (amount > 0 or _LOSS not in roles) else _LOSS)
        lines.append(JournalLine(account=account, credit=change, role=role) if change > 0
                     else JournalLine(account=account, debit=-change, role=role))
        added += change
    return lines, added


def _through(clearing: str, credit: Decimal) -> list:
    """The clearing line balancing ``credit`` added elsewhere."""
    from celerp.services.module_journals import JournalLine

    if not credit:
        return []
    return [JournalLine(account=clearing, debit=credit, role=_CLEARING) if credit > 0
            else JournalLine(account=clearing, credit=-credit, role=_CLEARING)]


async def _locked(session, company_id, asset_ids: list[str]) -> dict:
    from celerp.services.company_lock import lock_projections

    return await lock_projections(session, company_id, asset_ids)


async def _asset(session, company_id, asset_id: str) -> dict:
    row = (await _locked(session, company_id, [asset_id])).get(asset_id)
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    return row.state


async def _set(session, company_id, user_id, asset_id: str, status: str, **fields) -> None:
    await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.status", {"status": status, **fields})


async def refuse_waiting(session, company_id, asset_id: str, asset: dict) -> None:
    """Refuse (409) any other change to an asset waiting on its sale invoice."""
    sale = asset.get("sale") or {}
    if sale.get("state") != "pending":
        return
    invoice = await _state(session, company_id, sale["invoice_id"])
    number = doc_number(invoice, sale["invoice_id"])
    if invoice is not None and invoice.get("status") == "draft" and _carries(invoice, asset_id):
        raise conflict(f"{asset['number']} is on draft invoice {number}. Finalize or delete that invoice, "
                       "or cancel the sale from the asset's page.")
    if invoice is not None and invoice.get("status") == "void":
        raise conflict(f"{asset['number']} is waiting on void invoice {number}. Reissue or cancel the sale "
                       "from the asset's page.")
    raise conflict(f"{asset['number']} is waiting on a sale whose draft invoice was deleted. Reissue or cancel "
                   "the sale from the asset's page.")


async def _plan(session, company_id, books: runs.Books, asset_id: str, asset: dict, day: str,
                proceeds: Decimal, clearing: str | None = None, held: dict | None = None) -> dict:
    """What selling the whole asset on ``day`` for ``proceeds`` posts and moves: proceeds
    in from ``clearing`` (today's clearing account when not given), the result onto the
    accounts the sale already holds its gain and loss on (``held``)."""
    cls = await setup.get_class(session, company_id, asset.get("class_id"))
    accounts = _homes(disposal.accounts(books), held or {})
    return disposal._plan_asset(books, asset_id, asset, cls, await asset_movements(session, company_id, asset_id),
                                day, Decimal(1), _dec(asset.get("quantity")), accounts, proceeds=proceeds,
                                clearing=(clearing or accounts["clearing"]()) if proceeds else None,
                                clearing_role=_CLEARING)


# ── Drafting the documents ───────────────────────────────────────────────────


async def _draft(session, company_id, user_id, data: dict, before=None) -> str:
    """Create a draft invoice or credit note from ``data``, its money taken from its lines.
    ``before(doc_id)`` runs ahead of the check of its asset lines (at emit_event)."""
    from celerp.events.engine import emit_event
    from celerp.services.company_lock import locked_company
    from celerp_docs.doc_money import money_from_lines
    from celerp_docs.sequences import next_draft_ref

    company = await locked_company(session, company_id)
    data = money_from_lines(data, data["currency"])
    ref = next_draft_ref(company, data["doc_type"])
    doc_id = f"doc:{ref}"
    data.update(ref_id=ref, status="draft", amount_paid=0.0, amount_outstanding=data["total"])
    if before is not None:
        await before(doc_id)
    await emit_event(session, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
                     data=data, actor_id=user_id, location_id=None, source="fixed_assets",
                     idempotency_key=f"fa:doc:{doc_id}")
    return doc_id


async def _draft_invoice(session, company_id, user_id, asset_id: str, asset: dict, *, day: str, line: dict,
                         header: dict, sale: dict) -> str:
    """A new draft invoice for the asset, which waits on it."""
    data = {"doc_type": "invoice", "issue_date": day, "currency": header.pop("currency", None)
            or (await runs._books(session, company_id)).currency, **header,
            "line_items": [{**line, "line_id": uuid.uuid4().hex, "quantity": 1, "asset_id": asset_id}]}

    async def wait(doc_id):
        await _set(session, company_id, user_id, asset_id, asset["status"],
                   sale={**sale, "state": "pending", "invoice_id": doc_id, "date": day, "credits": {}})

    return await _draft(session, company_id, user_id, data, before=wait)


async def _draft_credit_notes(session, company_id, invoice_id: str, asset_id: str) -> list[dict]:
    """The draft credit notes on the sale invoice that carry ``asset_id``, oldest first."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string() == "credit_note",
        Projection.state["original_doc_id"].as_string() == invoice_id,
        Projection.state["status"].as_string() == "draft").order_by(Projection.entity_id))).scalars().all()
    return [{"id": r.entity_id, "number": doc_number(r.state, r.entity_id), "date": r.state.get("issue_date")}
            for r in rows if _carries(r.state, asset_id) is not None]


def _pending(asset: dict) -> dict:
    sale = asset.get("sale") or {}
    if sale.get("state") != "pending":
        raise conflict(f"{asset['number']} is not waiting on a sale invoice.")
    return sale


async def _buyer(session, company_id, contact_id) -> dict:
    """The contact the asset is sold to, as the invoice names it."""
    row = (await session.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "contact",
        Projection.entity_id == str(contact_id or "")))).scalar_one_or_none()
    if row is None or row.get("merged_into"):
        raise setup.unprocessable("Choose the buyer from your contacts.")
    return {"contact_id": str(contact_id), "contact_name": row.get("name") or ""}


async def sell(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Sell an asset: a draft invoice for it, which completes the sale when it is finalized."""

    async def plan(fa):
        asset = await _asset(session, company_id, asset_id)
        in_use(asset_id, asset, "it cannot be sold")
        day = _date(body.get("date"), "The sale date")
        books = await runs._books(session, company_id)
        currency = books.currency
        price = _money_in(body.get("price"), "The price", currency, positive=True)
        buyer = await _buyer(session, company_id, body.get("contact_id"))
        await refuse_waiting(session, company_id, asset_id, asset)
        item = await _plan(session, company_id, books, asset_id, asset, day, price)
        shown = {"number": asset["number"], "name": asset["name"], "date": day, "price": money(price, currency),
                 "contact_name": buyer["contact_name"],
                 **{k: item["shown"][k] for k in disposal.LEAVING},
                 "journals": [_journal_view(item["lines"], currency)]}
        basis = [asset.get("version"), asset.get("movement_count"), await runs._ledger_basis(session, company_id, books)]
        return shown, {"asset": asset, "day": day, "price": price, "buyer": buyer, "currency": currency,
                       "basis": basis}

    async def apply(context, op_key):
        asset = context["asset"]
        invoice_id = await _draft_invoice(
            session, company_id, user_id, asset_id, asset, day=context["day"],
            line={"name": f"{asset['number']} {asset['name']}", "unit_price": float(context["price"])},
            header=dict(context["buyer"]), sale={"price": money(context["price"], context["currency"]), **context["buyer"]})
        return {"invoice_id": invoice_id, "assets": [{"id": asset_id, "number": asset["number"]}]}, _effects()

    return await _run(session, company_id, user_id, body, op_type="sale", keys=_WRITE_KEYS, plan=plan, apply=apply)


async def reissue(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """A new draft invoice for a sale whose draft was deleted or whose invoice is void."""

    async def plan(fa):
        asset = await _asset(session, company_id, asset_id)
        sale = _pending(asset)
        invoice = await _state(session, company_id, sale["invoice_id"])
        if invoice is not None and invoice.get("status") == "draft" and _carries(invoice, asset_id):
            raise conflict(f"{asset['number']} is already on draft invoice {doc_number(invoice, sale['invoice_id'])}.")
        day = _date(body.get("date") or sale["date"], "The sale date")
        shown = {"number": asset["number"], "date": day, "price": sale.get("price"),
                 "contact_name": (invoice or {}).get("contact_name") or sale.get("contact_name")}
        return shown, {"asset": asset, "sale": sale, "invoice": invoice, "day": day,
                       "basis": [asset.get("version"), asset.get("movement_count")]}

    async def apply(context, op_key):
        asset, sale, invoice = context["asset"], context["sale"], context["invoice"] or {}
        old = _carries(invoice, asset_id)
        line = ({k: v for k, v in old.items() if k not in ("line_id", "line_total", "asset_id")} if old else
                {"name": f"{asset['number']} {asset['name']}", "unit_price": float(_dec(sale.get("price")))})
        header = {k: invoice[k] for k in ("currency", "conversion_rate", "contact_id", "contact_name")
                  if invoice.get(k) not in (None, "")}
        header = {**{k: sale[k] for k in ("contact_id", "contact_name") if sale.get(k)}, **header}
        invoice_id = await _draft_invoice(session, company_id, user_id, asset_id, asset, day=context["day"],
                                          line=line, header=header, sale=sale)
        return {"invoice_id": invoice_id, "assets": [{"id": asset_id, "number": asset["number"]}]}, _effects()

    return await _run(session, company_id, user_id, body, op_type="sale_reissue", keys=_WRITE_KEYS, plan=plan,
                      apply=apply)


async def cancel(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Stop selling an asset waiting on its sale invoice; a draft still carrying it is voided."""
    from celerp.events.engine import emit_event

    async def plan(fa):
        asset = await _asset(session, company_id, asset_id)
        sale = _pending(asset)
        invoice = await _state(session, company_id, sale["invoice_id"])
        draft = invoice is not None and invoice.get("status") == "draft" and _carries(invoice, asset_id) is not None
        shown = {"number": asset["number"], "voids": doc_number(invoice, sale["invoice_id"]) if draft else None}
        return shown, {"asset": asset, "sale": sale, "draft": draft,
                       "basis": [asset.get("version"), asset.get("movement_count")]}

    async def apply(context, op_key):
        asset, sale = context["asset"], context["sale"]
        await _set(session, company_id, user_id, asset_id, asset["status"], sale=None)
        if context["draft"]:
            await emit_event(session, company_id=company_id, entity_id=sale["invoice_id"], entity_type="doc",
                             event_type="doc.voided", data={"reason": "Sale cancelled", "pre_void_status": "draft"},
                             actor_id=user_id, location_id=None, source="fixed_assets",
                             idempotency_key=f"fa:sale-cancel:{op_key}")
        return {"assets": [{"id": asset_id, "number": asset["number"]}]}, _effects()

    return await _run(session, company_id, user_id, body, op_type="sale_cancel", keys=_WRITE_KEYS, plan=plan,
                      apply=apply)


async def credit(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """A draft credit note on the sale invoice: part of the price (``adjust``) or the
    asset back (``returned``, at what is left of the sale)."""
    from celerp_docs.routes import credit_note_currency

    async def plan(fa):
        kind = body.get("kind")
        if kind not in ("adjust", "returned"):
            raise setup.unprocessable("Choose whether the credit note credits part of the price or takes the asset "
                                      "back.")
        day = _date(body.get("date"), "The credit note date")
        asset = await _asset(session, company_id, asset_id)
        sale = asset.get("sale") or {}
        if sale.get("state") != "sold":
            raise conflict(f"{asset['number']} has no completed sale to credit.")
        invoice = await _state(session, company_id, sale["invoice_id"]) or {}
        source = _carries(invoice, asset_id)
        if source is None:
            raise conflict(f"Invoice {doc_number(invoice, sale['invoice_id'])} no longer carries {asset['number']}.")
        # A credit note already drafted for the sale is opened, never drafted twice.
        drafts = await _draft_credit_notes(session, company_id, sale["invoice_id"], asset_id)
        if drafts:
            return ({"number": asset["number"], "open_draft": drafts[0]["number"]},
                    {"open_draft": drafts[0]["id"], "basis": [asset.get("version"), drafts[0]["id"]]})
        remaining = _dec(sale["proceeds"]) - _credited(sale)
        rate = _dec(invoice.get("conversion_rate") or 1)
        base = await setup.currency(session, company_id)
        in_doc = str(invoice.get("currency") or base).upper()
        if kind == "adjust":
            amount = _money_in(body.get("amount"), "The amount", in_doc, positive=True)
            if amount * rate > remaining:
                raise conflict(f"Only {said(remaining, base)} of the sale of {asset['number']} is left to credit.")
        else:
            amount = round_money(remaining / rate, in_doc)
        shown = {"number": asset["number"], "kind": kind, "date": day, "amount": money(amount, in_doc),
                 "invoice": doc_number(invoice, sale["invoice_id"]), "remaining": money(remaining, base),
                 "currency": in_doc}
        return shown, {"invoice": invoice, "sale": sale, "source": source, "kind": kind, "day": day, "amount": amount,
                       "basis": [asset.get("version"), asset.get("movement_count"), sale.get("credits")]}

    async def apply(context, op_key):
        if context.get("open_draft"):
            return {"credit_note_id": context["open_draft"]}, _effects()
        invoice, sale, source = context["invoice"], context["sale"], context["source"]
        line = {"name": source.get("name"), "quantity": 1, "unit_price": float(context["amount"]),
                "asset_id": asset_id, "source_line_id": source["line_id"], "line_id": uuid.uuid4().hex,
                **{k: source[k] for k in _TAX_FIELDS if source.get(k) is not None}}
        if context["kind"] == "returned":
            line["asset_returned"] = True
        base = (await runs._books(session, company_id)).currency
        data = {"doc_type": "credit_note", "original_doc_id": sale["invoice_id"], "issue_date": context["day"],
                **{k: invoice[k] for k in ("contact_id", "contact_name") if invoice.get(k)},
                **credit_note_currency(invoice, base, None, None), "line_items": [line]}
        data.setdefault("currency", base)
        credit_note_id = await _draft(session, company_id, user_id, data)
        return {"credit_note_id": credit_note_id}, _effects()

    return await _run(session, company_id, user_id, body, op_type="sale_credit", keys=_WRITE_KEYS, plan=plan,
                      apply=apply)


# ── What the asset page shows ────────────────────────────────────────────────


async def view(session, company_id, asset_id: str) -> dict:
    """Where the asset's sale stands, with what completing it posts (pending) or posted."""
    from celerp.services.auto_je import asset_line_proceeds

    row = await session.get(Projection, {"company_id": company_id, "entity_id": asset_id})
    if row is None or row.entity_type != "fa_asset":
        raise HTTPException(status_code=404, detail="No such asset.")
    asset = row.state
    sale = asset.get("sale") or {}
    if not sale:
        return {"state": None}
    invoice = await _state(session, company_id, sale["invoice_id"])
    credits = []
    for cn_id, c in (sale.get("credits") or {}).items():
        credits.append({"id": cn_id, "number": doc_number(await _state(session, company_id, cn_id), cn_id),
                        "kind": c["kind"], "amount": c["amount"], "live": c["live"], "date": c.get("date")})
    out = {"state": sale["state"], "invoice_id": sale["invoice_id"],
           "invoice_number": doc_number(invoice, sale["invoice_id"]), "date": sale.get("date"),
           "contact_name": sale.get("contact_name"), "credit_notes": credits, "draft_deleted": False,
           "problem": None, "journal": {"lines": []}}
    if sale["state"] == "pending":
        books = await runs._books(session, company_id)
        line = _carries(invoice, asset_id)
        proceeds = _dec(sale.get("price"))
        if line is not None:
            proceeds = asset_line_proceeds(invoice, books.currency, asset_id)
        out["invoice_void"] = (invoice or {}).get("status") == "void"
        out["draft_deleted"] = line is None and not out["invoice_void"]
        out["actions"] = ["cancel"] + (["reissue"] if line is None or invoice.get("status") == "void" else [])
        currency = books.currency
        out.update(proceeds=money(proceeds, currency), credited=money(0, currency),
                   remaining=money(proceeds, currency))
        try:
            item = await _plan(session, company_id, books, asset_id, asset, (invoice or {}).get("issue_date")
                               or sale["date"], proceeds)
        except HTTPException as exc:
            out.update(carrying_amount=None, gain_or_loss=None, problem=str(exc.detail))
        else:
            out.update(carrying_amount=item["shown"]["carrying_amount"], gain_or_loss=item["shown"]["gain_or_loss"],
                       journal=_journal_view(item["lines"], currency))
        await label_lines(session, company_id, out)
        return out
    currency = await setup.currency(session, company_id)
    if sale["state"] == "returned":
        # A return credits what was left, and the sale's result went back with the asset.
        out.update(proceeds=money(_dec(sale["proceeds"]), currency), credited=money(_dec(sale["proceeds"]), currency),
                   remaining=money(0, currency), carrying_amount=None, gain_or_loss=None, actions=[],
                   draft_credit_notes=[])
        return out
    credited = _credited(sale)
    drafts = await _draft_credit_notes(session, company_id, sale["invoice_id"], asset_id)
    je = await _state(session, company_id, sale["journal_id"]) if sale.get("journal_id") else None
    out.update(proceeds=money(_dec(sale["proceeds"]), currency), credited=money(credited, currency),
               remaining=money(_dec(sale["proceeds"]) - credited, currency),
               carrying_amount=sale.get("carrying_amount"),
               gain_or_loss=(asset.get("disposal") or {}).get("gain_or_loss"),
               draft_credit_notes=drafts, actions=["credit"] if sale["state"] == "sold" and not drafts else [],
               journal={"lines": [{"account": e["account"], "debit": money(e.get("debit"), currency),
                                   "credit": money(e.get("credit"), currency)}
                                  for e in (je or {}).get("entries") or []]})
    await label_lines(session, company_id, out)
    return out


# ── The documents' hook ──────────────────────────────────────────────────────


async def _dispose(session, company_id, user_id, asset_id: str, asset: dict, sale: dict, *, day: str,
                   proceeds: Decimal, clearing: str | None, op_key: str, sale_proceeds: Decimal | None = None) -> None:
    """Complete the sale: dispose of the asset on ``day``, bringing ``proceeds`` in from
    ``clearing``, the account the document posted them to."""
    books = await runs._books(session, company_id)
    held = await _held(session, company_id, books.settings, sale)
    item = await _plan(session, company_id, books, asset_id, asset, day, proceeds, clearing, held)
    short = asset_id.removeprefix("fa:asset:")
    invoice = doc_number(await _state(session, company_id, sale["invoice_id"]), sale["invoice_id"])
    je_id = await _post(session, company_id, user_id, key=f"asset:{short}:disposal:{int(asset.get('movement_count') or 0) + 1}",
                        operation="sale", day=day, memo=f"{asset['number']} {asset['name']} sold on invoice {invoice}",
                        lines=await _continued(session, company_id, item["lines"]), source_id=asset_id,
                        href=_href(asset_id), label=asset["number"], op_key=op_key)
    currency = books.currency
    gain = money(_total(held) + item["gain"], currency)
    caught = await catch_up(session, company_id, user_id, asset_id, day=day, periods=item["periods"], op_key=op_key,
                            journal_id=je_id)
    movement_id = await _move(session, company_id, user_id, asset_id, kind="posted", operation="disposal",
                              effective=day, posting=day, amounts=item["amounts"], op_key=op_key,
                              quantity=str(-item["quantity"]), **caught, share="1", reason="sold",
                              note=None, journal_id=je_id, gain_or_loss=gain, invoice_id=sale["invoice_id"])
    await _set(session, company_id, user_id, asset_id, "sold", exception=None, disposal={
        "date": day, "reason": "sold", "note": None, "op_key": op_key, "movement_id": movement_id,
        "journal_id": je_id, "gain_or_loss": gain, "invoice_id": sale["invoice_id"]}, sale={
        **sale, "state": "sold", "date": day, "movement_id": movement_id, "journal_id": je_id,
        # A voided return brings the asset back through the return's clearing; the sale
        # still settles through its invoice's.
        "journals": [*(sale.get("journals") or ()), je_id],
        "clearing": clearing if sale_proceeds is None else sale.get("clearing"),
        "proceeds": money(proceeds if sale_proceeds is None else sale_proceeds, currency),
        "carrying_amount": item["shown"]["carrying_amount"]})


async def _reverse(session, company_id, user_id, key_journal: str, operation: str, reason: str) -> None:
    from celerp.services.module_journals import reverse_journal

    await reverse_journal(session, company_id=company_id, user_id=user_id, module=MODULE,
                          key=key_journal.removeprefix(f"je:mod:{MODULE}:"), operation=operation, reason=reason)


async def _unsell(session, company_id, user_id, asset_id: str, asset: dict, sale: dict, doc_id: str, doc: dict,
                  op_key: str) -> None:
    """The sale invoice is void or a draft again: reverse the disposal; the asset waits on it."""
    from celerp.notifications.service import create_keyed

    live = next((cn for cn, c in (sale.get("credits") or {}).items() if c.get("live")), None)
    if live is not None:
        number = doc_number(await _state(session, company_id, live), live)
        raise conflict(f"{asset['number']} has credit note {number} on this invoice. Void that credit note first.")
    books = await runs._books(session, company_id)
    await _reverse(session, company_id, user_id, sale["journal_id"], "sale_reverse", "Sale invoice reversed")
    mv = await _state(session, company_id, sale["movement_id"])
    await reverse_move(session, company_id, user_id, sale["movement_id"], mv, op_key, "sale_reverse")
    # A return or credit voided since leaves the gain and loss other journals hold; they
    # settle back to nothing on the accounts holding them, through the sale's clearing.
    accounts = disposal.accounts(books)
    settle, added = _restate(accounts, await _held(session, company_id, books.settings, sale), Decimal(0))
    journals = list(sale.get("journals") or ())
    if settle:
        short = asset_id.removeprefix("fa:asset:")
        lines = settle + _through(sale.get("clearing") or accounts["clearing"](), added)
        journals.append(await _post(session, company_id, user_id, key=f"asset:{short}:settle:{uuid.uuid4().hex}",
                                    operation="sale_reverse", day=mv["effective_date"],
                                    memo=f"{asset['number']} sale reversed: clearing settled",
                                    lines=await _continued(session, company_id, lines), source_id=asset_id,
                                    href=_href(asset_id), label=asset["number"], op_key=op_key))
    await _set(session, company_id, user_id, asset_id, "active", disposal=None,
               sale={**sale, "state": "pending", "journal_id": None, "movement_id": None, "journals": journals})
    await create_keyed(session, company_id, "fixed_assets", "fixed_assets.notice.sale_reversed",
                       {"number": asset["number"], "invoice": doc_number(doc, doc_id)}, action_url=_href(asset_id))


async def _credit_posted(session, company_id, user_id, asset_id: str, asset: dict, sale: dict, doc_id: str,
                         doc: dict, line: dict, op_key: str) -> None:
    """A credit note on the sale is issued or restored."""
    if (sale.get("credits") or {}).get(doc_id, {}).get("live"):
        return
    books = await runs._books(session, company_id)
    accounts = disposal.accounts(books)
    number = asset["number"]
    if sale.get("state") != "sold":
        raise conflict(f"{number} was already returned with a credit note on this invoice.")
    clearing, amount = await _cleared(session, company_id, books.settings, doc_id, asset_id, number)
    held = await _held(session, company_id, books.settings, sale)
    gain = _total(held)
    remaining = _dec(sale["proceeds"]) - _credited(sale)
    day = doc.get("issue_date") or sale["date"]
    short = asset_id.removeprefix("fa:asset:")
    if not line.get("asset_returned"):
        if amount > remaining:
            raise conflict(f"Only {said(remaining, books.currency)} of the sale of {number} is left to credit.")
        restated, added = _restate(accounts, held, gain - amount)
        je_id = await _post(session, company_id, user_id, key=f"asset:{short}:credit:{uuid.uuid4().hex}",
                            operation="sale_adjust", day=day, memo=f"{number} sale credited: {doc_number(doc, doc_id)}",
                            lines=await _continued(session, company_id, restated + _through(clearing, added)),
                            source_id=asset_id, href=_href(asset_id), label=number, op_key=op_key)
        credits = {**(sale.get("credits") or {}), doc_id: {
            "kind": "adjust", "amount": money(amount, books.currency), "journal_id": je_id, "live": True, "date": day}}
        await _set(session, company_id, user_id, asset_id, asset["status"],
                   disposal={**(asset.get("disposal") or {}), "gain_or_loss": money(gain - amount, books.currency)},
                   sale={**sale, "credits": credits, "journals": [*sale["journals"], je_id]})
        return
    # A foreign credit note converts to the base currency within one of its smallest units.
    foreign = str(doc.get("currency") or "").upper() not in ("", books.currency)
    if abs(amount - remaining) > (Decimal(1).scaleb(-currency_dp(books.currency)) if foreign else 0):
        raise conflict(f"A return credits what is left of the sale of {number}: "
                       f"{said(remaining, books.currency)}.")
    mv = await _state(session, company_id, sale["movement_id"])
    cls = await setup.get_class(session, company_id, asset.get("class_id"))
    moved = {k: _dec((mv.get("amounts") or {}).get(k)) for k in ("cost", _AD, _IMP, _SURPLUS)}
    gone_cost, gone_ad, gone_imp = -moved["cost"], -moved[_AD], -moved[_IMP]
    from celerp.services.module_journals import JournalLine

    lines = [JournalLine(account=cls["asset_account"], debit=gone_cost),
             *disposal.accumulated_lines(cls["accumulated_depreciation_account"], gone_ad, gone_imp, "credit")]
    # The surplus the sale released returns to the accounts the sale moved it between.
    sold = await _state(session, company_id, sale["journal_id"]) or {}
    for e in sold.get("entries") or []:
        role = next(iter(set(line_roles(books.settings, e)) & _CARRIED_ROLES), None)
        if role:
            lines.append(JournalLine(account=e["account"], debit=_dec(e.get("credit")), credit=_dec(e.get("debit")),
                                     role=role))
    if amount:
        lines.append(JournalLine(account=clearing, credit=amount, role=_CLEARING))
    restated, _ = _restate(accounts, held, gain + gone_cost - gone_ad - gone_imp - amount)
    lines += restated
    je_id = await _post(session, company_id, user_id, key=f"asset:{short}:credit:{uuid.uuid4().hex}",
                        operation="sale_return", day=day, memo=f"{number} returned: {doc_number(doc, doc_id)}",
                        lines=await _continued(session, company_id, lines), source_id=asset_id, href=_href(asset_id),
                        label=number, op_key=op_key)
    await _move(session, company_id, user_id, asset_id, kind="reversal", operation="sale_return",
                effective=mv["effective_date"], posting=day, amounts={k: -v for k, v in moved.items()},
                op_key=op_key, quantity=str(-_dec(mv.get("quantity"))), reverses=sale["movement_id"])
    credits = {**(sale.get("credits") or {}), doc_id: {"kind": "returned", "amount": money(amount, books.currency),
                                                       "journal_id": je_id, "live": True, "date": day,
                                                       "clearing": clearing}}
    await _set(session, company_id, user_id, asset_id, "active", disposal=None,
               sale={**sale, "state": "returned", "journal_id": None, "movement_id": None, "credits": credits,
                     "journals": [*sale["journals"], je_id]})


async def _credit_reversed(session, company_id, user_id, asset_id: str, asset: dict, sale: dict, doc_id: str,
                           op_key: str) -> None:
    """A credit note on the sale is voided or back to draft: undo what it did."""
    entry = (sale.get("credits") or {}).get(doc_id)
    if not entry or not entry.get("live"):
        return
    credits = {**sale["credits"], doc_id: {**entry, "live": False}}
    if entry["kind"] == "returned":
        await _dispose(session, company_id, user_id, asset_id, asset, {**sale, "credits": credits},
                       day=sale["date"], proceeds=_dec(entry["amount"]), clearing=entry.get("clearing"),
                       op_key=op_key, sale_proceeds=_dec(sale["proceeds"]))
        return
    if sale.get("state") == "returned":
        returned = next(cn for cn, c in sale["credits"].items() if c.get("live") and c["kind"] == "returned")
        raise conflict(f"{asset['number']} was returned with credit note "
                       f"{doc_number(await _state(session, company_id, returned), returned)}. Void that credit "
                       "note first.")
    books = await runs._books(session, company_id)
    await _reverse(session, company_id, user_id, entry["journal_id"], "sale_adjust", "Sale credit note reversed")
    held = await _held(session, company_id, books.settings, sale)
    gain = _total(held)
    # Credits voided out of order leave the split the later ones made; it is restated to the result.
    restated, _ = _restate(disposal.accounts(books), held, gain)
    journals = list(sale["journals"])
    if restated:
        short = asset_id.removeprefix("fa:asset:")
        journals.append(await _post(session, company_id, user_id, key=f"asset:{short}:restate:{uuid.uuid4().hex}",
                                    operation="sale_adjust", day=entry.get("date") or sale["date"],
                                    memo=f"{asset['number']} sale credit reversed: gain and loss restated",
                                    lines=await _continued(session, company_id, restated), source_id=asset_id,
                                    href=_href(asset_id), label=asset["number"], op_key=op_key))
    await _set(session, company_id, user_id, asset_id, asset["status"],
               disposal={**(asset.get("disposal") or {}), "gain_or_loss": money(gain, books.currency)},
               sale={**sale, "credits": credits, "journals": journals})


async def _authorize(session, company_id, user_id, change: str, numbers: str) -> dict:
    """The caller's authority over the document change, refused naming the assets."""
    from celerp.services.permissions import locked_authority

    if user_id is None:
        raise conflict(f"{numbers}: a document selling or crediting an asset is changed by a person.")
    try:
        _, settings = await locked_authority(session, company_id, user_id,
                                             _WRITE_KEYS if change == "write" else SALE_KEYS)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        raise HTTPException(status_code=403, detail=f"{numbers}: {exc.detail}") from exc
    return settings


async def on_asset_lines(*, session, company_id, user_id, doc_id: str, doc: dict, change: str) -> None:
    """The doc_asset_lines slot: follow a document's asset lines through its ``change``
    (write, unlink, finalize, credit, void, revert, unvoid). A refusal refuses the document action."""
    from celerp.modules.registry import uses_module
    from celerp.services.doc_asset_lines import asset_lines
    from celerp.services.line_measures import line_label

    lines = asset_lines(doc)
    if change == "unlink":
        # A draft invoice an asset is waiting on keeps its line, so the sale never waits on
        # a document that no longer sells it. Cancelling the sale voids the draft.
        for li in lines if doc.get("doc_type") == "invoice" else ():
            asset = await _state(session, company_id, li["asset_id"]) or {}
            sale = asset.get("sale") or {}
            if sale.get("state") == "pending" and sale.get("invoice_id") == doc_id:
                raise conflict(f"{asset['number']} is waiting on this invoice. Keep its line, or cancel the sale "
                               "from the asset's page, which voids this draft.")
        return
    rows = await _locked(session, company_id, [li["asset_id"] for li in lines])
    for li in lines:
        row = rows.get(li["asset_id"])
        if row is None or row.entity_type != "fa_asset":
            raise setup.unprocessable(f"{line_label(li)}: no such asset.")
    numbers = ", ".join(rows[li["asset_id"]].state["number"] for li in lines)
    settings = await _authorize(session, company_id, user_id, change, numbers)
    invoice = doc.get("doc_type") == "invoice"
    if invoice and change in ("write", "finalize") and not uses_module(settings, MODULE):
        raise conflict(f"{numbers}: turn on Fixed Assets to sell an asset.")
    op_key = f"{doc_id}:{change}"
    for li in lines:
        asset_id = li["asset_id"]
        asset = (await _state(session, company_id, asset_id))
        sale = asset.get("sale") or {}
        here = sale.get("invoice_id") == (doc_id if invoice else doc.get("original_doc_id"))
        number = asset["number"]
        if invoice and change in ("write", "finalize", "unvoid"):
            if sale.get("state") == "sold" and here and change != "write":
                continue
            if sale.get("state") == "pending" and here:
                if change != "write":
                    clearing, cleared = await _cleared(session, company_id, settings, doc_id, asset_id, number)
                    await _dispose(session, company_id, user_id, asset_id, asset, sale,
                                   day=doc.get("issue_date") or sale["date"], op_key=op_key, proceeds=-cleared,
                                   clearing=clearing)
                continue
            await refuse_waiting(session, company_id, asset_id, asset)
            raise conflict(f"{number} is not being sold on this invoice. Sell it from its page, which drafts its "
                           "invoice.")
        if invoice:
            if sale.get("state") == "sold" and here:
                await _unsell(session, company_id, user_id, asset_id, asset, sale, doc_id, doc, op_key)
            continue
        if change in _REVERSES:
            if here:
                await _credit_reversed(session, company_id, user_id, asset_id, asset, sale, doc_id, op_key)
            continue
        if not here or sale.get("state") not in ("sold", "returned"):
            original = str(doc.get("original_doc_id") or "")
            raise conflict(f"{number} was not sold on invoice "
                           f"{doc_number(await _state(session, company_id, original), original)}.")
        if change != "write":
            await _credit_posted(session, company_id, user_id, asset_id, asset, sale, doc_id, doc, li, op_key)
