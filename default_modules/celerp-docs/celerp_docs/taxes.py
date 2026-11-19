# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
from __future__ import annotations

from decimal import Decimal

from celerp.localization.taxcodes import LineTaxSnapshot
from celerp.services.money import round_money, to_decimal, to_stored_float


def compute_tax_amounts(
    taxes: list[LineTaxSnapshot],
    base: float,
    currency: str = "USD",
) -> list[LineTaxSnapshot]:
    """Recompute tax amounts in order, respecting is_compound.

    For each tax (sorted by order):
    - is_compound=False: amount = round(base * rate / 100, currency_dp)
    - is_compound=True:  amount = round((base + sum of preceding amounts) * rate / 100, currency_dp)

    Amounts provided by the caller (non-zero) are kept as-is (caller override).
    Returns a new list with amounts filled in.
    """
    sorted_taxes = sorted(taxes, key=lambda t: t.order)
    result: list[LineTaxSnapshot] = []
    running_tax_total = Decimal(0)
    base_d = to_decimal(base)

    for item in sorted_taxes:
        if item.amount != 0.0:
            # Caller provided explicit amount - trust it
            result.append(item)
            running_tax_total += to_decimal(item.amount)
        else:
            tax_base = (base_d + running_tax_total) if item.is_compound else base_d
            amount = round_money(tax_base * to_decimal(item.rate) / 100, currency)
            result.append(item.model_copy(update={"amount": to_stored_float(amount)}))
            running_tax_total += amount

    return result
