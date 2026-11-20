# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A document's money derived from its lines and header inputs, in one place.

The header discount (flat or a percentage of the subtotal) reduces the taxable
base, and every tax, per line or on the whole document, is computed on that
reduced base. This is the same rule the document editor applies on screen, and
lists (quotations) follow it too (list_money).
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException

from celerp.services.doc_balance import MEMO_DRAW_STATUSES
from celerp.services.list_behavior import FINALIZED
from celerp.services.money import discount_from_inputs, document_line_amount, round_money, to_decimal, to_stored_float
from celerp.tax_regimes import line_tax_unset, tax_entries, tax_text, unreadable_tax
from celerp_docs.doc_constants import (DRAFT_REVERT_RETURNED, DRAFT_REVERT_STATUSES, INBOUND_DOC_TYPES,
                                      INBOUND_DRAFT_REVERT_STATUSES)
from celerp.localization.taxcodes import LineTaxSnapshot
from celerp_docs.taxes import compute_tax_amounts
from celerp.accounting_roles import catalog_refusal

# The header fields a document's money comes from besides its lines: the inputs
# document_money reads, the currency, and the rate that values it in the base currency.
# A document made from another (an agreement's bill) carries every one of them.
HEADER_MONEY_FIELDS = ("currency", "conversion_rate", "discount", "discount_type", "shipping", "tax", "tax_rate",
                       "doc_taxes", "shipping_taxes")


class UnratedTaxError(ValueError):
    """The document holds a tax amount with no rate to recompute it from."""


def _past_draft(state: dict | None) -> bool:
    """Whether a document *state* is past draft, where its taxes can no longer be changed."""
    return (state or {}).get("status") not in (None, "draft")


def draft_route(state: dict) -> list[str]:
    """The message keys, one sentence per step, of the way a document *state* past draft gets
    back to a draft, where its taxes can be changed, as Revert to Draft and Void allow it.
    Payments and credit notes go first, then received goods by Return Goods and lines out on
    memo or sold by Set as available. A purchase order that received goods never reverts, so
    it is voided and made again; a bill made from an order may only revert into the order,
    which is then voided. A status Revert to Draft never takes leaves only a new document; a
    list reverts only from finalized."""
    doc_type, status = state.get("doc_type", ""), state.get("status")
    if state.get("entity_type") == "list":
        return ["documents.tax_route_revert" if status == FINALIZED else "documents.tax_route_new"]
    inbound = doc_type in INBOUND_DOC_TYPES
    paid = bool(to_decimal(state.get("amount_paid") or 0)) or to_decimal(state.get("credited") or 0) > 0
    received = bool(state.get("received_items"))
    steps = (["documents.tax_route_payments"] if paid else []) + (["documents.tax_route_return"] if received else [])
    if any(isinstance(fi, dict) and fi.get("item_id") is not None for fi in state.get("fulfilled_items") or []):
        steps.append("documents.tax_route_available")
    revertible = (INBOUND_DRAFT_REVERT_STATUSES | (DRAFT_REVERT_RETURNED if received else frozenset())
                  if inbound else DRAFT_REVERT_STATUSES) | ({"paid", "partial"} if paid else frozenset())
    if received and doc_type == "purchase_order":
        last = "documents.tax_route_void"
    elif received and doc_type == "bill" and state.get("source_po_ref"):
        last = "documents.tax_route_void_order"
    elif status in revertible:
        last = "documents.tax_route_revert"
    else:
        last = "documents.tax_route_new"
    return steps + [last]


def _issued_detail(key: str, state: dict, /, **values) -> dict:
    """A past-draft refusal *key* ending with the way back to a draft (draft_route)."""
    return catalog_refusal(key, route=[catalog_refusal(step) for step in draft_route(state)], **values)


def unreadable_tax_refusal(tax: str, state: dict | None = None) -> HTTPException:
    """The 422 refusing a tax it cannot read, named. Only a draft's taxes can be changed, so on a
    document *state* past draft the message names the way back to one (draft_route)."""
    if _past_draft(state):
        return HTTPException(status_code=422, detail=_issued_detail("documents.err_tax_unreadable_issued", state, tax=tax))
    return HTTPException(status_code=422, detail=catalog_refusal("documents.err_tax_unreadable", tax=tax))


