# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Depreciation on a dated movement sequence. Pure: no I/O, ``Decimal`` throughout.

An asset is a set of layers: the acquisition (or opening balance) and each
improvement. Each layer depreciates its own amount over its own life, consumed month
by month under the timing convention. An estimate change merges the layers into one:
the carrying amount less the new residual over the new remaining life, from the month
of the change. A revaluation merges them the same way at the fair value, over the life
left. An impairment, or its reversal, restates each layer pro rata to its carrying
amount and keeps the layers apart. Both are month-end measurements: they take effect
from the month after their date. An estimate change may also change the method, its parameters and the
timing convention from then on; every layer depreciates under the method it was laid
down with. A partial disposal splits every layer: the share disposed of takes the
convention's part of the disposal month and stops, the rest depreciates on.

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
from celerp_fixed_assets.calc import rules

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
    """From the month of ``on``: the carrying amount less ``residual`` over the life left
    (months, or units for a method driven by readings), optionally under a new method,
    parameters or convention."""
    on: date
    life_months: Decimal | int
    residual: Decimal
    method: str | None = None
    params: dict | None = None
    convention: str | None = None


@dataclass(frozen=True)
class Dispose:
    """Out of service on ``on``. A ``share`` below one disposes of that share of what is
    left; the rest depreciates on."""
    on: date
    share: Decimal = _ONE


@dataclass(frozen=True)
class Opening:
    """Brought in at the cutover ``on``: cost and accumulated depreciation as at that
    day and the life left. Depreciation starts the month after."""
    on: date
    cost: Decimal
    accumulated: Decimal
    life_months: int


@dataclass(frozen=True)
class Revalue:
    """Measured at fair value on ``on``: one layer of ``fair_value`` over the life left."""
    on: date
    fair_value: Decimal


@dataclass(frozen=True)
class Impair:
    """Carrying amount restated to ``carrying`` on ``on`` (down for an impairment, up for
    its reversal), spread over the layers by their carrying amounts."""
    on: date
    carrying: Decimal


Movement = Acquire | Improve | EstimateChange | Dispose | Opening | Revalue | Impair

_MONTH_END = (Opening, Revalue, Impair)


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
    method: object
    params: dict
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


    def split(self, share: Decimal) -> Layer:
        """Take ``share`` of this layer out as a layer of its own, with the same history;
        this layer keeps the rest. Every method scales with basis and residual."""
        gone = Layer(self.basis * share, self.residual * share, self.life, self.start, self.weights, self.method,
                     self.params, list(self.fractions))
        self.basis -= gone.basis
        self.residual -= gone.residual
        return gone


@dataclass(frozen=True)
class Schedule:
    """``parted`` is what each partial disposal's share was charged in the month it left,
    in date order."""
    periods: list[PeriodAmount]
    layers: list[Layer]
    parted: list[Decimal] = field(default_factory=list)


def _charges(layer: Layer) -> list[Decimal]:
    return layer.method.amounts(MethodInputs(layer.basis, layer.residual, layer.life, layer.fractions, layer.params))


def _carrying(layers: list[Layer]) -> Decimal:
    return sum((_carrying_of(ly) for ly in layers), _ZERO)


def _life(method, policy_life, params: dict) -> Decimal:
    return Decimal(str(params.get("total_units", 0))) if method.uses_readings else Decimal(policy_life)


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


def _left(layer: Layer) -> Decimal:
    return max(layer.life - layer.position, _ZERO)


def _carrying_of(layer: Layer) -> Decimal:
    return layer.basis + layer.residual - sum(_charges(layer), _ZERO)


def _merged(layers: list[Layer], value: Decimal, index: int, method, params: dict) -> Layer:
    """One layer carrying ``value`` over the longest life left among ``layers``."""
    residual = min(sum((ly.residual for ly in layers), _ZERO), value)
    life = max((_left(ly) for ly in layers), default=_ZERO)
    return Layer(value - residual, residual, life, index, [_ONE], method, params)


def _restated(layers: list[Layer], carrying: Decimal, index: int, method, params: dict) -> list[Layer]:
    """Each layer at its share of ``carrying``, pro rata to its carrying amount now."""
    now = [_carrying_of(ly) for ly in layers]
    total = sum(now, _ZERO)
    if total <= 0:
        return [_merged(layers, carrying, index, method, params)]
    out = []
    for ly, value in zip(layers, now):
        value = carrying * value / total
        residual = min(ly.residual, value)
        out.append(Layer(value - residual, residual, _left(ly), index, [_ONE], ly.method, ly.params))
    return out


def _first_month(movement: Movement) -> int:
    return month_index(movement.on) + (1 if isinstance(movement, _MONTH_END) else 0)


