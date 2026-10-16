# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The shapes a country's tax regime reads and returns.

A regime (celerp_fixed_assets.calc.rules) works out each tax year from these inputs and
nothing else: it never reads the books and never posts. Amounts are Decimal in the
company's base currency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class TaxCategory:
    """A category the regime writes assets down under, with the catalog key of its name."""
    key: str
    label_key: str


@dataclass(frozen=True)
class TaxAssetInputs:
    """One asset's tax year: its category and elections, and what moved in the year.
    ``additions`` is cost added in the year, ``disposals`` the proceeds of what left it,
    and ``disposed`` whether the asset was wholly disposed of by the year end."""
    asset_id: str
    category: str
    elections: dict
    year: int
    start: date
    end: date
    available_date: date
    opening: Decimal
    additions: Decimal
    disposals: Decimal
    disposed: bool


@dataclass(frozen=True)
class TaxYearResult:
    """A year of an asset or a pool: what it opened with, what moved, the allowance
    claimed and the written-down value carried to the next year."""
    opening: Decimal
    additions: Decimal
    disposals: Decimal
    allowance: Decimal
    closing: Decimal


PoolYearResult = TaxYearResult


@dataclass(frozen=True)
class PoolInputs:
    """One pool's tax year: the totals of the assets the regime pooled under ``key``."""
    key: str
    category: str
    year: int
    opening: Decimal
    additions: Decimal
    disposals: Decimal
    assets: tuple = field(default=())


@dataclass(frozen=True)
class DeferredTaxInputs:
    """The book and tax values of one asset or pool at a tax year end."""
    year: int
    book_carrying: Decimal
    tax_written_down_value: Decimal


@dataclass(frozen=True)
class DeferredTaxResult:
    rate: Decimal
    amount: Decimal


@dataclass(frozen=True)
class TaxTransferResult:
    """The tax written-down value an asset carries to a related party."""
    written_down_value: Decimal