def refuse_unset_line_tax(lines, state: dict | None = None, numbers: list[int] | None = None, *,
                          receiving: bool = False) -> None:
    """Refuse (422, naming the line) issuing *lines*, or *receiving* goods on them, while one's tax
    is still for the user to set (line_tax_unset): it would be charged none, and a receipt takes a
    draft past the point where the tax can be chosen. Each line is numbered by its position, or by
    *numbers* (counted from 0) when they are a selection of the document's lines. On a document
    *state* past draft the message names the way back to one, as unreadable_tax_refusal does."""
    for i, line in enumerate(lines or []):
        if isinstance(line, dict) and line_tax_unset(line):
            number = (numbers[i] if numbers else i) + 1
            if _past_draft(state):
                detail = _issued_detail("documents.err_line_tax_unset_issued", state, line=number)
            else:
                detail = catalog_refusal("documents.err_line_tax_unset_receive" if receiving else "documents.err_line_tax_unset",
                                         line=number)
            raise HTTPException(status_code=422, detail=detail)


def _definition(entry) -> LineTaxSnapshot | None:
    """A stored tax entry as the tax definition its amount is computed from, or None when it is
    not one (not a tax object, or a rate that is not a number)."""
    if not isinstance(entry, dict):
        return None
    try:
        return LineTaxSnapshot.model_validate({**entry, "amount": 0.0})
    except ValueError:
        return None


# The snapshot fields a line tax copies from its code and version (line_tax); a write never sets them.
_FROM_CODE_FIELDS = ("code", "rate", "base_fraction", "category", "component", "report_tags", "version_from",
                     "withholding_point", "cash_basis", "account", "base")


def build_line_taxes(doc: dict, lines: list, stored_lines: list, settings: dict) -> None:
    """Build in place each tax on a draft's *lines* that names a ``code_id`` into the snapshot
    posting reads (LineTaxSnapshot), so a write never sets what a tax code decides. *doc* is the
    document as written (its type, issue date and prices_include_tax), *stored_lines* its lines
    before this write.

    A tax sent as its ``code_id`` alone is line_tax of that code on the document's day (tax_day)
    for the document's side. A tax sending any snapshot field keeps what the same line already
    stored for that code and version, so a code renamed or retagged since changes nothing on
    it; with nothing stored, it is built from the code. Only ``order``, ``is_compound`` and
    ``label`` are the writer's; the amount is computed from the line (document_money), and a
    sent one only has its sign checked. Refused (422): a code_id the
    document's tax list does not hold, a code with no rate in force on the day, and a sent
    snapshot field, ``inclusive`` or amount sign that differs from what the code gives."""
    from celerp.localization.taxcodes import NoRateInForce
    from celerp.tax_regimes import TAX_LIST_PAGES, doc_side, line_tax, side_taxes, tax_day, taxes_for

    doc_type = doc.get("doc_type")
    side = doc_side(doc_type)
    day = tax_day(doc, settings.get("timezone"))
    codes = {code.get("id"): code for code in taxes_for(side_taxes(settings, doc_type), doc_type)}
    stored = {line["line_id"]: line for line in stored_lines or [] if isinstance(line, dict) and line.get("line_id")}

    def _reference(line: dict, entry: dict, sent: dict) -> dict:
        if sent.keys() - {"code_id"}:
            kept = next((tax for tax in tax_entries((stored.get(line.get("line_id")) or {}).get("taxes"))
                         if tax.get("code_id") == entry["code_id"]
                         and str(tax.get("version_from")) == str(sent.get("version_from", tax.get("version_from")))), None)
            if kept is not None:
                return LineTaxSnapshot.model_validate(kept).model_dump(mode="json")
        code = codes.get(entry["code_id"])
        if code is None:
            raise unreadable_tax_refusal(str(entry["code_id"]))
        try:
            return line_tax(code, day, side)
        except NoRateInForce as gap:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "documents.err_no_rate_in_force", name=gap.code.name, day=day.isoformat(),
                url=TAX_LIST_PAGES[side])) from None

    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("taxes"), list):
            continue
        built = []
        for entry in line["taxes"]:
            if not (isinstance(entry, dict) and entry.get("code_id")):
                built.append(entry)
                continue
            sent = {key: value for key, value in entry.items() if value is not None}
            ref = _reference(line, entry, sent)
            try:
                merged = LineTaxSnapshot.model_validate({**ref, **sent}).model_dump(mode="json")
            except ValueError:
                raise unreadable_tax_refusal(tax_text(entry)) from None
            sign = merged["amount"] * ref["rate"]
            if (any(field in sent and merged[field] != ref[field] for field in _FROM_CODE_FIELDS)
                    or ("inclusive" in sent and merged["inclusive"] != bool(doc.get("prices_include_tax")))
                    or sign < 0):
                raise HTTPException(status_code=422, detail=catalog_refusal(
                    "documents.err_tax_from_code", name=ref["code"]))
            built.append({**ref, "amount": 0.0, **{key: merged[key] for key in ("order", "is_compound", "label")
                                                   if key in sent}})
        line["taxes"] = built