def _consume(layer: Layer, index: int, reading: Decimal | None, convention, fy_start: int,
             disposal: date | None) -> None:
    """Append the life ``layer`` consumes in month ``index``; in the month it is disposed
    of, the convention's share of that month."""
    remaining = layer.life - layer.position
    if layer.method.uses_readings:
        fraction = min(reading or _ZERO, remaining)
    else:
        fraction = min(layer.normal_fraction(index), remaining)
        if disposal is not None:
            fy_consumed = sum(layer.fractions[max(fy_start - layer.start, 0):], _ZERO)
            fraction = convention.disposal(fraction, disposal, fy_consumed, remaining)
    layer.fractions.append(max(fraction, -layer.position))


def schedule(movements: list[Movement], policy: Policy, *, through: str, currency: str,
             readings: dict[str, Decimal] | None = None) -> Schedule:
    """Monthly depreciation from the first month in service through ``through``
    (``YYYY-MM``), ending with the month of disposal."""
    method = rules.method(policy.method)
    convention = rules.convention(policy.convention)
    params = dict(policy.params)
    life = _life(method, policy.life_months, params)
    readings = readings or {}
    last = parse_period(through)
    pending = sorted(movements, key=lambda m: (_first_month(m), m.on, isinstance(m, Dispose)))
    starts = [_first_month(m) for m in pending if not isinstance(m, Dispose)]
    if not starts:
        return Schedule([], [])
    layers: list[Layer] = []
    shares: list[tuple[int, list[Layer]]] = []
    retired: list[Layer] = []
    months: list[tuple[int, MissingReading | None]] = []
    with localcontext() as ctx:
        ctx.prec = 40
        index = min(starts)
        while index <= last:
            disposal = None
            leaving: list[tuple[Layer, date]] = []
            while pending and _first_month(pending[0]) <= index:
                m = pending.pop(0)
                if isinstance(m, Acquire):
                    layers.append(Layer(m.amount - policy.residual, policy.residual, life, index,
                                        convention.start_weights(m.on, policy.fiscal_year_start), method, params))
                elif isinstance(m, Opening):
                    layers.append(Layer(m.cost - m.accumulated - policy.residual, policy.residual,
                                        Decimal(m.life_months), index, [_ONE], method, params))
                elif isinstance(m, Improve):
                    weights = convention.start_weights(m.on, policy.fiscal_year_start)
                    layer_life = (Decimal(m.life_months) if m.life_months is not None
                                  else _keep_end_life(layers, index, weights))
                    layers.append(Layer(m.amount, _ZERO, layer_life, index, weights, method, params))
                elif isinstance(m, EstimateChange):
                    carrying = _carrying(layers)
                    retired.extend(layers)
                    if m.method is not None:
                        method = rules.method(m.method)
                    if m.params is not None:
                        params = dict(m.params)
                    if m.convention is not None:
                        convention = rules.convention(m.convention)
                    life = _life(method, policy.life_months, params)
                    layers = [Layer(carrying - m.residual, m.residual, Decimal(str(m.life_months)), index, [_ONE],
                                    method, params)]
                elif isinstance(m, Revalue):
                    retired.extend(layers)
                    layers = [_merged(layers, m.fair_value, index, method, params)]
                elif isinstance(m, Impair):
                    retired.extend(layers)
                    layers = _restated(layers, m.carrying, index, method, params)
                elif m.share < _ONE:
                    gone = [ly.split(m.share) for ly in layers]
                    shares.append((index, gone))
                    leaving.extend((ly, m.on) for ly in gone)
                else:
                    disposal = m.on
            exception = None
            reading = None
            if any(ly.method.uses_readings for ly in (*layers, *(ly for ly, _ in leaving))):
                reading = readings.get(period_label(index))
                if reading is None:
                    exception = MissingReading(period_label(index))
            fy_start = fiscal_year_start_index(index, policy.fiscal_year_start)
            for layer in layers:
                _consume(layer, index, reading, convention, fy_start, disposal)
            for layer, on in leaving:
                _consume(layer, index, reading, convention, fy_start, on)
                retired.append(layer)
            months.append((index, exception))
            if disposal is not None:
                break
            index += 1
        by_month: dict[int, Decimal] = {}
        for layer in (*retired, *layers):
            for offset, charge in enumerate(_charges(layer)):
                by_month[layer.start + offset] = by_month.get(layer.start + offset, _ZERO) + charge
        periods, cumulative, posted = [], _ZERO, round_money(0, currency)
        for index, exception in months:
            cumulative += by_month.get(index, _ZERO)
            rounded = round_money(cumulative, currency)
            periods.append(PeriodAmount(period_label(index), rounded - posted, cumulative, exception))
            posted = rounded
        parted_charges = [sum((sum(_charges(ly)[at - ly.start:], _ZERO) for ly in gone), _ZERO)
                          for at, gone in shares]
    return Schedule(periods, layers, parted_charges)
