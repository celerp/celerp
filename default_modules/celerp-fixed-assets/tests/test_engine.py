# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The depreciation engine on a dated movement sequence.

The worked example: cost 12,000, residual 0, straight line over 60 months, available
for use 2026-03-17; an improvement of 1,800 on 2026-09-17 keeping the end date; an
estimate change on 2027-01-01 to 24 months remaining; sold 2027-08-10 for proceeds of
9,000; fiscal year January to December. Every convention, month by month.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from celerp_fixed_assets.calc import (
    Acquire,
    Dispose,
    EstimateChange,
    Improve,
    MissingReading,
    Opening,
    Policy,
    schedule,
)
from celerp_fixed_assets.calc.rules import CONVENTIONS, METHODS

D = Decimal


def _amounts(values) -> list[Decimal]:
    return [D(str(v)) for v in values]


WORKED = {
    "full_month": _amounts(
        [200] * 6 + ["233.33", "233.34", "233.33", "233.33"]
        + ["486.11", "486.12"] + ["486.11"] * 5 + ["0.00"]),
    "mid_month": _amounts(
        [100] + [200] * 5 + ["216.67", "233.33", "233.33", "233.34"]
        + ["490.97", "490.97", "490.97", "490.98", "490.97", "490.97", "490.97"] + ["245.49"]),
    "daily": _amounts(
        ["96.77"] + [200] * 5 + ["215.56", "233.35", "233.34", "233.35"]
        + ["491.15"] * 6 + ["491.16"] + ["158.43"]),
    "half_year": _amounts(
        [120] * 6 + [165] * 4 + ["517.50"] * 7 + ["-517.50"]),
}
TOTALS = {"full_month": "5536.11", "mid_month": "5698.96", "daily": "5608.86", "half_year": "4485.00"}
GAINS = {"full_month": "736.11", "mid_month": "898.96", "daily": "808.86", "half_year": "-315.00"}
LAYER_LIVES = {"full_month": "54", "mid_month": "54", "daily": "53.9828", "half_year": "60"}

MONTHS = [f"2026-{m:02d}" for m in range(3, 13)] + [f"2027-{m:02d}" for m in range(1, 9)]


def _worked(convention: str, *, through: str = "2027-08"):
    movements = [
        Acquire(date(2026, 3, 17), D(12000)),
        Improve(date(2026, 9, 17), D(1800)),
        EstimateChange(date(2027, 1, 1), life_months=24, residual=D(0)),
        Dispose(date(2027, 8, 10)),
    ]
    policy = Policy(method="straight_line", life_months=60, residual=D(0), convention=convention)
    return schedule(movements, policy, through=through, currency="USD")


@pytest.mark.parametrize("convention", sorted(WORKED))
def test_engine_worked_example_all_conventions(convention):
    result = _worked(convention)
    assert [p.period for p in result.periods] == MONTHS
    assert [p.amount for p in result.periods] == WORKED[convention]
    total = sum((p.amount for p in result.periods), D(0))
    assert total == D(TOTALS[convention])
    carrying = D(13800) - total
    assert D(9000) - carrying == D(GAINS[convention])


@pytest.mark.parametrize("convention", sorted(WORKED))
def test_keep_end_date_layer_life_per_convention(convention):
    result = _worked(convention, through="2026-12")
    acquisition, improvement = result.layers
    assert acquisition.life == 60
    assert improvement.life.quantize(D("0.0001")) == D(LAYER_LIVES[convention])


def test_a_new_remaining_life_counts_from_the_improvement():
    policy = Policy(method="straight_line", life_months=60, residual=D(0), convention="full_month")
    result = schedule([Acquire(date(2026, 1, 1), D(6000)), Improve(date(2026, 7, 1), D(1200), life_months=12)],
                      policy, through="2027-12", currency="USD")
    assert result.layers[1].life == 12
    assert [p.amount for p in result.periods[6:18]] == [D("200.00")] * 12
    assert result.periods[18].amount == D("100.00")


def test_s2_available_for_use_starts_depreciation():
    """Nothing before the available-for-use month; months the asset stands idle after it
    are still charged."""
    policy = Policy(method="straight_line", life_months=10, residual=D(0), convention="full_month")
    result = schedule([Acquire(date(2026, 5, 1), D(1000))], policy, through="2026-08", currency="USD")
    assert [(p.period, p.amount) for p in result.periods] == [
        ("2026-05", D("100.00")), ("2026-06", D("100.00")), ("2026-07", D("100.00")), ("2026-08", D("100.00"))]
    assert schedule([Acquire(date(2026, 9, 1), D(1000))], policy, through="2026-08", currency="USD").periods == []


