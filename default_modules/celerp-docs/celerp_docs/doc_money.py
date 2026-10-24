# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A document's money derived from its lines and header inputs, in one place.

The header discount (flat or a percentage of the subtotal) reduces the taxable
base, and every tax, per line or on the whole document, is computed on that
reduced base. This is the same rule the document editor applies on screen.
"""
from __future__ import annotations

from fastapi import HTTPException

from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp.tax_regimes import line_tax_unset, tax_entries, tax_text, unreadable_tax
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from ui.i18n import t


class UnratedTaxError(ValueError):
    """The document holds a tax amount with no rate to recompute it from."""


def _past_draft(state: dict | None) -> bool:
    """Whether a document *state* is past draft, where its taxes can no longer be changed."""
    return (state or {}).get("status") not in (None, "draft")


def unreadable_tax_refusal(tax: str, state: dict | None = None) -> HTTPException:
    """The 422 refusing a tax it cannot read, named. Only a draft's taxes can be changed, so on a
    document *state* past draft the message names the way back to one."""
    key = "documents.err_tax_unreadable_issued" if _past_draft(state) else "documents.err_tax_unreadable"
    return HTTPException(status_code=422, detail=t(key, tax=tax))


def refuse_unset_line_tax(lines, state: dict | None = None, numbers: list[int] | None = None, *,
                          receiving: bool = False) -> None:
    """Refuse (422, naming the line) issuing *lines*, or *receiving* goods on them, while one's tax
    is still for the user to set (line_tax_unset): it would be charged none, and a receipt takes a
    draft past the point where the tax can be chosen. Each line is numbered by its position, or by
    *numbers* (counted from 0) when they are a selection of the document's lines. On a document
    *state* past draft the message names the way back to one, as unreadable_tax_refusal does."""
    for i, line in enumerate(lines or []):
        if isinstance(line, dict) and line_tax_unset(line):
            key = ("documents.err_line_tax_unset_issued" if _past_draft(state)
                   else "documents.err_line_tax_unset_receive" if receiving else "documents.err_line_tax_unset")
            raise HTTPException(status_code=422, detail=t(key, line=(numbers[i] if numbers else i) + 1))


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


def _line_amount(line: dict):
    value = line.get("line_total")
    if value not in (None, ""):
        return to_decimal(value or 0)
    return (
        to_decimal(line.get("quantity", 0) or 0)
        * to_decimal(line.get("unit_price", 0) or 0)
    )


def _has_rate(state: dict) -> bool:
    """Whether a document's tax comes from a rate: a line's ``taxes`` or ``tax_rate``, a
    document tax or a header ``tax_rate``."""
    if tax_entries(state.get("doc_taxes")) or to_decimal(state.get("tax_rate", 0) or 0):
        return True
    return any(isinstance(line, dict) and (tax_entries(line.get("taxes")) or to_decimal(line.get("tax_rate", 0) or 0))
               for line in state.get("line_items") or [])


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
    subtotal = round_money(
        sum((_line_amount(line) for line in lines if isinstance(line, dict)), to_decimal(0)),
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
        base = _line_amount(line) * ratio
        line_taxes = tax_entries(line.get("taxes"))
        if line_taxes:
            resolved, amount = _recompute_tax_applications(line_taxes, base, currency)
            line["taxes"] = resolved
            line_tax_total += amount
            has_line_tax = True
            continue
        rate = to_decimal(line.get("tax_rate", 0) or 0)
        if rate:
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
    lines = [{**line, "line_total": to_stored_float(round_money(_line_amount(line), currency))}
             for line in data.get("line_items") or [] if isinstance(line, dict)]
    return {**data, **document_money(data, lines, currency, keep_unrated_tax=True), "line_items": lines}
