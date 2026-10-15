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
from test_older_stock_placement import (
    _accounts, _entries, _lines, _notices, _off_books, _older_company, _today, _written_down,
)
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


async def _bank(client, auth) -> str:
    r = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
        "bank_name": "Second bank", "account_number": "0002", "bank_type": "checking", "currency": "USD"})
    assert r.status_code == 200, r.text
    return r.json()["chart_account_code"]


async def _role(client, auth, role: str, code: str):
    return await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": code})


async def _unlocked(session, auth) -> None:
    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()


async def _given(session, auth, **roles: str) -> None:
    """Posting accounts as an older release or a hand edit left them, with no check run."""
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "posting_roles": {**(company.settings.get("posting_roles") or {}), **roles}}
    await session.commit()


async def _sold(client, auth, item_id: str):
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "total": 50.0,
        "line_items": [{"entity_id": item_id, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    return await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"])


def _evidence(findings: list[dict]) -> list[tuple[str, float]]:
    """The books check's findings, which must all be lines that may or may not have been
    stock, as (account, amount), each from one entry."""
    assert {f["kind"] for f in findings} <= {"uncertain_evidence"}, findings
    assert all(len(f["entries"]) == 1 for f in findings), findings
    return [(f["account"], f["amount"]) for f in findings]


async def test_cash_sales_and_receivables_on_any_account_are_never_counted_as_stock(session, client, auth):
    """Only lines posted for an inventory role are stock: a bank is refused as an
    inventory account, a receivable once used as purchased inventory keeps its sales,
    and none of it is moved or recognized."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)  # 1130-P 60,000, 1130-OB 40,000
    await _posted(session, auth, "1111", "4100", 5000.0, "Cash sale")
    await _posted(session, auth, "1120", "4100", 3000.0, "Credit sale")
    await _settings(session, auth, lock_date=_today())
    await _startup(session)  # posting accounts set; the stock waits on the lock
    bank = await _bank(client, auth)

    for role in ("inventory_purchased", "inventory_opening"):
        for code in ("1111", bank):
            r = await _role(client, auth, role, code)
            assert r.status_code == 422, (role, code, r.text)
            detail = r.json()["detail"]
            assert detail["message_key"] == "posting.problem.not_inventory", detail
            assert code in detail["message"] and "\u2014" not in detail["message"]
    assert (await _role(client, auth, "inventory_purchased", "1120")).status_code == 200
    assert (await _role(client, auth, "inventory_purchased", "1130-P")).status_code == 200
    await _unlocked(session, auth)
    await _posted(session, auth, bank, "4100", 2000.0, "Cash sale into the second bank")
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-P"}
    assert await _net(session, auth, "1111", bank, "1120", "1130-P", "1130-OB") == (
        5000.0, 2000.0, 3000.0, 100000.0, 0.0)
    (record,) = await _records(session, auth)
    assert record["rooms"] == {"1130-P": 60000.0, "1130-OB": 40000.0, "1120": 0.0}
    assert (record["transferred"], record["recognized"], record["unresolved"]) == (40000.0, 0.0, 0.0)
    # The credit sale on 1120 carries no roles of its own and 1120 has served both a
    # receivable and purchased inventory: it is shown for review, never counted or booked.
    assert _evidence(await _off_books(session, auth)) == [("1120", 3000.0)]


async def test_a_bank_or_receivable_already_in_the_inventory_history_keeps_its_balance(session, client, auth):
    """A company whose purchased inventory history already names a bank account and a
    receivable, as an older release could leave it, places its older stock from the
    lines posted as stock only: the cash and the sales stay where they are. Both sales
    were posted by hand with no roles on their lines, and the bank being a bank account
    today says nothing of what an older line on it was for: each is evidence to review,
    never counted or booked."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)  # 1130-P 60,000, 1130-OB 40,000
    await _posted(session, auth, "1111", "4100", 5000.0, "Cash sale")
    await _posted(session, auth, "1120", "4100", 3000.0, "Credit sale")
    await _settings(session, auth, lock_date=_today())
    await _startup(session)  # posting accounts set; the stock waits on the lock
    company = await locked_company(session, auth["company_id"])
    scopes = dict(company.settings["posting_role_scopes"])
    scopes["inventory_purchased"] = ["1111", "1120", *scopes["inventory_purchased"]]
    company.settings = {k: v for k, v in {**company.settings, "posting_role_scopes": scopes}.items()
                        if k != "lock_date"}
    await session.commit()
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-P"}
    (entry,) = await _placement_entries(session, auth)
    assert _lines(entry) == {"1130-P": (40000.0, 0.0), "1130-OB": (0.0, 40000.0)}
    _no_equity([entry])
    assert await _net(session, auth, "1111", "1120", "1130-P", "1130-OB") == (5000.0, 3000.0, 100000.0, 0.0)
    (record,) = await _records(session, auth)
    assert record["rooms"] == {"1130-P": 60000.0, "1130-OB": 40000.0, "1111": 0.0, "1120": 0.0}
    assert (record["transferred"], record["recognized"], record["unresolved"]) == (40000.0, 0.0, 0.0)
    assert _evidence(await _off_books(session, auth)) == [("1111", 5000.0), ("1120", 3000.0)]


async def test_prepaid_rent_on_an_account_once_used_as_purchased_inventory_is_never_swept(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    await _unlocked(session, auth)
    await _posted(session, auth, "1140", "1111", 3000.0, "Prepaid rent")  # classified when posted: not stock
    await _settings(session, auth, lock_date=_today())
    assert (await _role(client, auth, "inventory_purchased", "1140")).status_code == 200
    assert (await _role(client, auth, "inventory_purchased", "1130-P")).status_code == 200
    await _unlocked(session, auth)
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert await _net(session, auth, "1140", "1130-P", "1130-OB") == (3000.0, 0.0, 100000.0)
    (record,) = await _records(session, auth)
    assert (record["transferred"], record["recognized"], record["unresolved"]) == (0.0, 0.0, 0.0)
    assert await _off_books(session, auth) == []


async def test_a_deposit_account_already_on_purchased_inventory_still_places_and_sells(session, client, auth):
    """Settings an older release left with the deposit account on purchased inventory do
    not stop older stock being placed or sold, and deposits taken there are never stock."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _given(session, auth, default_deposit="1130-P")
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert await _notices(session, auth, "Older stock waits for a posting account") == []
    assert (await _sold(client, auth, lots[0])).status_code == 200
    await _posted(session, auth, "1130-P", "4100", 700.0, "Deposit taken")
    assert await _net(session, auth, "1130-P") == (700.0,)
    assert await _off_books(session, auth) == []
    assert (await _role(client, auth, "tax_input", "1150")).status_code == 200


@pytest.mark.parametrize("change", ["cash_header_over_inventory", "inventory_under_cash", "bank_on_purchased"])
async def test_a_change_outside_the_inventory_accounts_never_stops_placement(session, client, auth, change):
    lots = await _older_company(session, client, auth, _LOTS)
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    if change == "cash_header_over_inventory":
        r = await _role(client, auth, "cash_and_equivalents", "1100")
    elif change == "inventory_under_cash":
        r = await client.patch("/accounting/accounts/1130", headers=auth["headers"], json={"parent_code": "1110"})
    else:
        r = await client.post("/accounting/bank-accounts", headers=auth["headers"], json={
            "bank_name": "Linked bank", "bank_type": "checking", "currency": "USD", "account_number": "0003",
            "account_code": "1130-P"})
    assert r.status_code == 200, r.text
    await _unlocked(session, auth)
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert (await _sold(client, auth, lots[0])).status_code == 200
    assert await _off_books(session, auth) == []


async def test_an_account_still_holding_stock_is_refused_for_any_other_posting_account(session, client, auth):
    """Lots placed on opening inventory stay there after opening inventory moves on, and
    older value stays on a former purchased account until placed: neither account can
    then take deposits, input tax, receivables or fixed assets."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _new_inventory_account(client, auth, "1135")
    await _posted(session, auth, "1135", "1130-OB", 20000.0, "Goods moved to 1135")
    await _settings(session, auth, lock_date=_today())
    await _startup(session)
    assert (await _role(client, auth, "inventory_purchased", "1135")).status_code == 200
    assert (await _role(client, auth, "inventory_purchased", "1130-P")).status_code == 200
    for role in ("tax_input", "default_deposit", "receivable", "fixed_assets"):
        r = await _role(client, auth, role, "1135")
        assert r.status_code == 422, (role, r.text)
        detail = r.json()["detail"]
        assert detail["message_key"] == "posting.problem.holds_inventory", detail
        assert "1135" in detail["message"] and "\u2014" not in detail["message"]
    await _unlocked(session, auth)
    await _startup(session)
    (record,) = await _records(session, auth)
    assert (record["transferred"], record["recognized"]) == (20000.0, 0.0)

    await _new_inventory_account(client, auth, "1136")
    assert (await _role(client, auth, "inventory_opening", "1136")).status_code == 200
    held = set(await _accounts(session, auth, lots))
    assert held == {"1130-OB"}
    r = await _role(client, auth, "default_deposit", "1130-OB")
    assert r.status_code == 422 and r.json()["detail"]["message_key"] == "posting.problem.holds_inventory"
    assert (await _role(client, auth, "default_deposit", "1135")).status_code == 200  # emptied by the placement


async def _inactive(session, auth, code: str, *, active: bool = False) -> None:
    from celerp_accounting.models import Account

    account = (await session.execute(select(Account).where(
        Account.company_id == auth["company_id"], Account.code == code))).scalar_one()
    account.is_active = active
    await session.commit()


async def _mapped(session, auth) -> None:
    """The inventory and retained earnings accounts set the way a current release sets
    them, so switching one off later leaves the role pointing at it."""
    roles = {"inventory_purchased": "1130-P", "inventory_opening": "1130-OB", "retained_earnings": "3200"}
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings,
                        "posting_roles": {**(company.settings.get("posting_roles") or {}), **roles},
                        "posting_role_scopes": {**(company.settings.get("posting_role_scopes") or {}),
                                                **{role: [code] for role, code in roles.items()}}}
    await session.commit()


@pytest.mark.parametrize("on_purchased", [0.0, 60000.0])
async def test_an_inactive_opening_account_holding_stock_value_holds_placement_naming_it(
        session, client, auth, on_purchased):
    """Opening inventory switched off while it still holds stock value: that value is
    counted, cannot be moved off a switched-off account, so nothing is placed or
    recognized again, and the notice names the account to fix. Once it is switched back
    on the stock is placed with nothing added to the books."""
    lots = await _older_company(session, client, auth, _LOTS)
    if on_purchased:
        await _split(session, auth, on_purchased)
    await _mapped(session, auth)
    await _inactive(session, auth, "1130-OB")
    await _startup(session)

    assert await _accounts(session, auth, lots) == [None] * 4
    assert await _records(session, auth) == []
    (notice,) = await _notices(session, auth, "Older stock waits for a posting account")
    assert "Opening inventory is set to account 1130-OB, which is inactive." in notice.body

    await _inactive(session, auth, "1130-OB", active=True)
    await _startup(session)
    (record,) = await _records(session, auth)
    assert record["recognized"] == 0.0
    assert sum((await assert_books_carry_stock(session, auth["company_id"])).values()) == Decimal("100000.00")


async def test_an_inactive_purchased_account_holding_nothing_leaves_older_stock_on_opening_inventory(
        session, client, auth):
    """Purchased inventory switched off while it holds nothing: older stock is placed on
    opening inventory, nothing is recognized, and the stock sells."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _mapped(session, auth)
    await _inactive(session, auth, "1130-P")
    await _startup(session)

    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert await _notices(session, auth, "Older stock waits for a posting account") == []
    (record,) = await _records(session, auth)
    assert (record["account"], record["transferred"], record["recognized"]) == ("1130-OB", 0.0, 0.0)
    assert await assert_books_carry_stock(session, auth["company_id"]) == {
        "1130-P": Decimal("0.00"), "1130-OB": Decimal("100000.00")}
    assert (await _sold(client, auth, lots[0])).status_code == 200


async def test_an_inactive_purchased_account_holding_stock_value_holds_placement_naming_it(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS)
    await _split(session, auth, 60000.0)
    await _mapped(session, auth)
    await _inactive(session, auth, "1130-P")
    await _startup(session)

    assert await _accounts(session, auth, lots) == [None] * 4
    assert await _records(session, auth) == []
    (notice,) = await _notices(session, auth, "Older stock waits for a posting account")
    assert "Inventory purchased is set to account 1130-P, which is inactive." in notice.body


async def test_older_stock_waits_naming_each_inventory_account_and_why_it_cannot_take_entries(
        session, client, auth):
    """Neither inventory account can take the stock: nothing is placed or recognized, and
    the notice names each account and what is wrong with it."""
    lots = await _older_company(session, client, auth, _LOTS)
    await _mapped(session, auth)
    await _inactive(session, auth, "1130-P")
    await _inactive(session, auth, "1130-OB")
    await _startup(session)

    assert await _accounts(session, auth, lots) == [None] * 4
    assert await _records(session, auth) == []
    (notice,) = await _notices(session, auth, "Older stock waits for a posting account")
    assert "Inventory purchased is set to account 1130-P, which is inactive." in notice.body
    assert "Opening inventory is set to account 1130-OB, which is inactive." in notice.body


async def test_older_stock_short_of_its_value_waits_naming_the_retained_earnings_account(session, client, auth):
    lots = await _older_company(session, client, auth, _LOTS, short=10000.0)
    await _mapped(session, auth)
    await _inactive(session, auth, "3200")
    await _startup(session)

    assert await _accounts(session, auth, lots) == [None] * 4
    (notice,) = await _notices(session, auth, "Older stock waits for a posting account")
    assert "Retained earnings is set to account 3200, which is inactive." in notice.body


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
