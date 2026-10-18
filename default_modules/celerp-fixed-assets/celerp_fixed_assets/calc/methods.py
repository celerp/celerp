# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Depreciation methods. Each turns one layer's depreciable amount and the life it
consumes month by month into unrounded monthly charges."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

_ZERO = Decimal(0)


@dataclass(frozen=True)
class MethodInputs:
    """``basis`` is the amount to depreciate, ``residual`` what stays at the end,
    ``life`` the life to consume and ``fractions`` how much of it each month consumed
    (months for time-based methods, units for units of use)."""
    basis: Decimal
    residual: Decimal
    life: Decimal
    fractions: list[Decimal]
    params: dict = field(default_factory=dict)


def _by_curve(inputs: MethodInputs, curve) -> list[Decimal]:
    """Charges as differences of the depreciated share ``curve(position)``, so they
    add up exactly to the basis when the life is used up."""
    out, position, done = [], _ZERO, _ZERO
    for fraction in inputs.fractions:
        position += fraction
        reached = inputs.basis * curve(position)
        out.append(reached - done)
        done = reached
    return out


class StraightLine:
    key = "straight_line"
    label_key = "fixed_assets.method.straight_line"
    params_schema: dict = {}
    uses_readings = False

    def amounts(self, inputs: MethodInputs) -> list[Decimal]:
        if inputs.life <= 0:
            return [_ZERO] * len(inputs.fractions)
        return _by_curve(inputs, lambda t: min(max(t, _ZERO), inputs.life) / inputs.life)


class UnitsOfUse(StraightLine):
    """The life is a number of units; each month consumes the units read for it."""
    key = "units_of_use"
    label_key = "fixed_assets.method.units_of_use"
    params_schema = {"total_units": "decimal"}
    uses_readings = True


class SumOfYearsDigits:
    """Each year of life charges its digit over the sum of the digits; a final part year
    has a part digit. Months within a year charge evenly."""
    key = "sum_of_years_digits"
    label_key = "fixed_assets.method.sum_of_years_digits"
    params_schema: dict = {}
    uses_readings = False

    def amounts(self, inputs: MethodInputs) -> list[Decimal]:
        if inputs.life <= 0:
            return [_ZERO] * len(inputs.fractions)
        years = inputs.life / 12
        count = math.ceil(years)
        digits = [years - k for k in range(count)]
        total = sum(digits, _ZERO)

        def curve(t: Decimal) -> Decimal:
            t = min(max(t, _ZERO), inputs.life)
            share, start = _ZERO, _ZERO
            for digit in digits:
                length = min(Decimal(12), inputs.life - start)
                if t <= start + length:
                    return share + digit / total * (t - start) / length
                share += digit / total
                start += length
            return share

        return _by_curve(inputs, curve)


class DecliningBalance:
    """A fixed share of the carrying amount each year (``factor`` over the life in years,
    or an entered annual ``rate``), switching to straight line over the remaining life
    once that charges more. The last month of life writes down to the residual."""
    key = "declining_balance"
    label_key = "fixed_assets.method.declining_balance"
    params_schema = {"factor": "decimal", "rate": "decimal", "switch_to_straight_line": "bool"}
    uses_readings = False

    def amounts(self, inputs: MethodInputs) -> list[Decimal]:
        if inputs.life <= 0:
            return [_ZERO] * len(inputs.fractions)
        params = inputs.params
        if params.get("rate") not in (None, ""):
            monthly = Decimal(str(params["rate"])) / 12
        else:
            monthly = Decimal(str(params.get("factor", 2))) / inputs.life
        switch = params.get("switch_to_straight_line", True)
        carrying = inputs.basis + inputs.residual
        out, position = [], _ZERO
        for fraction in inputs.fractions:
            left = carrying - inputs.residual
            remaining = inputs.life - position
            if fraction and position + fraction >= inputs.life:
                charge = left
            elif fraction:
                charge = carrying * monthly * fraction
                if switch and remaining > 0:
                    charge = max(charge, left * fraction / remaining)
                charge = min(charge, left)
            else:
                charge = _ZERO
            out.append(charge)
            carrying -= charge
            position += fraction
        return out


class NoDepreciation:
    """Land and indefinite-life intangibles."""
    key = "none"
    label_key = "fixed_assets.method.none"
    params_schema: dict = {}
    uses_readings = False

    def amounts(self, inputs: MethodInputs) -> list[Decimal]:
        return [_ZERO] * len(inputs.fractions)
