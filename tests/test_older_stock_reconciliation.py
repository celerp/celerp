# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock is placed against what all its inventory accounts hold together.

Older releases booked stock to purchased inventory and to opening inventory, so neither
account alone says where a lot's value sits. Placing older stock compares the lots'
value with what the two accounts hold together beyond the stock recorded on them:

- split across both: the value is moved onto the default account, nothing is added;
- less than the stock: the missing value is recognized as opening stock, the way
  opening stock is booked, and recorded as such;
- more than the stock: nothing is written off; the difference is recorded on the
  account, unresolved, for the accountant;
- exactly the stock: the lots are placed and nothing is booked.

Every account that has served either role takes part, current or former: an account
purchased inventory has since moved off still holds what was booked to it, and that
value is moved onto the default account, never recognized again.

After each, every inventory account holds exactly the lots recorded on it, less the
cost invoices recognized and have not shipped, plus any unresolved difference
recorded on it (stock_books.assert_books_carry_stock).

The stock comes in through the batch import, as a demo company's does, and is then
made to look as an older release left it (test_older_stock_placement._older_company).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.company_lock import locked_company
from stock_books import assert_books_carry_stock
from test_older_stock_placement import _accounts, _entries, _lines, _off_books, _older_company, _today, _written_down
from test_posting_roles_lots import _new_inventory_account
from test_posting_roles_older_stock import _events, _net, _settings
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_LOTS = [25000.0] * 4  # 100,000 of older stock


async def _posted(session, auth, debit: str, credit: str, amount: float, memo: str) -> None:
    je = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=je, idem_create=f"{je}:c",
        idem_posted=f"{je}:p", memo=memo, metadata_={"trigger": "manual"},
        entries=[{"account": debit, "debit": amount, "credit": 0.0},
                 {"account": credit, "debit": 0.0, "credit": amount}])
    await session.commit()


async def _split(session, auth, purchased: float) -> None:
    """``purchased`` of the stock's value booked on purchased inventory, as an older
    release booked goods movements there; the rest stays on opening inventory."""
    await _posted(session, auth, "1130-P", "1130-OB", purchased, "Goods movements booked by an older release")


async def _placement_entries(session, auth) -> list[dict]:
    return await _entries(session, auth, "je:auto:older-stock-placed:")


async def _records(session, auth) -> list[dict]:
    session.expire_all()
    return [r.state for r in (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_type == "older_stock_placement"))).scalars()]


async def _purchased_moved_while_waiting(session, client, auth, code: str) -> None:
    """Purchased inventory pointed at a new account ``code`` in Settings while the older
    stock waits on a period lock over today, then the lock lifted; the stock is placed on
    the next start."""
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    await _new_inventory_account(client, auth, code)
    r = await client.put("/accounting/posting-accounts/inventory_purchased", headers=auth["headers"],
                         json={"code": code})
    assert r.status_code == 200, r.text
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()


def _no_equity(entries: list[dict]) -> None:
    for entry in entries:
        assert "3200" not in _lines(entry), entry


async def test_stock_split_across_both_accounts_is_moved_onto_one_with_no_new_inventory(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-P"}
    (entry,) = await _placement_entries(session, auth)
    assert _lines(entry) == {"1130-P": (40000.0, 0.0), "1130-OB": (0.0, 40000.0)}
    _no_equity([entry])
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("100000.00"), "1130-OB": Decimal("0.00")}
    (record,) = await _records(session, auth)
    assert (record["account"], record["transferred"], record["recognized"], record["unresolved"]) == (
        "1130-P", 40000.0, 0.0, 0.0)
    assert record["classification"] == "transfer"
    assert record["rooms"] == {"1130-P": 60000.0, "1130-OB": 40000.0}
    assert await _off_books(session, auth) == []


async def test_stock_the_accounts_fall_short_of_is_recognized_as_opening_stock(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _written_down(session, auth, "1130-OB", 30000.0)  # the two hold 70,000 for 100,000
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-P"}
    transfer, recognized = await _placement_entries(session, auth)
    assert _lines(transfer) == {"1130-P": (10000.0, 0.0), "1130-OB": (0.0, 10000.0)}
    assert _lines(recognized) == {"1130-P": (30000.0, 0.0), "3200": (0.0, 30000.0)}
    assert recognized["memo"] == "Opening stock recognized for older stock"
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("100000.00"), "1130-OB": Decimal("0.00")}
    (record,) = await _records(session, auth)
    assert (record["transferred"], record["recognized"], record["unresolved"]) == (10000.0, 30000.0, 0.0)
    assert record["classification"] == "opening_stock"
    assert record["journal"] == [transfer["id"], recognized["id"]]
    assert await _off_books(session, auth) == []


