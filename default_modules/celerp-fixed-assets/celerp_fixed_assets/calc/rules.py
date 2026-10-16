# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The method and convention registries and the shapes their entries take."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from celerp_fixed_assets.calc import conventions, methods
from celerp_fixed_assets.calc.methods import MethodInputs


class DepreciationMethod(Protocol):
    key: str
    label_key: str
    params_schema: dict
    uses_readings: bool

    def amounts(self, inputs: MethodInputs) -> list[Decimal]: ...


class TimingConvention(Protocol):
    key: str
    label_key: str

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]: ...

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal: ...


METHODS: dict[str, DepreciationMethod] = {m.key: m for m in (
    methods.StraightLine(), methods.DecliningBalance(), methods.SumOfYearsDigits(),
    methods.UnitsOfUse(), methods.NoDepreciation())}

CONVENTIONS: dict[str, TimingConvention] = {c.key: c for c in (
    conventions.FullMonth(), conventions.MidMonth(), conventions.Daily(), conventions.HalfYear())}
