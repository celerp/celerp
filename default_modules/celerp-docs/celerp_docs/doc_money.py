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

from celerp.services.list_behavior import FINALIZED
from celerp.services.money import discount_from_inputs, document_line_amount, round_money, to_decimal, to_stored_float
from celerp.tax_regimes import line_tax_unset, tax_entries, tax_text, unreadable_tax
from celerp_docs.doc_constants import (DRAFT_REVERT_RETURNED, DRAFT_REVERT_STATUSES, INBOUND_DOC_TYPES,
                                      INBOUND_DRAFT_REVERT_STATUSES)
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from ui.i18n import t


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


def _issued_detail(key: str, state: dict, **values) -> str:
    """A past-draft refusal *key* ending with the way back to a draft (draft_route)."""
    return t(key, route=" ".join(t(step) for step in draft_route(state)), **values)


def unreadable_tax_refusal(tax: str, state: dict | None = None) -> HTTPException:
    """The 422 refusing a tax it cannot read, named. Only a draft's taxes can be changed, so on a
    document *state* past draft the message names the way back to one (draft_route)."""
    if _past_draft(state):
        return HTTPException(status_code=422, detail=_issued_detail("documents.err_tax_unreadable_issued", state, tax=tax))
    return HTTPException(status_code=422, detail=t("documents.err_tax_unreadable", tax=tax))


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
                detail = t("documents.err_line_tax_unset_receive" if receiving else "documents.err_line_tax_unset",
                           line=number)
            raise HTTPException(status_code=422, detail=detail)


def _definition(entry) -> TaxApplication | None:
    """A stored tax entry as the tax definition its amount is computed from, or None when it is
    not one (not a tax object, or a rate that is not a number)."""
    if not isinstance(entry, dict):
        return None
    try:
        return TaxApplication.model_validate({**entry, "amount": 0.0})
    except ValueError:
        return None


def computable_tax(entry) -> bool:
    """Whether a stored tax entry is a tax a total can be computed from."""
    return _definition(entry) is not None


def _recompute_tax_applications(entries: list, base, currency: str):
    """Recompute stored tax definitions against a new base, never stale amounts. The entries
    were checked first (refuse_malformed_taxes)."""
    resolved = compute_tax_amounts([_definition(value) for value in entries],
                                   to_stored_float(round_money(base, currency)), currency)
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
    held = [values.get("doc_taxes")]
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
    document tax or a header ``tax_rate``."""
    if tax_entries(state.get("doc_taxes")) or to_decimal(state.get("tax_rate", 0) or 0):
        return True
    return any(isinstance(line, dict) and line_has_tax(line) for line in state.get("line_items") or [])


def document_money(state: dict, lines: list[dict], currency: str, *, keep_unrated_tax: bool,
                   stored: dict | None = None) -> dict:
    """Subtotal, discount, taxes and total of a document with these lines.

    Line ``taxes`` are recomputed from their rates, as are a line ``tax_rate``,
    ``doc_taxes`` and a header ``tax_rate``. A document with none of those but a
    ``tax`` amount has nothing to recompute it from: with *keep_unrated_tax* that
    amount stands as it is, otherwise UnratedTaxError is raised so the caller can
    refuse rather than change the amount. When *stored* (the document before this
    change) had a rate, its ``tax`` was computed from it, so an edit that removes the
    last rate leaves no tax rather than that amount. A tax entry it cannot read is
    refused (refuse_malformed_taxes, by the status of *stored* when given), never left out of
    the total.
    """
    refuse_malformed_taxes({"doc_taxes": state.get("doc_taxes"), "line_items": lines},
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


def _derived_money(state: dict, lines: list[dict], currency: str, *, keep_unrated_tax: bool,
                   stored: dict | None) -> dict:
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
            resolved, amount = _recompute_tax_applications(line_taxes, base, currency)
            line["taxes"] = resolved
            line_tax_total += amount
            has_line_tax = True
        elif rate:
            line_tax_total += round_money(base * rate / 100, currency)
            has_line_tax = True

    result: dict = {
        "subtotal": to_stored_float(subtotal),
        "discount_amount": to_stored_float(discount_amount),
    }
    doc_taxes = tax_entries(state.get("doc_taxes"))
    if doc_taxes:
        resolved_doc_taxes, doc_tax_total = _recompute_tax_applications(
            doc_taxes, taxable, currency)
        result["doc_taxes"] = resolved_doc_taxes
        tax_total = line_tax_total + doc_tax_total
    elif has_line_tax:
        tax_total = line_tax_total
    elif to_decimal(state.get("tax_rate", 0) or 0):
        tax_total = round_money(
            taxable * to_decimal(state.get("tax_rate", 0) or 0) / 100,
            currency,
        )
    elif stored is not None and _has_rate(stored):
        tax_total = to_decimal(0)
    else:
        tax_total = round_money(state.get("tax", 0) or 0, currency)
        if tax_total and not keep_unrated_tax:
            raise UnratedTaxError("tax amount has no rate")

    tax_total = round_money(tax_total, currency)
    shipping = round_money(state.get("shipping", 0) or 0, currency)
    result["tax"] = to_stored_float(tax_total)
    result["total"] = to_stored_float(round_money(taxable + tax_total + shipping, currency))
    return result


def money_from_lines(data: dict, currency: str) -> dict:
    """A document given its lines but no total, with its money taken from those lines: each
    line's total (its own, or quantity times unit price) and the document's subtotal,
    discount, taxes and total (document_money). The one rule for a document created in the
    app and a draft imported without totals."""
    lines = [{**line, "line_total": to_stored_float(_line_amount(line, currency))}
             for line in data.get("line_items") or [] if isinstance(line, dict)]
    return {**data, **document_money(data, lines, currency, keep_unrated_tax=True), "line_items": lines}