async def test_stock_the_accounts_hold_more_than_is_kept_as_an_unresolved_difference(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _posted(session, auth, "1130-OB", "3200", 30000.0, "Stock written up")  # 130,000 for 100,000
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    (entry,) = await _placement_entries(session, auth)
    assert _lines(entry) == {"1130-OB": (60000.0, 0.0), "1130-P": (0.0, 60000.0)}
    _no_equity([entry])
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 130000.0)
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("0.00"), "1130-OB": Decimal("130000.00")}
    (record,) = await _records(session, auth)
    assert (record["account"], record["transferred"], record["recognized"], record["unresolved"]) == (
        "1130-OB", 60000.0, 0.0, 30000.0)
    assert record["classification"] == "unresolved"
    assert await _off_books(session, auth) == [
        {"kind": "unresolved_difference", "account": "1130-OB", "amount": 30000.0}]


async def test_value_left_on_a_former_purchased_account_is_moved_not_recognized_again(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)  # 1130-P 60,000, 1130-OB 40,000
    await _purchased_moved_while_waiting(session, client, auth, "1131")
    assert set(await _accounts(session, auth, lots)) == {None}
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    (entry,) = await _placement_entries(session, auth)
    assert _lines(entry) == {"1130-OB": (60000.0, 0.0), "1130-P": (0.0, 60000.0)}
    _no_equity([entry])
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("0.00"), "1130-OB": Decimal("100000.00"), "1131": Decimal("0.00")}
    (record,) = await _records(session, auth)
    assert (record["account"], record["transferred"], record["recognized"], record["unresolved"]) == (
        "1130-OB", 60000.0, 0.0, 0.0)
    assert record["classification"] == "transfer"
    assert record["rooms"] == {"1131": 0.0, "1130-OB": 40000.0, "1130-P": 60000.0}
    assert await _off_books(session, auth) == []


async def test_stock_split_across_three_accounts_is_short_only_of_what_all_three_hold(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 30000.0)  # 1130-P 30,000, 1130-OB 70,000
    await _purchased_moved_while_waiting(session, client, auth, "1131")
    await _posted(session, auth, "1131", "1130-OB", 20000.0, "Goods movements booked on the new account")
    await _written_down(session, auth, "1130-OB", 20000.0)  # 30,000 + 20,000 + 30,000 for 100,000
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    transfer, recognized = await _placement_entries(session, auth)
    assert _lines(transfer) == {"1130-OB": (50000.0, 0.0), "1131": (0.0, 20000.0), "1130-P": (0.0, 30000.0)}
    assert _lines(recognized) == {"1130-OB": (20000.0, 0.0), "3200": (0.0, 20000.0)}
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("0.00"), "1130-OB": Decimal("100000.00"), "1131": Decimal("0.00")}
    (record,) = await _records(session, auth)
    assert (record["transferred"], record["recognized"], record["unresolved"]) == (50000.0, 20000.0, 0.0)
    assert record["classification"] == "opening_stock"
    assert await _off_books(session, auth) == []


async def test_stock_the_accounts_hold_exactly_is_placed_with_no_entry(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert await _placement_entries(session, auth) == []
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("0.00"), "1130-OB": Decimal("100000.00")}
    (record,) = await _records(session, auth)
    assert (record["transferred"], record["recognized"], record["unresolved"], record["journal"]) == (
        0.0, 0.0, 0.0, [])
    assert record["classification"] == "exact" and record["count"] == len(lots)


async def test_placing_again_adds_nothing(session, client, auth):
    await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _written_down(session, auth, "1130-OB", 30000.0)
    await _startup(session)
    events = await _events(session, auth)
    await _startup(session)
    assert await _events(session, auth) == events
    assert len(await _records(session, auth)) == 1


async def test_a_placement_that_fails_part_way_leaves_nothing_applied(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _written_down(session, auth, "1130-OB", 30000.0)
    events = await _events(session, auth)
    with patch("celerp.services.lot_origin.post_opening_stock_delta", AsyncMock(side_effect=RuntimeError("boom"))):
        await _startup(session)
    assert set(await _accounts(session, auth, lots)) == {None}
    assert await _placement_entries(session, auth) == []
    assert await _records(session, auth) == []
    assert await _net(session, auth, "1130-P", "1130-OB") == (60000.0, 10000.0)
    assert await _events(session, auth) == events
