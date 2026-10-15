# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Correcting how a placement of older stock treated its difference.

A placement recognizes what the inventory accounts held short of the stock as opening
stock, and leaves what they held beyond it unresolved for the accountant
(test_older_stock_reconciliation). A surplus stays open until the accountant writes it
off against retained earnings as a prior-period correction, and a write-off is undone
in one step. The accountant can correct either treatment: book an unresolved difference
against retained earnings the way opening stock is booked, or take a recognized amount
back off the books and leave it unresolved instead. Each
correction is one balanced entry dated today, the placement record follows it, and
every inventory account still holds its lots less unshipped recognized cost plus its
unresolved difference. A placement whose lots were sold, shipped or moved since, or a
locked today, is refused with a plain reason and nothing is written.
"""
from __future__ import annotations

import pytest

from celerp.services import lot_origin
from test_older_stock_placement import (
    _entries,
    _imported,
    _lines,
    _lock_through,
    _move,
    _off_books,
    _older_company,
    _refused,
    _today,
    _view,
    _written_down,
)
from test_posting_roles_lots import _sell
from test_posting_roles_older_stock import _books_over, _events, _net, _older_release
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_PATH = "/accounting/older-stock/corrections"


async def _correct(client, auth, kind: str, *pids: str):
    return await client.post(_PATH, headers=auth["headers"], json={"placements": list(pids), "kind": kind})


async def _corrections(session, auth, pid: str) -> list[dict]:
    return await _entries(session, auth, f"je:auto:older-stock-placed:{pid}:correction:")


async def _short_placement(session, client, auth) -> tuple[list[str], dict]:
    """30 of stock with 1130-OB holding 20: 10 recognized as opening stock."""
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    assert placement["recognized"] == 10.0 and placement["unresolved"] == 0.0
    return lots, placement


async def test_recognized_opening_stock_can_be_left_unresolved_and_booked_again(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)

    r = await _correct(client, auth, "unresolve", placement["pid"])

    assert r.status_code == 200, r.text
    (after,) = r.json()["placements"]
    assert after["recognized"] == 0.0 and after["unresolved"] == -10.0
    assert [c["kind"] for c in after["corrections"]] == ["unresolve"]
    assert after["journal"] == placement["journal"] and after["transferred"] == placement["transferred"]
    assert after["corrections"][0]["journal"] == f"je:auto:older-stock-placed:{placement['pid']}:correction:1"
    assert await _net(session, auth, "1130-OB") == (20.0,)
    assert await _off_books(session, auth) == [{"kind": "unresolved_difference", "account": "1130-OB",
                                                "amount": -10.0}]
    events = await _events(session, auth)
    assert (await _correct(client, auth, "unresolve", placement["pid"])).status_code == 200
    assert await _events(session, auth) == events  # nothing left to take off: nothing written

    r = await _correct(client, auth, "recognize", placement["pid"])

    assert r.status_code == 200, r.text
    (after,) = r.json()["placements"]
    assert after["recognized"] == 10.0 and after["unresolved"] == 0.0
    assert await _net(session, auth, "1130-OB") == (30.0,)
    assert await _off_books(session, auth) == []
    assert [_lines(je) for je in await _corrections(session, auth, placement["pid"])] == [
        {"3200": (10.0, 0.0), "1130-OB": (0.0, 10.0)}, {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}]
    assert await lot_origin.unresolved_differences(session, auth["company_id"]) == {}
    assert len(lots) == 1


async def _rooms(session, auth) -> dict:
    session.expire_all()
    return await lot_origin.account_rooms(session, auth["company_id"], {"1130-P", "1130-OB"})


async def test_a_surplus_stays_open_until_written_off_and_the_write_off_can_be_undone(session, client, auth):
    lots = await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _written_down(session, auth, "1130-OB", 30.0)
    await _books_over(session, client, auth)  # 1130-P carries 40 for 30 of stock
    await _startup(session)
    view = await _view(client, auth)
    (placement,) = view["placements"]
    pid = placement["pid"]

    # Placing never writes a surplus off: it stays open on the record and on the books.
    assert placement["unresolved"] == 10.0 and placement["recognized"] == 0.0
    assert placement["classification"] == "unresolved" and placement["corrections"] == []
    assert await _corrections(session, auth, pid) == []
    assert await _net(session, auth, "1130-P") == (40.0,)
    assert await _off_books(session, auth) == [{"kind": "unresolved_difference", "account": "1130-P",
                                                "amount": 10.0}]
    assert await _rooms(session, auth) == {"1130-P": 0, "1130-OB": 0}
    assert view["retained"]["code"] == "3200"  # the account a write-off posts to, named before it is chosen

    r = await _correct(client, auth, "recognize", pid)

    assert r.status_code == 200, r.text
    (after,) = r.json()["placements"]
    assert after["unresolved"] == 0.0 and after["recognized"] == -10.0
    assert after["classification"] == "unresolved"  # what the placement found stays on its record
    assert [c["kind"] for c in after["corrections"]] == ["write_off"]
    assert [c["kind"] for c in r.json()["corrections"]] == ["write_off"]
    assert await _net(session, auth, "1130-P") == (30.0,)
    assert await _off_books(session, auth) == []
    assert await _rooms(session, auth) == {"1130-P": 0, "1130-OB": 0}
    (write_off,) = await _corrections(session, auth, pid)
    assert _lines(write_off) == {"3200": (10.0, 0.0), "1130-P": (0.0, 10.0)}
    assert "prior-period correction" in write_off["memo"]

    r = await _correct(client, auth, "unresolve", pid)

    assert r.status_code == 200, r.text
    (undone,) = r.json()["placements"]
    assert undone["unresolved"] == 10.0 and undone["recognized"] == 0.0
    assert [c["kind"] for c in undone["corrections"]] == ["write_off", "write_off_undone"]
    assert await _net(session, auth, "1130-P") == (40.0,)
    assert await _off_books(session, auth) == [{"kind": "unresolved_difference", "account": "1130-P",
                                                "amount": 10.0}]
    assert await _rooms(session, auth) == {"1130-P": 0, "1130-OB": 0}
    assert [_lines(je) for je in await _corrections(session, auth, pid)] == [
        {"3200": (10.0, 0.0), "1130-P": (0.0, 10.0)}, {"1130-P": (10.0, 0.0), "3200": (0.0, 10.0)}]
    assert len(lots) == 1


async def test_a_placement_whose_lot_was_sold_since_cannot_be_corrected(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)
    await _sell(client, auth, (lots[0], 1))
    events = await _events(session, auth)

    detail = _refused(await _correct(client, auth, "unresolve", placement["pid"]), 409,
                      "older_stock.correct.changed_since")

    assert detail["params"]["sku"]
    assert await _events(session, auth) == events


async def test_a_placement_whose_lot_was_moved_since_cannot_be_corrected(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)
    assert (await _move(client, auth, lots, "1130-P")).status_code == 200
    events = await _events(session, auth)

    _refused(await _correct(client, auth, "unresolve", placement["pid"]), 409, "older_stock.correct.changed_since")

    assert await _events(session, auth) == events


async def test_a_correction_on_a_locked_day_writes_nothing(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)
    await _lock_through(client, auth, _today())
    events = await _events(session, auth)

    r = await _correct(client, auth, "unresolve", placement["pid"])

    assert r.status_code == 422, r.text
    assert await _events(session, auth) == events
    assert len(lots) == 1


async def test_a_correction_that_would_leave_an_account_off_its_stock_is_refused(session, client, auth, monkeypatch):
    """The equation is checked again after the entry: an entry that did not book what
    the record says is refused whole."""
    lots, placement = await _short_placement(session, client, auth)

    async def books_nothing(*a, **k):
        return None

    monkeypatch.setattr(lot_origin, "post_opening_stock_delta", books_nothing)
    events = await _events(session, auth)

    _refused(await _correct(client, auth, "unresolve", placement["pid"]), 409, "older_stock.correct.unbalanced")

    assert await _events(session, auth) == events
    assert len(lots) == 1


async def test_correcting_an_unknown_placement_or_with_no_kind_is_refused(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)
    events = await _events(session, auth)

    _refused(await _correct(client, auth, "unresolve", placement["pid"], "nothing"), 404,
             "older_stock.correct.no_placement")
    _refused(await _correct(client, auth, "write_off", placement["pid"]), 422, "older_stock.correct.no_kind")
    _refused(await _correct(client, auth, "unresolve"), 422, "older_stock.correct.nothing_selected")

    assert await _events(session, auth) == events
    assert len(lots) == 1


async def test_the_reversal_toggle_is_gone(session, client, auth):
    lots, placement = await _short_placement(session, client, auth)

    r = await client.post(f"/accounting/older-stock/placements/{placement['pid']}/adjustment",
                          headers=auth["headers"], json={"booked": False})

    assert r.status_code in (404, 405), r.text
    assert not hasattr(lot_origin, "set_placement_adjustment")
    assert "booked" not in placement and "adjustment" not in placement
    assert len(lots) == 1
