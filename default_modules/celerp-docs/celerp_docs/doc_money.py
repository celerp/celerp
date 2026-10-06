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
from celerp_docs.taxes import TaxApplication, compute_tax_amounts
from ui.i18n import t


class UnratedTaxError(ValueError):
    """The document holds a tax amount with no rate to recompute it from."""


def _recompute_tax_applications(raw, base, currency: str):
    """Recompute stored tax definitions against a new base, never stale amounts."""
    if not isinstance(raw, list) or not raw:
        return [], to_decimal(0)
    definitions: list[TaxApplication] = []
    try:
        for value in raw:
            if not isinstance(value, dict):
                raise ValueError("tax entry is not an object")
            definitions.append(TaxApplication.model_validate({**value, "amount": 0.0}))
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=t("documents.err_tax_invalid"),
        ) from exc
    resolved = compute_tax_amounts(definitions, to_stored_float(round_money(base, currency)), currency)
    return [item.model_dump() for item in resolved], sum(
        (to_decimal(item.amount) for item in resolved), to_decimal(0))


def document_money(state: dict, lines: list[dict], currency: str, *, keep_unrated_tax: bool) -> dict:
    """Subtotal, discount, taxes and total of a document with these lines.

    Line ``taxes`` are recomputed from their rates, as are a line ``tax_rate``,
    ``doc_taxes`` and a header ``tax_rate``. A document with none of those but a
    ``tax`` amount has nothing to recompute it from: with *keep_unrated_tax* that
    amount stands as it is, otherwise UnratedTaxError is raised so the caller can
    refuse rather than change the amount.
    """
    def _line_amount(line: dict):
        value = line.get("line_total")
        if value not in (None, ""):
            return to_decimal(value or 0)
        return (
            to_decimal(line.get("quantity", 0) or 0)
            * to_decimal(line.get("unit_price", 0) or 0)
        )

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
        raw_taxes = line.get("taxes")
        if isinstance(raw_taxes, list) and raw_taxes:
            resolved, amount = _recompute_tax_applications(raw_taxes, base, currency)
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
    raw_doc_taxes = state.get("doc_taxes")
    if isinstance(raw_doc_taxes, list) and raw_doc_taxes:
        resolved_doc_taxes, doc_tax_total = _recompute_tax_applications(
            raw_doc_taxes, taxable, currency)
        result["doc_taxes"] = resolved_doc_taxes
        tax_total = line_tax_total + doc_tax_total
    elif has_line_tax:
        tax_total = line_tax_total
    elif to_decimal(state.get("tax_rate", 0) or 0):
        tax_total = round_money(
            taxable * to_decimal(state.get("tax_rate", 0) or 0) / 100,
            currency,
        )
    else:
        tax_total = round_money(state.get("tax", 0) or 0, currency)
        if tax_total and not keep_unrated_tax:
            raise UnratedTaxError("tax amount has no rate")

    tax_total = round_money(tax_total, currency)
    shipping = round_money(state.get("shipping", 0) or 0, currency)
    result["tax"] = to_stored_float(tax_total)
    result["total"] = to_stored_float(round_money(taxable + tax_total + shipping, currency))
    return result