def main_supply_taxes(lines: list, currency: str | None) -> list[dict]:
    """The taxes shipping takes by default: each tax code of the main supply, the line with the
    largest amount (the first on a tie), as a ``code_id`` to build (build_line_taxes)."""
    priced = [line for line in lines if isinstance(line, dict)]
    if not priced:
        return []
    main = max(priced, key=lambda line: _line_amount(line, currency))
    return [{"code_id": tax["code_id"]} for tax in tax_entries(main.get("taxes")) if tax.get("code_id")]


def build_shipping_taxes(doc: dict, stored: dict | None, settings: dict) -> None:
    """Build in place a draft's ``shipping_taxes`` by the rule a line's taxes follow
    (build_line_taxes), against the shipping taxes the document held before this write
    (*stored*). Left out on a document with shipping, they are the main supply's
    (main_supply_taxes); an empty list leaves the shipping untaxed."""
    taxes = doc.get("shipping_taxes")
    if taxes is None and to_decimal(doc.get("shipping") or 0) > 0:
        taxes = main_supply_taxes(doc.get("line_items") or [], doc.get("currency"))
    if not isinstance(taxes, list):
        return
    charge = {"line_id": "shipping", "taxes": list(taxes)}
    build_line_taxes(doc, [charge], [{"line_id": "shipping", "taxes": (stored or {}).get("shipping_taxes")}],
                     settings)
    doc["shipping_taxes"] = charge["taxes"]


def computable_tax(entry) -> bool:
    """Whether a stored tax entry is a tax a total can be computed from."""
    return _definition(entry) is not None


def _recompute_tax_applications(entries: list, base, currency: str, inclusive: bool = False):
    """Recompute stored tax definitions against a new base, never stale amounts; with
    *inclusive*, *base* already holds them (compute_tax_amounts). The entries were checked first
    (refuse_malformed_taxes)."""
    resolved = compute_tax_amounts([_definition(value) for value in entries],
                                   to_stored_float(round_money(base, currency)), currency, inclusive=inclusive)
    return [item.model_dump() for item in resolved], sum(
        (to_decimal(item.amount) for item in resolved), to_decimal(0))


def line_tax_definition(line: dict) -> tuple[list[dict], Decimal]:
    """What a line's tax is charged from, by the one rule every reader of it follows: its
    ``taxes`` when they hold a tax a total can be computed from (computable_tax), else its
    ``tax_rate``, as an older version and a CSV import store a line's tax. The list editor saves
    the sum of the taxes' rates beside them, so that rate is never charged over them. Returns
    (taxes, 0) or ([], rate); ([], 0) is a line with no tax, and a 0% tax is a tax."""
    entries = [entry for entry in tax_entries(line.get("taxes")) if computable_tax(entry)]
    if entries:
        return entries, to_decimal(0)
    return [], to_decimal(line.get("tax_rate", 0) or 0)


def line_has_tax(line: dict) -> bool:
    """Whether a line has a tax of its own (line_tax_definition), so a header rate is not
    charged on it."""
    entries, rate = line_tax_definition(line)
    return bool(entries or rate)


def line_tax_rate(line: dict) -> Decimal:
    """The rate a line is taxed at, as its tax choice shows it: the sum of its taxes' rates
    (line_tax_definition), else its ``tax_rate``."""
    entries, rate = line_tax_definition(line)
    return sum((to_decimal(entry.get("rate") or 0) for entry in entries), rate)


