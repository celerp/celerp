# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot whose goods cost is not known stays so through everything done to it, until a cost is set.

Three such lots: one merged from a part of known cost and a part without cost, one whose goods
cost was cleared while it carried freight, and one without cost that priced goods were received
into. Each goes through one of the things done to stock: a rename, a count up or down, a count
to zero and back, a split, a sale of part or all of it, a write-off, a return to the supplier, a
customer's return, a transform, undoing the merge or the receipt, setting its cost, correcting
the cost of a merged part in either order, setting the merged lot's cost before a part's is
corrected, or merging it again. After each, the books carry exactly the stock, whether its goods
cost is known is as the operation leaves it, and rebuilding the company from its events gives the
same states. Its backup restores as a new company with the same states."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.account_roles import AccountRole, resolve
from company_backup_support import download
from migration_support import maker, real_client, real_engine  # noqa: F401
from stock_books import assert_settled
from test_company_backup import _BK_UUID, _bk_local, _bk_restore_new, _bk_setup
from test_cost_restatement import _state
from test_helpers import provision_company_books
from test_landed_cost_merge_lineage import _merge, _transform
from test_landed_cost_pools import _po_into, _post, _sell
from test_landed_cost_removals import _held, _split, _writeoff
from test_merge_unknown_cost import MARK, _item, _set_cost
from test_receipt_accounting import _doc, _receive, _return

pytestmark = pytest.mark.asyncio


class _Lot:
    """The lot of unknown goods cost, its merged parts, the bill or order that brought goods into it
    and the value its lot accounts carry for goods billed and not received."""

    def __init__(self, lot: str, parts: tuple[str, str] | None = None, doc: str | None = None):
        self.lot, self.parts, self.doc = lot, parts, doc
        self.in_transit: dict[str, Decimal] = {}


async def _merged(c, s, auth) -> _Lot:
    a = await _item(c, auth, qty=2, cost_total=100.0)
    b = await _item(c, auth, qty=3)
    return _Lot(await _merge(c, auth, [a, b]), parts=(a, b))


async def _cleared(c, s, auth) -> _Lot:
    lot = await _item(c, auth, qty=1, cost_total=10.0)
    doc = await _po_into(c, auth, lot, 9, 10.0, shipping=30.0)
    await _set_cost(c, auth, lot, None)
    return _Lot(lot, doc=doc)


async def _topped(c, s, auth) -> _Lot:
    """3 units without cost and 2 received at 10.00 on an order, then billed."""
    lot = await _item(c, auth, qty=3)
    return _Lot(lot, doc=await _po_into(c, auth, lot, 2, 10.0))


_ORIGINS = {"merged": _merged, "cleared": _cleared, "topped": _topped}


async def _adjust(c, auth, lot: str, *qtys: float) -> None:
    for qty in qtys:
        r = await c.post(f"/items/{lot}/adjust", headers=auth["headers"], json={"new_qty": qty})
        assert r.status_code == 200, r.text


async def _qty(s, auth, lot: str) -> float:
    return float((await _state(s, auth, lot))["quantity"])


# Each operation returns the lots it leaves and whether each one's goods cost is then known
# (None: nothing left on hand to say it of).

async def _rename(c, s, auth, x: _Lot):
    r = await c.patch(f"/items/{x.lot}", headers=auth["headers"], json={"fields_changed": {"name": {"new": "Renamed"}}})
    assert r.status_code == 200, r.text
    return {x.lot: False}


async def _count_up(c, s, auth, x: _Lot):
    await _adjust(c, auth, x.lot, await _qty(s, auth, x.lot) + 2)
    return {x.lot: False}


async def _count_down(c, s, auth, x: _Lot):
    await _adjust(c, auth, x.lot, await _qty(s, auth, x.lot) - 1)
    return {x.lot: False}


async def _zero_and_back(c, s, auth, x: _Lot):
    await _adjust(c, auth, x.lot, 0, 4)
    return {x.lot: False}


async def _split_off(c, s, auth, x: _Lot):
    [part] = await _split(c, auth, x.lot, 1)
    return {x.lot: False, part: False}


async def _partial_sale(c, s, auth, x: _Lot):
    await _sell(c, auth, x.lot, 1)
    return {await _held(s, auth, x.lot): False}


async def _full_sale(c, s, auth, x: _Lot):
    await _sell(c, auth, x.lot, await _qty(s, auth, x.lot))
    return {x.lot: None}


async def _write_off(c, s, auth, x: _Lot):
    await _writeoff(c, auth, x.lot, 1)
    return {x.lot: False}


async def _supplier_return(c, s, auth, x: _Lot):
    r = await _return(c, auth, x.doc, x.lot, 1)
    assert r.status_code == 200, r.text
    return {x.lot: False}


async def _customer_return(c, s, auth, x: _Lot):
    inv = await _sell(c, auth, x.lot, 2)
    r = await c.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": inv, "total": 50.0, "line_items": [
            {"entity_id": x.lot, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await _post(c, auth, cn, "finalize")).status_code == 200
    r = await c.post(f"/docs/{cn}/receive-return", headers=auth["headers"], json={
        "items": [{"sku": (await _state(s, auth, x.lot))["sku"], "quantity": 1}]})
    assert r.status_code == 200, r.text
    back = r.json()["received_items"][0]["item_id"]
    return {await _held(s, auth, x.lot): False, back: False}


