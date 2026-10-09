# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Landed-cost allocation (P3): spread a bill's capitalisable charges (freight/insurance/duty +
non-recoverable import VAT) across its stocked goods lines by value, returning a per-unit landed cost
per (line, kind). Per-unit results scale naturally as quantity is received. Pure and deterministic.

See context/2026-0614-freight-tracking-plan.md sections 3 and 6.
"""
from __future__ import annotations

from collections.abc import Hashable

from celerp.services.money import allocate_pro_rata, to_decimal, to_stored_float


def allocate_landed_cost(
    goods: list[dict],
    components: list[dict],
    currency: str,
) -> dict[Hashable, dict[str, float]]:
    """Allocate landed-cost components across goods lines by value.

    goods:      [{"key": hashable line key, "value": float (extended base-currency cost), "qty": float}]
    components: [{"kind": str, "amount": float (base-currency, capitalisable only)}]

    Returns {goods_key: {kind: per_unit_landed}}. Per kind, the shares, rounded to the minor unit of
    `currency` (the base currency), sum exactly to the component pool (allocate_pro_rata, largest
    remainder, so no line takes less than nothing); falls back to a quantity basis when the total
    goods value is zero. Per-unit amounts are kept at full precision; callers round the final
    landed total (unit x quantity).
    """
    result: dict[Hashable, dict[str, float]] = {g["key"]: {} for g in goods}
    if not goods:
        return result

    total_value = sum(float(g["value"]) for g in goods)
    total_qty = sum(float(g["qty"]) for g in goods)

    by_kind: dict[str, float] = {}
    for c in components:
        amt = float(c.get("amount") or 0)
        if amt:
            by_kind[c["kind"]] = by_kind.get(c["kind"], 0.0) + amt

    use_value = total_value > 0
    if not use_value and total_qty <= 0:
        return result  # nothing to allocate against

    def _basis(g: dict) -> float:
        return float(g["value"]) if use_value else float(g["qty"])

    for kind, pool in by_kind.items():
        if pool <= 0:
            continue
        shares = dict(zip((g["key"] for g in goods),
                          allocate_pro_rata(to_decimal(pool), [to_decimal(_basis(g)) for g in goods], currency)))
        for g in goods:
            qty = float(g["qty"])
            if qty:
                result[g["key"]][kind] = to_stored_float(shares[g["key"]]) / qty  # per-unit, full precision
    return result




async def compute_bill_landed_allocation(session, company_id, doc_state: dict) -> dict[int, dict[str, float]]:
    """Allocate a bill's capitalisable charges across the lines it brings in as stock.

    Returns {line index: {kind: landed per unit of the line's quantity}}. Every stock line
    takes its share, whether or not it names an item or SKU. The charges are its charge lines,
    each at what the bill's entry books for it after its share of any discount
    (bill_line_charges), and the shipping it charged, which is freight. Recoverable import VAT
    is excluded (it does not capitalise); landed amounts are converted to base currency by the
    bill conversion rate. Reuses the same account-routing logic as the bill JE so cost
    allocation and GL postings stay consistent.
    """
    from celerp.models.company import Company
    from celerp.models.projections import Projection
    from celerp.accounting_roles import LANDED_KIND_BY_ROLE, AccountRole
    from celerp.services.auto_je import bill_line_charges, bill_line_kind, landed_role_for_line
    from celerp.services.money import require_doc_rate, to_base
    from celerp.services.units import is_non_stock_line

    company = await session.get(Company, company_id)
    base_currency = (company.settings.get("currency", "USD") if company else "USD")
    rate = require_doc_rate(doc_state, base_currency)
    shipping = to_base(doc_state.get("shipping") or 0, rate, base_currency)
    components: list[dict] = [{"kind": LANDED_KIND_BY_ROLE[AccountRole.LANDED_FREIGHT], "amount": shipping}]
    goods: list[dict] = []
    lines = doc_state.get("line_items", [])
    for index, (li, (gross, discount, _tax)) in enumerate(zip(lines, bill_line_charges(doc_state))):
        line_total = float(li.get("line_total") or
                           (float(li.get("quantity") or 0) * float(li.get("unit_price") or 0)))
        base_amt = to_base(line_total, rate, base_currency)
        role = await landed_role_for_line(session, company_id, li)
        if role in LANDED_KIND_BY_ROLE:
            charged = to_base(gross - discount, rate, base_currency)
            if charged:
                components.append({"kind": LANDED_KIND_BY_ROLE[role], "amount": charged})
            continue
        if role == AccountRole.TAX_INPUT:
            continue  # recoverable import VAT: not capitalised
        # Goods line: include if the bill brings it in as stock (skip service/non-stock lines).
        if bill_line_kind(li) != "stock":
            continue
        inv_type = None
        item_id = li.get("item_id") or li.get("entity_id")
        if item_id:
            proj = await session.get(Projection, {"company_id": company_id, "entity_id": str(item_id)})
            if proj:
                inv_type = proj.state.get("inventory_type")
        if not is_non_stock_line(inv_type, li.get("sell_by")) and base_amt > 0:
            goods.append({"key": index, "value": base_amt, "qty": float(li.get("quantity") or 0)})
    return allocate_landed_cost(goods, components, base_currency)