def uncomputable_tax(values: dict) -> str | None:
    """The first entry of ``doc_taxes`` or a line's ``taxes`` in *values* that is not a tax a
    total can be computed from (computable_tax), as tax_text, or the whole value when it is not
    a list; None when every one is."""
    lines = values.get("line_items")
    held = [values.get("doc_taxes"), values.get("shipping_taxes")]
    if isinstance(lines, list):
        held += [line.get("taxes") for line in lines if isinstance(line, dict)]
    for value in held:
        named = unreadable_tax(value)
        if named is not None:
            return named
        named = next((tax_text(entry) for entry in value or [] if not computable_tax(entry)), None)
        if named is not None:
            return named
    return None


def refuse_malformed_taxes(values: dict, state: dict | None = None) -> None:
    """Refuse (422, naming the entry; unreadable_tax_refusal on the document *state*)
    ``doc_taxes`` or a line's ``taxes`` in *values* holding anything but a list of taxes a total
    can be computed from (uncomputable_tax). Every write of a document's or a list's taxes checks
    it, and so does every recalculation of a document's money (document_money), so a value an
    older version stored is never dropped from a total or charged at another rate in silence."""
    named = uncomputable_tax(values)
    if named is not None:
        raise unreadable_tax_refusal(named, state)


def _line_amount(line: dict, currency: str) -> Decimal:
    """A line's amount (document_line_amount: its own total, else quantity times its price less
    its discount_pct); a line with no price is 0."""
    return document_line_amount(line, currency) or to_decimal(0)


def _has_rate(state: dict) -> bool:
    """Whether a document's tax comes from a rate: a line's ``taxes`` or ``tax_rate``, a
    document tax, a shipping tax or a header ``tax_rate``."""
    if (tax_entries(state.get("doc_taxes")) or tax_entries(state.get("shipping_taxes"))
            or to_decimal(state.get("tax_rate", 0) or 0)):
        return True
    return any(isinstance(line, dict) and line_has_tax(line) for line in state.get("line_items") or [])


def fixed_discount(state: dict) -> Decimal:
    """A document's header discount when it is a fixed amount; 0 for a percentage."""
    if state.get("discount_type") == "percentage":
        return to_decimal(0)
    return max(to_decimal(0), to_decimal(state.get("discount", 0) or 0))


def unrated_tax(state: dict) -> Decimal:
    """A document's tax amount when nothing gives it a rate (_has_rate); otherwise 0."""
    return to_decimal(0) if _has_rate(state) else to_decimal(state.get("tax", 0) or 0)


def memo_draws(state: dict) -> list[dict]:
    """The takes that still count on a memo *state*, each with its lines not yet back on the memo.

    A take is what one draw put on an invoice (``invoiced_on``, written only by the memo's
    ``doc.lines_invoiced`` and ``doc.lines_returned`` events). It counts until a release names
    its draw, whatever happened to the invoice since; a released take's lines count for nothing.
    A memo converted before draws existed holds no takes: its invoice ``converted_to`` holds
    every fixed amount of the memo, as one take with no lines, until that invoice's void
    reopens the memo.
    """
    if state.get("status") == "converted" and state.get("converted_to"):
        return [{"draw_id": None, "invoice_id": state["converted_to"], "doc_number": None, "lines": [],
                 "shipping": float(state.get("shipping") or 0), "discount": to_stored_float(fixed_discount(state)),
                 "tax": to_stored_float(unrated_tax(state)), "released": False, "legacy": True}]
    return [{**take, "lines": [ln for ln in take["lines"] if not ln.get("returned")]}
            for take in state.get("invoiced_on") or [] if not take.get("released")]


def drawn_line_money(memo_line: dict, n, take_lines: list[dict], currency: str) -> Decimal:
    """The amount *n* units of a memo line bill when drawn onto an invoice.

    *take_lines* are the unreturned take lines of this memo line (memo_draws). The draw that
    brings them to the line's whole quantity, all taken at the line's present amount, takes the
    remainder, so the drawn amounts sum to the memo line's amount exactly; every other draw is
    its share of that amount, rounded once. Units set as available never change the quantity,
    so later draws stay proportional.
    """
    amount = _line_amount(memo_line, currency)
    quantity = to_decimal(memo_line.get("quantity", 0) or 0)
    n = to_decimal(n)
    taken = sum((to_decimal(ln["quantity"]) for ln in take_lines), to_decimal(0))
    if taken + n == quantity and all(to_decimal(ln["memo_line_amount"]) == amount for ln in take_lines):
        return amount - sum((to_decimal(ln["memo_amount"]) for ln in take_lines), to_decimal(0))
    if not quantity:
        return to_decimal(0)
    return round_money(amount * n / quantity, currency)


