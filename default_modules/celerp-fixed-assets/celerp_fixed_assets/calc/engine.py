# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Depreciation on a dated movement sequence. Pure: no I/O, ``Decimal`` throughout.

An asset is a set of layers: the acquisition (or opening balance) and each
improvement. Each layer depreciates its own amount over its own life, consumed month
by month under the timing convention. An estimate change merges the layers into one:
the carrying amount less the new residual over the new remaining life, from the month
of the change.

Rounding is cumulative: a month's amount is the rounded cumulative charge less the
rounded cumulative of the month before, so the total lands exactly on the depreciable
amount at any currency precision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from celerp.services.money import round_money
from celerp_fixed_assets.calc.methods import MethodInputs
from celerp_fixed_assets.calc.periods import (
    fiscal_year_start_index,
    month_index,
    parse_period,
    period_label,
)
from celerp_fixed_assets.calc.rules import CONVENTIONS, METHODS

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True)
class Acquire:
    """Available for use on ``on`` at ``amount``."""
    on: date
    amount: Decimal


@dataclass(frozen=True)
class Improve:
    """An improvement layer. With no ``life_months`` it keeps the asset's end date."""
    on: date
    amount: Decimal
    life_months: int | None = None


@dataclass(frozen=True)
class EstimateChange:
    on: date
    life_months: int
    residual: Decimal


@dataclass(frozen=True)
class Dispose:
    on: date


@dataclass(frozen=True)
class Opening:
    """Brought in at the cutover ``on``: cost and accumulated depreciation as at that
    day and the life left. Depreciation starts the month after."""
    on: date
    cost: Decimal
    accumulated: Decimal
    life_months: int


Movement = Acquire | Improve | EstimateChange | Dispose | Opening


@dataclass(frozen=True)
class Policy:
    method: str
    life_months: int
    residual: Decimal
    convention: str
    fiscal_year_start: int = 1
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MissingReading:
    period: str


@dataclass(frozen=True)
class PeriodAmount:
    period: str
    amount: Decimal
    cumulative: Decimal
    exception: MissingReading | None = None


@dataclass
class Layer:
    """One layer: ``basis`` to depreciate down to ``residual`` over ``life``, from month
    index ``start``; ``fractions`` is the life each month has consumed so far."""
    basis: Decimal
    residual: Decimal
    life: Decimal
    start: int
    weights: list[Decimal]
    fractions: list[Decimal] = field(default_factory=list)

    @property
    def position(self) -> Decimal:
        return sum(self.fractions, _ZERO)

    def normal_fraction(self, index: int) -> Decimal:
        offset = index - self.start
        return self.weights[offset] if offset < len(self.weights) else _ONE

    def end(self, index: int) -> tuple[int, Decimal]:
        """The month this layer's life runs out, consuming normally from ``index``, and
        the fraction of that month it consumes."""
        left = self.life - self.position
        while True:
            fraction = min(self.normal_fraction(index), left)
            left -= fraction
            if left <= 0:
                return index, fraction
            index += 1


@dataclass(frozen=True)
class Schedule:
    periods: list[PeriodAmount]
    layers: list[Layer]


def _charges(layer: Layer, method, params: dict) -> list[Decimal]:
    return method.amounts(MethodInputs(layer.basis, layer.residual, layer.life, layer.fractions, params))


def _carrying(layers: list[Layer], method, params: dict) -> Decimal:
    return sum((ly.basis + ly.residual - sum(_charges(ly, method, params), _ZERO) for ly in layers), _ZERO)


def _keep_end_life(layers: list[Layer], index: int, weights: list[Decimal]) -> Decimal:
    """The life an improvement starting at ``index`` needs to end with the asset."""
    ends = [ly.end(index) for ly in layers if ly.life > ly.position]
    if not ends:
        return _ZERO
    end, last = max(ends)
    life = _ZERO
    for i in range(index, end + 1):
        offset = i - index
        weight = weights[offset] if offset < len(weights) else _ONE
        life += min(weight, last) if i == end else weight
    return life


def _first_month(movement: Movement) -> int:
    return month_index(movement.on) + (1 if isinstance(movement, Opening) else 0)


def schedule(movements: list[Movement], policy: Policy, *, through: str, currency: str,
             readings: dict[str, Decimal] | None = None) -> Schedule:
    """Monthly depreciation from the first month in service through ``through``
    (``YYYY-MM``), ending with the month of disposal."""
    method = METHODS[policy.method]
    convention = CONVENTIONS[policy.convention]
    params = dict(policy.params)
    life = Decimal(str(params.get("total_units", 0))) if method.uses_readings else Decimal(policy.life_months)
    readings = readings or {}
    last = parse_period(through)
    pending = sorted(movements, key=lambda m: (_first_month(m), m.on, isinstance(m, Dispose)))
    starts = [_first_month(m) for m in pending if not isinstance(m, Dispose)]
    if not starts:
        return Schedule([], [])
    layers: list[Layer] = []
    retired: list[Layer] = []
    months: list[tuple[int, MissingReading | None]] = []
    with localcontext() as ctx:
        ctx.prec = 40
        index = min(starts)
        while index <= last:
            disposal = None
            while pending and _first_month(pending[0]) <= index:
                m = pending.pop(0)
                if isinstance(m, Acquire):
                    layers.append(Layer(m.amount - policy.residual, policy.residual, life, index,
                                        convention.start_weights(m.on, policy.fiscal_year_start)))
                elif isinstance(m, Opening):
                    layers.append(Layer(m.cost - m.accumulated - policy.residual, policy.residual,
                                        Decimal(m.life_months), index, [_ONE]))
                elif isinstance(m, Improve):
                    weights = convention.start_weights(m.on, policy.fiscal_year_start)
                    layer_life = (Decimal(m.life_months) if m.life_months is not None
                                  else _keep_end_life(layers, index, weights))
                    layers.append(Layer(m.amount, _ZERO, layer_life, index, weights))
                elif isinstance(m, EstimateChange):
                    carrying = _carrying(layers, method, params)
                    retired.extend(layers)
                    layers = [Layer(carrying - m.residual, m.residual, Decimal(m.life_months), index, [_ONE])]
                else:
                    disposal = m.on
            exception = None
            reading = None
            if method.uses_readings:
                reading = readings.get(period_label(index))
                if reading is None:
                    exception = MissingReading(period_label(index))
            fy_start = fiscal_year_start_index(index, policy.fiscal_year_start)
            for layer in layers:
                remaining = layer.life - layer.position
                if method.uses_readings:
                    fraction = min(reading or _ZERO, remaining)
                else:
                    fraction = min(layer.normal_fraction(index), remaining)
                    if disposal is not None:
                        fy_consumed = sum(layer.fractions[max(fy_start - layer.start, 0):], _ZERO)
                        fraction = convention.disposal(fraction, disposal, fy_consumed, remaining)
                layer.fractions.append(max(fraction, -layer.position))
            months.append((index, exception))
            if disposal is not None:
                break
            index += 1
        by_month: dict[int, Decimal] = {}
        for layer in (*retired, *layers):
            for offset, charge in enumerate(_charges(layer, method, params)):
                by_month[layer.start + offset] = by_month.get(layer.start + offset, _ZERO) + charge
        periods, cumulative, posted = [], _ZERO, round_money(0, currency)
        for index, exception in months:
            cumulative += by_month.get(index, _ZERO)
            rounded = round_money(cumulative, currency)
            periods.append(PeriodAmount(period_label(index), rounded - posted, cumulative, exception))
            posted = rounded
    return Schedule(periods, layers)