async def _transform_it(c, s, auth, x: _Lot):
    return {await _transform(c, auth, x.lot): False}


async def _undo_merge(c, s, auth, x: _Lot):
    r = await c.post(f"/items/{x.lot}/undo-merge", headers=auth["headers"])
    assert r.status_code == 200, r.text
    a, b = x.parts
    return {a: True, b: False}


async def _undo_receipt(c, s, auth, x: _Lot):
    r = await c.delete(f"/docs/{x.doc}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    # The bill still books its 2 at 10.00, now as goods billed and not received.
    x.in_transit = {await resolve(s, auth["company_id"], AccountRole.INVENTORY_PURCHASED): Decimal("20.00")}
    st = await _state(s, auth, x.lot)
    # The undo keeps the mark the receipt left: the lot's goods cost stays unknown, its value 0.
    assert st[MARK] is True and not st.get("cost_total"), st
    return {x.lot: False}


async def _cost_set(c, s, auth, x: _Lot):
    await _set_cost(c, auth, x.lot, 77.0)
    return {x.lot: True}


async def _parts_known_first(c, s, auth, x: _Lot):
    a, b = x.parts
    await _set_cost(c, auth, a, 110.0)
    assert (await _state(s, auth, x.lot))[MARK] is True
    await _set_cost(c, auth, b, 60.0)
    return {x.lot: True}


async def _parts_unknown_first(c, s, auth, x: _Lot):
    a, b = x.parts
    await _set_cost(c, auth, b, 60.0)
    await _set_cost(c, auth, a, 110.0)
    return {x.lot: True}


async def _result_then_part(c, s, auth, x: _Lot):
    await _set_cost(c, auth, x.lot, 150.0)
    await _set_cost(c, auth, x.parts[0], 110.0)
    st = await _state(s, auth, x.lot)
    assert st["cost_total"] == 160.0, st
    return {x.lot: True}


async def _merged_again(c, s, auth, x: _Lot):
    sku = (await _state(s, auth, x.lot))["sku"]
    other = await _item(c, auth, qty=1, cost_total=10.0, sku=sku)
    y = await _merge(c, auth, [x.lot, other])
    if x.parts is None:
        return {y: False}
    assert (await _state(s, auth, y))[MARK] is True
    await _set_cost(c, auth, x.parts[1], 60.0)
    return {y: True}


_OPS = {f.__name__.lstrip("_"): f for f in (
    _rename, _count_up, _count_down, _zero_and_back, _split_off, _partial_sale, _full_sale, _write_off,
    _supplier_return, _customer_return, _transform_it, _undo_merge, _undo_receipt, _cost_set, _parts_known_first,
    _parts_unknown_first, _result_then_part, _merged_again)}
_ONLY = {"supplier_return": ("cleared",), "undo_receipt": ("topped",), "undo_merge": ("merged",),
         "parts_known_first": ("merged",), "parts_unknown_first": ("merged",), "result_then_part": ("merged",)}
_CASES = [(o, op) for op in _OPS for o in _ORIGINS if o in _ONLY.get(op, _ORIGINS)]


async def _states(s, cid) -> dict[str, dict]:
    s.expire_all()
    return {p.entity_id: p.state for p in (await s.execute(
        select(Projection).where(Projection.company_id == cid))).scalars()}


async def _replays(s, cid) -> None:
    """Rebuilding the company from its events gives the stored states."""
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company
    stored = await _states(s, cid)
    await lock_company(s, cid)
    await ProjectionEngine.rebuild(s, cid)
    await s.commit()
    replayed = await _states(s, cid)
    diff = {k: (stored.get(k), replayed.get(k)) for k in stored.keys() | replayed.keys()
            if stored.get(k) != replayed.get(k)}
    assert not diff, diff


def _normalized(states) -> list[str]:
    return sorted(_BK_UUID.sub("<id>", json.dumps(st, sort_keys=True, default=str)) for st in states)


@pytest.mark.parametrize("origin, op", _CASES, ids=[f"{o}-{op}" for o, op in _CASES])
async def test_unknown_cost_through_each_operation(real_engine, real_client, tmp_path, monkeypatch, origin, op):
    _bk_local(monkeypatch, tmp_path)
    _user, cid, tok = await _bk_setup(real_engine)
    async with maker(real_engine)() as s:
        await provision_company_books(s, cid)
        await s.commit()
        auth = {"headers": {"Authorization": f"Bearer {tok}"}, "company_id": cid}
        c = real_client
        x = await _ORIGINS[origin](c, s, auth)
        assert (await _state(s, auth, x.lot))[MARK] is True
        await assert_settled(c, s, auth)
        await _replays(s, cid)

        for lot, known in (await _OPS[op](c, s, auth, x)).items():
            st = await _state(s, auth, lot)
            if known is not None:
                assert (MARK not in st and st.get("cost_total") is not None) is known, (lot, st)
            assert MARK not in (st.get("attributes") or {}), st
        await assert_settled(c, s, auth, in_transit=x.in_transit)
        await _replays(s, cid)
        held = _normalized((await _states(s, cid)).values())

    copy = await _bk_restore_new(real_client, tok, await download(real_client, tok))
    async with maker(real_engine)() as s:
        assert _normalized((await _states(s, copy)).values()) == held
        await _replays(s, copy)