def memo_fixed_amounts(state: dict, out_by_line: dict[str, float]) -> dict:
    """What is left on a memo *state* of its fixed amounts, and each take that holds some.

    Shipping and a tax amount with no rate go whole with the first take and come back when it
    is released; a fixed discount is taken in parts. ``still_on_memo`` is what the units still
    out (*out_by_line*, by memo line id) would bill by drawn_line_money, plus the fixed amounts
    left, on a memo a draw can come from; None on any other.
    """
    currency = state.get("currency")
    takes = memo_draws(state)
    shipping = to_decimal(0) if any(t["shipping"] for t in takes) else to_decimal(state.get("shipping") or 0)
    discount = max(to_decimal(0), fixed_discount(state) - sum((to_decimal(t["discount"]) for t in takes),
                                                             to_decimal(0)))
    tax = to_decimal(0) if any(t["tax"] for t in takes) else unrated_tax(state)
    still = None
    if state.get("status") in MEMO_DRAW_STATUSES:
        goods = sum((drawn_line_money(line, out_by_line.get(line.get("line_id"), 0),
                                      [ln for t in takes for ln in t["lines"]
                                       if ln["memo_line_id"] == line.get("line_id")], currency)
                     for line in state.get("line_items") or [] if isinstance(line, dict)), to_decimal(0))
        still = to_stored_float(round_money(goods + shipping + tax - discount, currency))
    return {"shipping": to_stored_float(round_money(shipping, currency)),
            "discount": to_stored_float(round_money(discount, currency)),
            "tax": to_stored_float(round_money(tax, currency)),
            "takes": [{"invoice_id": t["invoice_id"], "doc_number": t["doc_number"], "shipping": t["shipping"],
                       "discount": t["discount"], "tax": t["tax"]} for t in takes],
            "still_on_memo": still}


def released_header_money(state: dict, released: list[dict]) -> dict:
    """The header fields of an invoice *state* lowered by the memo takes *released* in one return:
    shipping and a tax amount with no rate each lose ``min(take, current)``, and a fixed discount
    likewise while the invoice's discount is still a fixed amount (a percentage the user set is
    left as it is). Only fields that change are returned; nothing goes below 0."""
    currency = state.get("currency")
    pct = state.get("discount_type") == "percentage"
    current = {"shipping": to_decimal(state.get("shipping") or 0), "discount": fixed_discount(state),
               "tax": unrated_tax(state)}
    fields = {}
    for key, now in current.items():
        if key == "discount" and pct:
            continue
        taken = sum((to_decimal(t.get(key) or 0) for t in released), to_decimal(0))
        if taken and now:
            fields[key] = to_stored_float(round_money(now - min(taken, now), currency))
    return fields


def document_terms(state: dict, *, own_unrated_tax) -> dict:
    """The terms that apply to every line of a document *state*, which a draw's target must share
    with the memo: a percentage header discount, the document taxes, the header rate, and
    *own_unrated_tax*, a tax amount with no rate that is the document's own (0 on a memo; on a
    draft, what none of this memo's takes put there). A fixed discount is not a term, nor is a
    percentage of 0, which discounts nothing."""
    pct = state.get("discount_type") == "percentage" and to_decimal(state.get("discount", 0) or 0)
    taxes = tuple((d.code, to_decimal(d.rate), d.order, d.is_compound)
                  for d in (_definition(e) for e in tax_entries(state.get("doc_taxes"))) if d is not None)
    return {"discount_pct": to_decimal(state.get("discount", 0) or 0) if pct else None,
            "doc_taxes": taxes,
            "tax_rate": to_decimal(state.get("tax_rate", 0) or 0),
            "unrated_tax": max(to_decimal(0), to_decimal(own_unrated_tax or 0))}


def memo_tax_rate_money(memo: dict, rate) -> dict:
    """A memo's money with its tax worked out at *rate* (document_money), the rule every invoice
    drawn from it is then taxed by; the rate correction's preview and write both read it."""
    lines = [dict(line) for line in memo.get("line_items") or [] if isinstance(line, dict)]
    return document_money({**memo, "tax_rate": rate}, lines, memo.get("currency"), keep_unrated_tax=False)


