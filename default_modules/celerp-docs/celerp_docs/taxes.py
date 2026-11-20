# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
from __future__ import annotations

from decimal import Decimal

from celerp.localization.taxcodes import LineTaxSnapshot
from celerp.services.money import round_money, to_decimal, to_stored_float
from celerp.tax_regimes import charged


def compute_tax_amounts(
    taxes: list[LineTaxSnapshot],
    base: float,
    currency: str = "USD",
    *,
    inclusive: bool = False,
) -> list[LineTaxSnapshot]:
    """Compute each tax's amount in ``order``: round(rate% of its base, currency_dp), its base
    being *base* plus, for an ``is_compound`` tax, the amounts before it. Each result records the
    base it applied to and *inclusive*.

    With *inclusive*, *base* is a price that already holds these taxes: the net is *base* over
    one plus the taxes' combined fraction, rounded, each tax is computed on that net as above, and
    the last one takes the rounding remainder, so the net and the amounts add up to *base*
    exactly (107.00 at 7% is 100.00 plus 7.00).

    A self-assessed tax (not charged) is computed on the net the same way, but the price never
    holds it and no tax after it compounds on it.
    """
    ordered = sorted(taxes, key=lambda t: t.order)
    base_d = round_money(to_decimal(base), currency)
    if inclusive and ordered:
        base_d = round_money(base_d / (1 + _combined_fraction(ordered)), currency)
    result: list[LineTaxSnapshot] = []
    running = Decimal(0)
    last_charged = None
    for item in ordered:
        tax_base = base_d + running if item.is_compound else base_d
        amount = round_money(tax_base * to_decimal(item.rate) / 100, currency)
        result.append(item.model_copy(update={"amount": to_stored_float(amount), "base": to_stored_float(tax_base),
                                              "inclusive": inclusive}))
        if charged(item):
            running += amount
            last_charged = len(result) - 1
    if inclusive and last_charged is not None:
        remainder = round_money(to_decimal(base), currency) - base_d - running
        last = result[last_charged]
        result[last_charged] = last.model_copy(update={"amount": to_stored_float(to_decimal(last.amount) + remainder)})
    return result


def _combined_fraction(ordered: list[LineTaxSnapshot]) -> Decimal:
    """What the charged taxes add to a net of 1, unrounded, compounding as compute_tax_amounts does."""
    running = Decimal(0)
    for item in ordered:
        if charged(item):
            running += (1 + running if item.is_compound else Decimal(1)) * to_decimal(item.rate) / 100
    return running
