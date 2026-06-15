# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Landed-cost allocation (P3): spread a bill's capitalisable charges (freight/insurance/duty +
non-recoverable import VAT) across its stocked goods lines by value, returning a per-unit landed cost
per (line, kind). Per-unit results scale naturally as quantity is received. Pure and deterministic.

See context/2026-0614-freight-tracking-plan.md sections 3 and 6.
"""
from __future__ import annotations


def allocate_landed_cost(
    goods: list[dict],
    components: list[dict],
) -> dict[str, dict[str, float]]:
    """Allocate landed-cost components across goods lines by value.

    goods:      [{"key": str, "value": float (extended base-currency cost), "qty": float}]
    components: [{"kind": str, "amount": float (base-currency, capitalisable only)}]

    Returns {goods_key: {kind: per_unit_landed}}. Per kind, the rounded shares sum exactly to the
    component pool (residual assigned to the largest line); falls back to a quantity basis when the
    total goods value is zero. Per-unit amounts are kept at full precision; callers round the final
    landed total (unit x quantity).
    """
    result: dict[str, dict[str, float]] = {g["key"]: {} for g in goods}
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

    basis_total = total_value if use_value else total_qty
    largest = max(goods, key=_basis)

    for kind, pool in by_kind.items():
        if pool <= 0:
            continue
        shares: dict[str, float] = {g["key"]: round(pool * _basis(g) / basis_total, 2) for g in goods}
        residual = round(pool - sum(shares.values()), 2)
        if residual:
            shares[largest["key"]] = round(shares[largest["key"]] + residual, 2)
        for g in goods:
            qty = float(g["qty"])
            if qty:
                result[g["key"]][kind] = shares[g["key"]] / qty  # per-unit, full precision
    return result


_KIND_BY_CLEARING = {"1130-FRT": "freight", "1130-INS": "insurance", "1130-DTY": "duty", "1130-IVT": "import_vat"}


async def compute_bill_landed_allocation(session, company_id, doc_state: dict) -> dict[str, dict[str, float]]:
    """Allocate a bill's capitalisable charge lines across its stocked goods lines, keyed by goods SKU.

    Returns {sku: {kind: per_unit_landed}}. Recoverable import VAT is excluded (it does not capitalise);
    landed amounts are converted to base currency by the bill conversion rate. Reuses the same
    account-routing logic as the bill JE so cost allocation and GL postings stay consistent.
    """
    from celerp.services.auto_je import landed_account_for_line
    from celerp.services.units import is_non_stock_line
    from celerp.models.projections import Projection

    rate = float(doc_state.get("conversion_rate") or 1)
    components: list[dict] = []
    goods: list[dict] = []
    for li in doc_state.get("line_items", []):
        line_total = float(li.get("line_total") or
                           (float(li.get("quantity") or 0) * float(li.get("unit_price") or 0)))
        base_amt = round(line_total * rate, 2)
        acct = await landed_account_for_line(session, company_id, li)
        if acct in _KIND_BY_CLEARING:
            if base_amt:
                components.append({"kind": _KIND_BY_CLEARING[acct], "amount": base_amt})
            continue
        if acct == "1150":
            continue  # recoverable import VAT: not capitalised
        # Goods line: include if it is stocked (skip service/non-stock lines).
        inv_type = None
        item_id = li.get("item_id") or li.get("entity_id")
        if item_id:
            proj = await session.get(Projection, {"company_id": company_id, "entity_id": str(item_id)})
            if proj:
                inv_type = proj.state.get("inventory_type")
        if not is_non_stock_line(inv_type, li.get("sell_by")) and base_amt > 0 and li.get("sku"):
            goods.append({"key": li.get("sku"), "value": base_amt, "qty": float(li.get("quantity") or 0)})
    return allocate_landed_cost(goods, components)