def document_money(state: dict, lines: list[dict], currency: str, *, keep_unrated_tax: bool,
                   stored: dict | None = None) -> dict:
    """Subtotal, discount, taxes and total of a document with these lines.

    Line ``taxes`` are recomputed from their rates, as are a line ``tax_rate``,
    ``doc_taxes``, ``shipping_taxes`` (on the shipping) and a header ``tax_rate``. A document with none of those but a
    ``tax`` amount has nothing to recompute it from: with *keep_unrated_tax* that
    amount stands as it is, otherwise UnratedTaxError is raised so the caller can
    refuse rather than change the amount. When *stored* (the document before this
    change) had a rate, its ``tax`` was computed from it, so an edit that removes the
    last rate leaves no tax rather than that amount. A tax entry it cannot read is
    refused (refuse_malformed_taxes, by the status of *stored* when given), never left out of
    the total.
    """
    refuse_malformed_taxes({"doc_taxes": state.get("doc_taxes"), "shipping_taxes": state.get("shipping_taxes"),
                            "line_items": lines},
                           stored if stored is not None else state)
    return _derived_money(state, lines, currency, keep_unrated_tax=keep_unrated_tax, stored=stored)


def list_money(state: dict, lines: list[dict], currency: str) -> dict:
    """A list's subtotal, discount_amount, tax_amount and total by the document rule
    (document_money): its header ``tax`` is the rate a document holds as ``tax_rate``, and a list
    has no document taxes or shipping. A tax an older version stored that cannot be computed is
    not charged (line_tax_definition), so recomputing a list never fails on it; every list write
    refuses one (refuse_malformed_taxes)."""
    if discount_from_inputs(state, 0, currency) is None:
        raise ValueError("Discount must be a number")
    inputs = {"discount": state.get("discount"), "discount_type": state.get("discount_type"),
              "tax_rate": state.get("tax")}
    money = _derived_money(inputs, lines, currency, keep_unrated_tax=True, stored=None)
    return {"subtotal": money["subtotal"], "discount_amount": money["discount_amount"],
            "tax_amount": money["tax"], "total": money["total"]}


LIST_TOTAL_FIELDS = ("subtotal", "discount_amount", "tax_amount", "total")
# What a document's money is worked out from, and the figures worked out (document_money).
DOC_MONEY_INPUTS = frozenset({"line_items", "discount", "discount_type", "shipping", "doc_taxes", "shipping_taxes",
                              "tax_rate", "currency", "prices_include_tax"})
DOC_TOTAL_FIELDS = ("subtotal", "discount_amount", "tax", "total")
# What a conversion (a quotation to an invoice or memo, a consignment bought) keeps of its source beyond
# a copy's parties and lines (record_copy.conversion_of): every key document_money reads of it (the money
# inputs and a tax amount with no rate, which stands when there is no rate to recompute it from). The term
# it is paid on travels with every copy (record_copy.RECORD_KEYS).
CONVERSION_KEYS = (DOC_MONEY_INPUTS - {"line_items"}) | {"tax"}


def stored_list_totals(state: dict) -> dict:
    """The totals a list holds, by LIST_TOTAL_FIELDS, as numbers; an absent one is zero."""
    return {k: float(state.get(k) or 0) for k in LIST_TOTAL_FIELDS}


def list_totals(state: dict) -> dict:
    """The totals the document rule (list_money) gives a list as it stands, its lines untouched."""
    lines = [dict(i) for i in state.get("line_items") or [] if isinstance(i, dict)]
    return list_money(state, lines, state.get("currency"))


def list_totals_differ(state: dict) -> tuple[str, str] | None:
    """The total a list holds and the one the document rule gives it, as money text, when they
    differ: a list an earlier version totalled by its own rule. None when they agree or the list
    cannot be totalled."""
    currency = state.get("currency")
    try:
        derived = list_totals(state)
    except (ArithmeticError, TypeError, ValueError):
        return None
    held = {k: round_money(to_decimal(state.get(k) or 0), currency) for k in LIST_TOTAL_FIELDS}
    if all(held[k] == round_money(derived[k], currency) for k in LIST_TOTAL_FIELDS):
        return None
    return str(held["total"]), str(round_money(derived["total"], currency))


def _rate_tax(base, rate, currency: str, inclusive: bool) -> Decimal:
    """The tax at *rate* percent on *base*; with *inclusive*, the part of *base* that is that tax."""
    if inclusive:
        return round_money(base, currency) - round_money(base / (1 + rate / 100), currency)
    return round_money(base * rate / 100, currency)


