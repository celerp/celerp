# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Country ZZ's fixed asset rules."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from celerp_fixed_assets.calc.tax import (
    DeferredTaxResult, TaxCategory, TaxTransferResult, TaxYearResult)

_ZERO = Decimal(0)


class FixedRate:
    """A fixed share of cost each month, ``rate`` a year, until nothing is left."""
    key = "zz_fixed_rate"
    label_key = "zz.method.fixed_rate"
    params_schema = {"rate": "decimal"}
    uses_readings = False

    def amounts(self, inputs):
        monthly = inputs.basis * Decimal(str(inputs.params.get("rate") or "0.1")) / 12
        out, left = [], inputs.basis
        for fraction in inputs.fractions:
            charge = min(monthly * fraction, left)
            out.append(charge)
            left -= charge
        return out


class QuarterStart:
    """A layer starts at the beginning of the next quarter of the calendar year."""
    key = "zz_quarter_start"
    label_key = "zz.convention.quarter_start"

    def start_weights(self, day, fiscal_year_start):
        return [Decimal(0)] * ((3 - (day.month - 1) % 3) % 3) + [Decimal(1)]

    def disposal(self, normal, day, fy_consumed, remaining):
        return min(normal, remaining)


def _year(tax_year_end: str, year: int) -> tuple[date, date]:
    month, day = (int(p) for p in tax_year_end.split("-"))
    return date(year - 1, month, day) + timedelta(days=1), date(year, month, day)


class _Regime:
    country = "ZZ"
    elections_schema: dict = {}
    deferred_tax = None

    def tax_year(self, tax_year_end, year):
        return _year(tax_year_end, year)

    def related_party_transfer(self, inputs):
        return TaxTransferResult(written_down_value=inputs.opening)


class AssetRegime(_Regime):
    """Each asset writes down its own balance: a category rate a year, the whole of it
    when the owner elects full expensing, and what is left when it is disposed of."""
    key = "zz_asset"
    label_key = "zz.regime.asset"
    basis = "asset"
    categories = [TaxCategory("zz_plant", "zz.category.plant"),
                  TaxCategory("zz_buildings", "zz.category.buildings"),
                  TaxCategory("zz_none", "zz.category.none")]
    elections_schema = {"full_expensing": "bool"}
    _RATES = {"zz_plant": Decimal("0.2"), "zz_buildings": Decimal("0.05"), "zz_none": _ZERO}

    def asset_year(self, inputs):
        balance = inputs.opening + inputs.additions - inputs.disposals
        if inputs.disposed or inputs.elections.get("full_expensing"):
            allowance = balance
        else:
            allowance = (balance * self._RATES[inputs.category]).quantize(Decimal("0.01"))
        return TaxYearResult(opening=inputs.opening, additions=inputs.additions, disposals=inputs.disposals,
                             allowance=allowance, closing=balance - allowance)


class PoolRegime(_Regime):
    """Assets pool by category; each pool writes down a quarter of its balance a year,
    and a pool driven below zero by proceeds claws the excess back."""
    key = "zz_pool"
    label_key = "zz.regime.pool"
    basis = "pool"
    categories = [TaxCategory("zz_general", "zz.category.general"),
                  TaxCategory("zz_cars", "zz.category.cars")]
    _RATE = Decimal("0.25")

    def pool_key(self, inputs):
        return inputs.category

    def pool_year(self, pool):
        balance = pool.opening + pool.additions - pool.disposals
        allowance = balance if balance < 0 else (balance * self._RATE).quantize(Decimal("0.01"))
        return TaxYearResult(opening=pool.opening, additions=pool.additions, disposals=pool.disposals,
                             allowance=allowance, closing=balance - allowance)

    def deferred_tax(self, inputs):
        rate = Decimal("0.2")
        return DeferredTaxResult(rate=rate, amount=((inputs.book_carrying - inputs.tax_written_down_value)
                                                    * rate).quantize(Decimal("0.01")))


class PlainPoolRegime(PoolRegime):
    """The same pools, for a company that reports no deferred tax."""
    key = "zz_pool_plain"
    label_key = "zz.regime.pool_plain"
    deferred_tax = None


class FormA:
    """Form A: the tax written-down value of each pool at the tax year end."""
    key = "zz_form_a"
    country = "ZZ"
    label_key = "zz.form.a"
    regime = "zz_pool"

    def render(self, books, year):
        return {"columns": ["zz.form.pool", "zz.form.allowance", "zz.form.closing"],
                "rows": [[row["key"], row["allowance"], row["closing"]] for row in books]}


class Broken:
    """A method that cannot calculate anything: it has no amounts."""
    key = "zz_broken"
    label_key = "zz.method.broken"
    params_schema: dict = {}
    uses_readings = False


class Shadow(FixedRate):
    key = "straight_line"
