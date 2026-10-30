# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every merge keeps the books and never turns an unknown cost or price into a number.

Lots of every kind (with or without stock, a known goods cost, value of unknown goods cost,
no cost, a unit cost waiting for stock, value with no units, freight from one bill or two,
selling prices set, missing, under a list's raw name, under a derived list's key, or not a
number) are merged in a fixed pseudo-random sweep, with or without a hand-set resulting
quantity. After each merge:

1. the merged lot's recorded value is the sum of its sources';
2. its freight is the sources' freight summed per bill, and its goods cost is the value less it;
3. each lot account holds the same as before, tied to the lots on it;
4. each selling unit price lies between the stocked sources' lowest and highest, equals it
   when they agree, sits at the rate ceiling, or is absent; no derived list key is stored;
5. its goods cost is known exactly when every source holding something had a known one;
6. whether the goods cost is known is the lot's own field, never an attribute.
"""
from __future__ import annotations

import random
import uuid
from collections import Counter
from decimal import Decimal

import pytest
from celerp_inventory.routes import flatten_item
from celerp_inventory.services import goods_cost_empty, goods_cost_known

from celerp.services.lot_origin import recorded_value
from celerp.services.money import round_basis, round_rate
from celerp.services.pricing import coerce_price
from stock_books import assert_books_carry_stock, assert_settled
from test_cost_restatement import _state
from test_import_invariants import _set_company_settings
from test_landed_cost_pools import _po_into
from test_landed_cost_removals import _seed
from test_merge_unknown_cost import MARK, _item, _merge

pytestmark = pytest.mark.asyncio

_LISTS = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"},
          {"name": "Trade", "base": "Retail", "multiplier": 1.2}]
_PRICE_KEYS = ("retail_price", "wholesale_price")
_COSTS = ("known", "unknown_value", "no_cost", "negative", "waiting", "zero_marked_value", "zero_marked_nothing",
          "landed_only")
_STOCKED = ("known", "unknown_value", "no_cost")
_PRICES = ("set", "missing", "raw_name", "derived_leftover", "not_a_number")
_RESULTING = (None, "zero", "equal", "lower", "higher")


async def _patch(client, auth, lot: str, **fields) -> None:
    r = await client.patch(f"/items/{lot}", headers=auth["headers"],
                           json={"fields_changed": {k: {"new": v} for k, v in fields.items()}})
    assert r.status_code == 200, r.text


async def _stored(session, auth, lot: str, data: dict) -> None:
    """Data an older release or import left on the lot, as its ledger holds it."""
    from celerp.events.engine import emit_event
    await emit_event(session, company_id=auth["company_id"], entity_id=lot, entity_type="item",
                     event_type="item.patched", data=data, actor_id=None, location_id=None, source="test",
                     idempotency_key=f"sweep:{uuid.uuid4()}", metadata_={})
    await session.commit()


async def _lot(client, session, auth, rng: random.Random, cost: str, pools: int, price: str) -> str:
    qty = rng.randint(1, 6)
    unit = round(rng.uniform(1, 30), rng.choice((0, 2, 4)))
    priced = {"set": {"retail_price": unit, "wholesale_price": round(unit * 0.8, 2)},
              "raw_name": {"attributes": {"Retail": unit}},
              "derived_leftover": {"retail_price": unit}}.get(price, {})
    if cost in ("known", "negative", "landed_only"):
        lot = await _item(client, auth, qty=qty, cost_total=round(qty * rng.uniform(1, 9), 2), **priced)
    elif cost == "waiting":
        lot = await _item(client, auth, qty=0, cost_price=rng.choice((4.0, 5.0)), **priced)
    elif cost == "no_cost":
        lot = await _item(client, auth, qty=qty, **priced)
    else:  # value whose goods cost is not known: a known part merged with a part without cost
        known = await _item(client, auth, qty=1, cost_total=0.0 if cost == "zero_marked_nothing" else 10.0)
        lot = await _merge(client, auth, [known, await _item(client, auth, qty=qty, **priced)])
    for i in range(pools if cost in (*_STOCKED, "landed_only") else 0):
        await _po_into(client, auth, lot, 1 + i, 2.0, shipping=10.0 * (i + 1))
    if cost == "landed_only":
        st = await _state(session, auth, lot)
        await _patch(client, auth, lot, cost_total=sum(st.get("landed_costs", {}).values()) or None)
    if cost in ("zero_marked_value", "zero_marked_nothing", "landed_only"):
        await _patch(client, auth, lot, quantity=0)
    if cost == "negative":
        await _stored(session, auth, lot, {"quantity": -2})
    if price == "derived_leftover":
        await _stored(session, auth, lot, {"trade_price": round(unit * 1.5, 2)})
    if price == "not_a_number":
        await _stored(session, auth, lot, {"retail_price": "n/a"})
    return lot


def _pools(states) -> dict[str, float]:
    total: Counter = Counter()
    for st in states:
        total.update({k: Decimal(str(v)) for k, v in (st.get("landed_costs") or {}).items()})
    return {k: float(round_basis(v)) for k, v in total.items() if v}


def _check_prices(sources: list[dict], merged: dict, currency: str) -> None:
    stocked = [s for s in sources if float(s.get("quantity") or 0) > 0] or sources
    assert "trade_price" not in merged and "Trade" not in (merged.get("attributes") or {}), merged
    for key in _PRICE_KEYS:
        if key not in merged:
            continue
        got = merged[key]
        units = [coerce_price(s.get(key)) for s in stocked]
        assert None not in units, (key, units, got)
        assert float(round_rate(got, currency)) == got, (key, got)
        if len(set(units)) == 1:
            assert got == units[0], (key, units, got)
        else:
            assert float(round_rate(min(units), currency)) <= got <= float(round_rate(max(units), currency)), (
                key, units, got)


async def test_every_merge_keeps_value_freight_books_and_unknowns(client, session, auth):
    from celerp.services.auto_je import company_currency

    await _seed(session, auth)
    await _set_company_settings(session, auth["company_id"], price_lists=_LISTS)
    currency = await company_currency(session, auth["company_id"])
    rng = random.Random(20261030)
    kinds = [(c, p, pr) for c in _COSTS for p in (0, 1, 2) for pr in _PRICES]
    rng.shuffle(kinds)
    seen: Counter = Counter()
    for n in range(28):
        picked = [kinds.pop() for _ in range(rng.choice((2, 2, 3)))]
        ids = [await _lot(client, session, auth, rng, *kind) for kind in picked]
        before = [await _state(session, auth, i) for i in ids]
        flats = [flatten_item(st, i) for st, i in zip(before, ids)]
        natural = sum(max(float(st.get("quantity") or 0), 0) for st in before)
        resulting = rng.choice(_RESULTING)
        extra = {} if resulting is None else {"resulting_quantity": {
            "zero": 0, "equal": natural, "lower": max(natural - 1, 0), "higher": natural + 2}[resulting]}
        books = await assert_books_carry_stock(session, auth["company_id"])
        merged_id = await _merge(client, auth, ids, **extra)
        merged = await _state(session, auth, merged_id)
        step = (n, picked, resulting, before, merged)

        value = round_basis(sum(recorded_value(st) for st in before))
        assert round_basis(recorded_value(merged)) == value, step
        pools = _pools(before)
        assert (merged.get("landed_costs") or {}) == pools, step
        if value or MARK in merged or "cost_total" in merged:
            assert round_basis(merged.get("cost_base") or 0) == round_basis(value - sum(pools.values(), 0.0)), step
        assert await assert_books_carry_stock(session, auth["company_id"]) == books, step
        _check_prices(flats, merged, currency)
        holding = [st for st in before if not goods_cost_empty(st)]
        if holding:
            assert goods_cost_known(merged) == all(goods_cost_known(st) for st in holding), step
        assert merged.get(MARK, True) is True and MARK not in (merged.get("attributes") or {}), step
        seen.update(c for c, _, _ in picked)
    await assert_settled(client, session, auth)
    assert set(seen) == set(_COSTS), seen