@pytest.mark.parametrize("currency,cost,residual,places", [
    ("JPY", "1000003", "7", 0),
    ("USD", "12345.67", "0.01", 2),
    ("KWD", "1234.567", "0.001", 3),
])
@pytest.mark.parametrize("convention", sorted(CONVENTIONS))
def test_invariant_total_depreciation_exact(currency, cost, residual, places, convention):
    """Cumulative rounding lands the total exactly on the depreciable amount, at every
    currency precision and under every convention."""
    policy = Policy(method="straight_line", life_months=37, residual=D(residual), convention=convention)
    result = schedule([Acquire(date(2026, 2, 13), D(cost))], policy, through="2030-12", currency=currency)
    assert sum((p.amount for p in result.periods), D(0)) == D(cost) - D(residual)
    assert all(-p.amount.as_tuple().exponent == places for p in result.periods)


def test_declining_balance_switches_to_straight_line_and_ends_at_residual():
    policy = Policy(method="declining_balance", life_months=60, residual=D(1000), convention="full_month",
                    params={"factor": "2"})
    result = schedule([Acquire(date(2026, 1, 1), D(10000))], policy, through="2031-06", currency="USD")
    amounts = [p.amount for p in result.periods]
    assert amounts[0] == D("333.33")
    assert amounts[0] > amounts[11] > amounts[30]
    tail = amounts[48:60]
    assert max(tail) - min(tail) <= D("0.01")
    assert sum(amounts, D(0)) == D(9000)
    assert len([a for a in amounts if a]) == 60


def test_declining_balance_without_switch_writes_down_to_residual_at_the_end():
    policy = Policy(method="declining_balance", life_months=24, residual=D(0), convention="full_month",
                    params={"factor": "1.5", "switch_to_straight_line": False})
    result = schedule([Acquire(date(2026, 1, 1), D(2400))], policy, through="2027-12", currency="USD")
    amounts = [p.amount for p in result.periods]
    assert amounts[0] == D("150.00")
    assert amounts[-1] > amounts[-2]
    assert sum(amounts, D(0)) == D(2400)


def test_sum_of_years_digits_by_year_of_life():
    policy = Policy(method="sum_of_years_digits", life_months=60, residual=D(0), convention="full_month")
    result = schedule([Acquire(date(2026, 1, 1), D(15000))], policy, through="2030-12", currency="USD")
    years = [sum((p.amount for p in result.periods[i:i + 12]), D(0)) for i in range(0, 60, 12)]
    assert years == [D(5000), D(4000), D(3000), D(2000), D(1000)]


def test_units_of_use_needs_a_reading_every_month():
    """A month without a reading is an exception and is never estimated; the months
    around it are charged by their readings."""
    policy = Policy(method="units_of_use", life_months=0, residual=D(0), convention="full_month",
                    params={"total_units": "1000"})
    readings = {"2026-01": D(100), "2026-03": D(50)}
    result = schedule([Acquire(date(2026, 1, 1), D(5000))], policy, through="2026-03", currency="USD",
                      readings=readings)
    assert [(p.period, p.amount, p.exception) for p in result.periods] == [
        ("2026-01", D("500.00"), None),
        ("2026-02", D("0.00"), MissingReading("2026-02")),
        ("2026-03", D("250.00"), None),
    ]


def test_an_opening_balance_depreciates_its_carrying_amount_from_the_next_month():
    policy = Policy(method="straight_line", life_months=60, residual=D(0), convention="mid_month")
    result = schedule([Opening(date(2025, 12, 31), cost=D(12000), accumulated=D(4800), life_months=36)],
                      policy, through="2026-03", currency="USD")
    assert [(p.period, p.amount) for p in result.periods] == [
        ("2026-01", D("200.00")), ("2026-02", D("200.00")), ("2026-03", D("200.00"))]


def test_land_and_indefinite_life_never_depreciate():
    for policy in (Policy(method="none", life_months=0, residual=D(0), convention="full_month"),):
        result = schedule([Acquire(date(2026, 1, 1), D(5000))], policy, through="2026-06", currency="USD")
        assert {p.amount for p in result.periods} == {D("0.00")}


def test_the_registries_name_every_core_method_and_convention():
    assert set(METHODS) == {"straight_line", "declining_balance", "sum_of_years_digits", "units_of_use", "none"}
    assert set(CONVENTIONS) == {"full_month", "mid_month", "daily", "half_year"}
    for entry in (*METHODS.values(), *CONVENTIONS.values()):
        assert entry.label_key.startswith("fixed_assets.")
