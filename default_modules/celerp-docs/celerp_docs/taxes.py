# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
from __future__ import annotations

from pydantic import BaseModel


class TaxApplication(BaseModel):
    code: str            # references company tax registry by name
    rate: float          # rate snapshot at doc creation time
    amount: float = 0.0  # computed signed amount (+liability / -offset like WHT); server recomputes if 0
    order: int = 0       # sequencing for stacking
    is_compound: bool = False  # if True: applies to (base + sum of preceding tax amounts)
    label: str = ""      # optional display override; if empty, derived from code or "Tax"


def compute_tax_amounts(taxes: list[TaxApplication], base: float) -> list[TaxApplication]:
    """Recompute tax amounts in order, respecting is_compound.

    For each tax (sorted by order):
    - is_compound=False: amount = base * rate / 100
    - is_compound=True:  amount = (base + sum of preceding amounts) * rate / 100

    Amounts provided by the caller (non-zero) are kept as-is (caller override).
    Returns a new list with amounts filled in.
    """
    sorted_taxes = sorted(taxes, key=lambda t: t.order)
    result: list[TaxApplication] = []
    running_tax_total = 0.0
    for item in sorted_taxes:
        if item.amount != 0.0:
            # Caller provided explicit amount — trust it
            result.append(item)
            running_tax_total += item.amount
        else:
            tax_base = (base + running_tax_total) if item.is_compound else base
            amount = round(tax_base * item.rate / 100, 2)
            result.append(item.model_copy(update={"amount": amount}))
            running_tax_total += amount
    return result