def _derived_money(state: dict, lines: list[dict], currency: str, *, keep_unrated_tax: bool,
                   stored: dict | None) -> dict:
    """document_money's figures. With ``prices_include_tax`` the line prices hold the taxes
    computed on the lines (line taxes, a line or header rate, document taxes): each is
    back-calculated from its gross on its own (compute_tax_amounts), the subtotal is net of
    them and the total is the prices less the discount. Shipping is a charge, not a price: its
    taxes are added to it either way."""
    inclusive = bool(state.get("prices_include_tax"))
    subtotal = round_money(
        sum((_line_amount(line, currency) for line in lines if isinstance(line, dict)), to_decimal(0)),
        currency,
    )
    discount = max(to_decimal(0), to_decimal(state.get("discount", 0) or 0))
    if state.get("discount_type") == "percentage":
        discount_amount = subtotal * discount / 100
    else:
        discount_amount = discount
    discount_amount = round_money(min(max(discount_amount, to_decimal(0)), subtotal), currency)
    taxable = subtotal - discount_amount
    ratio = taxable / subtotal if subtotal > 0 else to_decimal(1)

    line_tax_total = to_decimal(0)
    has_line_tax = False
    for line in lines:
        if not isinstance(line, dict):
            continue
        base = _line_amount(line, currency) * ratio
        line_taxes, rate = line_tax_definition(line)
        if line_taxes:
            resolved, amount = _recompute_tax_applications(line_taxes, base, currency, inclusive)
            line["taxes"] = resolved
            line_tax_total += amount
            has_line_tax = True
        elif rate:
            line_tax_total += _rate_tax(base, rate, currency, inclusive)
            has_line_tax = True

    result: dict = {"discount_amount": to_stored_float(discount_amount)}
    # Shipping is taxed on its own amount by its own taxes (shipping_taxes); the header
    # discount applies to the lines only.
    shipping = round_money(state.get("shipping", 0) or 0, currency)
    shipping_taxes = tax_entries(state.get("shipping_taxes"))
    shipping_tax = to_decimal(0)
    if shipping_taxes:
        result["shipping_taxes"], shipping_tax = _recompute_tax_applications(shipping_taxes, shipping, currency)
    doc_taxes = tax_entries(state.get("doc_taxes"))
    if doc_taxes:
        resolved_doc_taxes, doc_tax_total = _recompute_tax_applications(
            doc_taxes, taxable - line_tax_total if inclusive else taxable, currency, inclusive)
        result["doc_taxes"] = resolved_doc_taxes
        tax_total = line_tax_total + doc_tax_total
    elif has_line_tax or shipping_taxes:
        tax_total = line_tax_total
    elif to_decimal(state.get("tax_rate", 0) or 0):
        tax_total = _rate_tax(taxable, to_decimal(state.get("tax_rate", 0) or 0), currency, inclusive)
    elif stored is not None and _has_rate(stored):
        tax_total = to_decimal(0)
    else:
        tax_total = round_money(state.get("tax", 0) or 0, currency)
        if tax_total and not keep_unrated_tax:
            raise UnratedTaxError("tax amount has no rate")

    # An unrated tax amount (the last branch) was never part of the prices.
    rated = bool(doc_taxes) or has_line_tax or bool(to_decimal(state.get("tax_rate", 0) or 0))
    included = round_money(tax_total, currency) if inclusive and rated else to_decimal(0)
    tax_total = round_money(tax_total + shipping_tax, currency)
    result["subtotal"] = to_stored_float(subtotal - included)
    result["tax"] = to_stored_float(tax_total)
    result["total"] = to_stored_float(round_money(taxable - included + tax_total + shipping, currency))
    return result


def money_from_lines(data: dict, currency: str) -> dict:
    """A document given its lines but no total, with its money taken from those lines: each
    line's total (its own, or quantity times unit price) and the document's subtotal,
    discount, taxes and total (document_money). The one rule for a document created in the
    app and a draft imported without totals."""
    lines = [{**line, "line_total": to_stored_float(_line_amount(line, currency))}
             for line in data.get("line_items") or [] if isinstance(line, dict)]
    return {**data, **document_money(data, lines, currency, keep_unrated_tax=True), "line_items": lines}
