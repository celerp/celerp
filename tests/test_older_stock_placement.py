# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock placed on an inventory account by itself, moved in bulk, and undone.

Stock from before lots recorded their inventory account is placed at start, every lot
of it, on one default account: the account the books show holding it, or purchased
inventory. Any difference between what that account holds and the stock is booked in
one dated adjustment against retained earnings, and the company is told. From
Settings > Accounting > Older stock the lots can be moved to another inventory account
in bulk, each move undone, and the adjustment reversed or booked again.

The stock comes in the way a demo company's does, through the batch import, and is then
made to look as an older release left it: no lot records an account and the company
has no posting accounts in its settings.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, INVENTORY_ORIGIN_SCHEMA
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.auto_je import _emit_auto_posted_je
from celerp.services.business_time import business_date_at
from celerp.services.company_lock import locked_company
from test_helpers import TZ, in_language
from test_posting_roles_lots import _new_inventory_account, _sell
from test_posting_roles_older_stock import (
    _books_over,
    _events,
    _lock_notices,
    _net,
    _older_release,
    _settings,
)
from test_posting_roles_rollout import _startup
from ui import i18n

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_PLACED = "Older stock placed on an inventory account"
_UNMAPPED = "Older stock waits for a posting account"
_TAB = "/settings/accounting?tab=older-stock"
_LOCALES = ("am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi")


# --- Older stock as a demo import brings it in -----------------------------------------


async def _imported(session, client, auth, costs: list[float], **fields) -> list[str]:
    """Lots brought in by the batch import, 500 to a call, then stripped of the account
    each records and of the event recording it, as an older release never recorded one.
    The import's opening entry stays on 1130-OB, where an older release booked opening
    stock."""
    ids: list[str] = []
    for start in range(0, len(costs), 500):
        records = [{"entity_id": f"item:{uuid.uuid4()}", "event_type": "item.created", "source": "csv",
                    "idempotency_key": f"older:{uuid.uuid4()}",
                    "data": {"sku": f"OLD-{start + i:05}", "name": "Older lot", "quantity": 1, "sell_by": "piece",
                             "status": "available", "cost_total": cost, **fields}}
                   for i, cost in enumerate(costs[start:start + 500])]
        r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": records})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == len(records), r.text
        ids += [rec["entity_id"] for rec in records]
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.in_(ids))
        .execution_options(populate_existing=True))).scalars().all()
    for row in rows:
        row.state = {k: v for k, v in row.state.items() if k != _FIELD}
    await session.execute(delete(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id.in_(ids),
        LedgerEntry.event_type == "item.inventory_account.recorded"))
    await session.commit()
    return ids


async def _written_down(session, auth, code: str, amount: float) -> None:
    """An entry taking ``amount`` off ``code`` against retained earnings, so the account
    no longer holds all the stock booked to it."""
    je = f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=je, idem_create=f"{je}:c",
        idem_posted=f"{je}:p", memo="Stock written down", metadata_={"trigger": "manual"},
        entries=[{"account": "3200", "debit": amount, "credit": 0.0},
                 {"account": code, "debit": 0.0, "credit": amount}])
    await session.commit()


async def _marked(session, auth) -> None:
    """A company an earlier release already upgraded, its older stock still unplaced."""
    await _settings(session, auth, **{INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA})


async def _older_company(session, client, auth, costs: list[float], short: float = 0.0) -> list[str]:
    """An upgraded company whose older lots record no account, with 1130-OB holding their
    value less ``short``."""
    lots = await _imported(session, client, auth, costs)
    await _older_release(session, auth)
    if short:
        await _written_down(session, auth, "1130-OB", short)
    await _marked(session, auth)
    return lots


async def _accounts(session, auth, lots: list[str]) -> list[str | None]:
    session.expire_all()
    rows = {r.entity_id: r.state for r in (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.in_(lots)))).scalars()}
    return [rows[lot].get(_FIELD) for lot in lots]


async def _notices(session, auth, title: str) -> list:
    from celerp.models.notification import Notification

    session.expire_all()
    return list((await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.title == title))).scalars().all())


async def _entries(session, auth, prefix: str) -> list[dict]:
    """The posted entries whose id starts with ``prefix``, oldest first."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like(f"{prefix}%")).order_by(Projection.created_at))).scalars().all()
    return [{**r.state, "id": r.entity_id} for r in rows if r.state.get("status") == "posted"]


async def _placements(session, auth) -> list[dict]:
    return [e for e in await _entries(session, auth, "je:auto:older-stock-placed:") if ":t" not in e["id"][-4:]]


def _lines(je: dict) -> dict[str, tuple[float, float]]:
    return {e["account"]: (e["debit"], e["credit"]) for e in je["entries"]}


async def _off_books(session, auth) -> list[dict]:
    from celerp.services.lot_origin import stock_off_books

    session.expire_all()
    return await stock_off_books(session, auth["company_id"])


async def _placed_lots(session, auth) -> list[str]:
    """The lots recorded on an account by an automatic placement, once per recording."""
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.event_type == "item.inventory_account.recorded"))).scalars().all()
    return [r.entity_id for r in rows if (r.metadata_ or {}).get("placement")]


def _today() -> str:
    return business_date_at(datetime.now(timezone.utc), TZ)


async def _view(client, auth, **params) -> dict:
    r = await client.get("/accounting/older-stock", headers=auth["headers"], params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _move(client, auth, item_ids: list[str], code: str, key: str | None = "default"):
    body = {"item_ids": item_ids, "code": code}
    if key is not None:
        body["key"] = uuid.uuid4().hex if key == "default" else key
    return await client.post("/accounting/older-stock/move", headers=auth["headers"], json=body)


def _refused(r, status: int, key: str) -> dict:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key, detail
    assert in_language("de", detail) != detail["message"], detail
    return detail


# --- Placed at start -------------------------------------------------------------------


async def test_thousands_of_imported_older_lots_are_placed_in_one_start(session, client, auth):
    lots = await _imported(session, client, auth, [1.0] * 2000)
    await _older_release(session, auth)
    await _settings(session, auth, restored_backup={"backup_id": str(uuid.uuid4())})
    await _written_down(session, auth, "1130-OB", 10.0)
    await _startup(session)
    assert set(await _accounts(session, auth, lots)) == {"1130-OB"}
    assert sorted(await _placed_lots(session, auth)) == sorted(lots)
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}
    (notice,) = await _notices(session, auth, _PLACED)
    assert notice.priority == "high" and notice.action_url == _TAB
    assert "2000" in notice.body and "1130-OB" in notice.body and _today() in notice.body
    assert await _net(session, auth, "1130-OB") == (2000.0,)
    assert await _off_books(session, auth) == []


async def test_a_migrated_company_places_its_stock_on_opening_inventory(session, client, auth):
    from test_posting_roles_older_stock import _migration_run

    lots = await _imported(session, client, auth, [30.0, 20.0])
    await _older_release(session, auth)
    await _migration_run(session, client, auth)
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 50.0)
    (notice,) = await _notices(session, auth, _PLACED)
    assert "2 older lots" in notice.body and "1130-OB" in notice.body and "adjustment" not in notice.body
    assert await _off_books(session, auth) == []


async def test_accounts_that_do_not_add_up_are_placed_with_one_adjustment(session, client, auth):
    lots = await _imported(session, client, auth, [30.0])
    await _older_release(session, auth)
    await _written_down(session, auth, "1130-OB", 30.0)
    await _books_over(session, client, auth)  # 1130-P carries 40 for 30 of stock
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-P"]
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"3200": (10.0, 0.0), "1130-P": (0.0, 10.0)}
    assert entry["ts"][:10] == _today()
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)
    (notice,) = await _notices(session, auth, _PLACED)
    assert notice.body == i18n.t("notice.older_stock_placed.body_adjusted", "en", **notice.i18n["params"])
    for part in ("1130-P", "40.00", "10.00", "3200", _today()):
        assert part in notice.body, part
    assert await _off_books(session, auth) == []


async def test_existing_upgraded_company_with_unplaced_lots_is_placed_at_the_next_start(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0, 20.0])
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert len(await _notices(session, auth, _PLACED)) == 1
    assert await _off_books(session, auth) == []


async def test_placing_again_changes_nothing(session, client, auth):
    from celerp.services.lot_origin import place_older_stock

    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    events = await _events(session, auth)
    await _startup(session)
    assert not await place_older_stock(session, auth["company_id"])
    await session.commit()
    assert await _events(session, auth) == events
    assert len(await _placements(session, auth)) == 1
    assert len(await _notices(session, auth, _PLACED)) == 1


async def test_placement_held_by_a_period_lock_writes_nothing_and_runs_once_unlocked(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _settings(session, auth, lock_date=_today())
    events = await _events(session, auth)
    await _startup(session)
    await _startup(session)
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == [None]
    (notice,) = await _lock_notices(session, auth)
    assert notice.body == i18n.t("notice.older_stock_locked.body", "en", day=_today())
    assert "Choose its inventory account" not in notice.body
    assert _today() in notice.body

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "lock_date"}
    await session.commit()
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    (entry,) = await _placements(session, auth)
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}
    assert await _lock_notices(session, auth) == []


@pytest.mark.parametrize("role,code,fix", [("inventory_purchased", "1130-P", ("1135", "asset", "1100")),
                                           ("retained_earnings", "3200", ("3210", "equity", "3000"))])
async def test_older_stock_waits_for_purchased_inventory_then_is_placed_when_it_is_set(
        session, client, auth, role, code, fix):
    from celerp_accounting.models import Account

    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == code)
                          .values(is_active=False))
    await session.commit()
    events = await _events(session, auth)
    await _startup(session)
    await _startup(session)
    assert await _accounts(session, auth, lots) == [None]
    assert await _events(session, auth) == events
    (notice,) = await _notices(session, auth, _UNMAPPED)
    assert notice.action_url == "/settings/accounting?tab=posting-accounts"
    setting = "Purchased inventory" if role == "inventory_purchased" else "Retained earnings"
    assert setting in notice.body and "Settings > Accounting > Posting accounts" in notice.body

    new, kind, parent = fix
    r = await client.post("/accounting/accounts", headers=auth["headers"], json={
        "code": new, "name": f"Account {new}", "account_type": kind, "parent_code": parent})
    assert r.status_code == 200, r.text
    r = await client.put(f"/accounting/posting-accounts/{role}", headers=auth["headers"], json={"code": new})
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    (entry,) = await _placements(session, auth)
    equity = new if role == "retained_earnings" else "3200"
    assert _lines(entry) == {"1130-OB": (10.0, 0.0), equity: (0.0, 10.0)}
    assert await _notices(session, auth, _UNMAPPED) == []


async def test_older_stock_with_no_cost_is_placed_at_zero(session, client, auth):
    lots = await _older_company(session, client, auth, [0.0, 30.0])
    await _startup(session)
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    assert await _placements(session, auth) == []
    assert await _net(session, auth, "1130-OB") == (30.0,)


async def test_consigned_and_draft_lots_are_not_placed(session, client, auth):
    owned = await _imported(session, client, auth, [30.0])
    consigned = await _imported(session, client, auth, [40.0])
    draft = await _imported(session, client, auth, [50.0], status="draft")
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": consigned[0]},
                            populate_existing=True)
    row.state = {**row.state, "consignment_flag": "in"}  # held for a consignor, as goods received on consignment
    await session.commit()
    await _older_release(session, auth)
    await _marked(session, auth)
    await _startup(session)
    assert await _accounts(session, auth, owned + consigned + draft) == ["1130-OB", None, None]


# --- Moving placed lots in bulk ---------------------------------------------------------


async def test_moving_lots_moves_their_value_with_one_entry(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0, 30.0])
    await _startup(session)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert (result["moved"], result["value"], result["code"]) == (3, 60.0, "1130-P")
    assert await _accounts(session, auth, lots) == ["1130-P"] * 3
    (entry,) = await _entries(session, auth, f"je:auto:older-stock-moved:{result['move_id']}")
    assert _lines(entry) == {"1130-P": (60.0, 0.0), "1130-OB": (0.0, 60.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (60.0, 0.0)
    assert await _off_books(session, auth) == []

    # A lot already on the account is skipped: nothing moves, nothing is booked.
    r = await _move(client, auth, lots[:1], "1130-P")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["moved"] == 0
    assert len(await _entries(session, auth, "je:auto:older-stock-moved:")) == 1
    view = r.json()
    assert {lot["item_id"]: lot["account"] for lot in view["lots"]} == dict.fromkeys(lots, "1130-P")
    assert view["moves"][0]["count"] == 3 and view["moves"][0]["code"] == "1130-P"


async def test_a_move_naming_no_lots_is_refused(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    _refused(await _move(client, auth, [], "1130-P"), 422, "older_stock.move.nothing_selected")
    _refused(await _move(client, auth, [f"item:{i}" for i in range(10001)], "1130-P"), 422,
             "older_stock.move.too_many")
    for key in (None, "", " ", "k" * 65):
        _refused(await _move(client, auth, lots, "1130-P", key=key), 422, "older_stock.move.no_key")
    assert await _events(session, auth) == events


async def test_a_move_to_no_account_is_refused(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    _refused(await _move(client, auth, lots, "  "), 422, "posting.choose_account")
    assert await _events(session, auth) == events


async def test_a_move_to_an_account_that_cannot_hold_inventory_is_refused(session, client, auth):
    from celerp_accounting.models import Account

    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    await _new_inventory_account(client, auth, "1131")
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "1131")
                          .values(is_active=False))
    await session.commit()
    events = await _events(session, auth)
    _refused(await _move(client, auth, lots, "1120"), 422, "posting.older_stock.never_inventory")
    _refused(await _move(client, auth, lots, "1999"), 422, "posting.older_stock.not_in_chart")
    r = await _move(client, auth, lots, "1131")
    assert r.status_code == 422, r.text
    assert "1131" in r.json()["detail"]["message"]
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_a_lot_with_no_account_or_not_owned_cannot_be_moved(session, client, auth):
    from test_posting_roles_lots import _lot

    placed = await _older_company(session, client, auth, [10.0])
    consigned = await _imported(session, client, auth, [40.0])
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": consigned[0]},
                            populate_existing=True)
    row.state = {**row.state, "consignment_flag": "in"}
    await session.commit()
    await _startup(session)
    bought = await _lot(client, auth, 5.0, sku="NEW-1")  # records its account when it is made
    events = await _events(session, auth)
    for lot in (consigned[0], bought):
        detail = _refused(await _move(client, auth, [*placed, lot], "1130-P"), 422, "older_stock.move.not_placed")
        assert detail["params"]["sku"] in ("OLD-00000", "NEW-1")
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, placed) == ["1130-OB"]


async def test_a_lot_an_unshipped_invoice_holds_cannot_be_moved(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    inv = await _sell(client, auth, (lots[0], 1))
    events = await _events(session, auth)
    detail = _refused(await _move(client, auth, lots, "1130-P"), 409, "older_stock.move.invoiced")
    assert detail["params"]["sku"] == "OLD-00000" and detail["params"]["doc"] == inv
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]


async def test_a_move_on_a_locked_day_writes_nothing(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    await _settings(session, auth, lock_date=_today())
    events = await _events(session, auth)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 422, r.text
    await session.rollback()
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_a_move_sent_twice_moves_once(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    key = uuid.uuid4().hex
    first = await _move(client, auth, lots, "1130-P", key=key)
    assert first.status_code == 200, first.text
    events = await _events(session, auth)
    again = await _move(client, auth, lots, "1130-P", key=key)
    assert again.status_code == 200, again.text
    assert again.json()["result"]["move_id"] == first.json()["result"]["move_id"]
    assert await _events(session, auth) == events
    assert len(await _entries(session, auth, "je:auto:older-stock-moved:")) == 1
    assert await _net(session, auth, "1130-P", "1130-OB") == (30.0, 0.0)


async def test_a_lot_moved_since_cannot_be_moved_from_its_old_account(session, client, auth):
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    lots = await _older_company(session, client, auth, [10.0])
    await _startup(session)
    events = await _events(session, auth)
    with pytest.raises(HTTPException) as refused:
        await emit_event(session, company_id=auth["company_id"], entity_id=lots[0], entity_type="item",
                         event_type="item.inventory_account.moved",
                         data={_FIELD: "1131", "from_account": "1130-P"}, actor_id=auth["user_id"],
                         location_id=None, source="system", idempotency_key=f"lot-move:stale:{lots[0]}",
                         metadata_={"move": "stale"})
    await session.rollback()
    assert refused.value.status_code == 409
    assert refused.value.detail["message_key"] == "inventory.account_moved_since"
    assert await _events(session, auth) == events
    assert await _accounts(session, auth, lots) == ["1130-OB"]


async def test_undo_puts_lots_back_once_and_refuses_after_a_later_move(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0])
    await _startup(session)
    r = await _move(client, auth, lots, "1130-P")
    assert r.status_code == 200, r.text
    move = r.json()["result"]["move_id"]

    r = await client.post(f"/accounting/older-stock/moves/{move}/undo", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lots) == ["1130-OB", "1130-OB"]
    (entry,) = await _entries(session, auth, f"je:auto:older-stock-moved:{move}:undo")
    assert _lines(entry) == {"1130-OB": (30.0, 0.0), "1130-P": (0.0, 30.0)}
    assert await _net(session, auth, "1130-P", "1130-OB") == (0.0, 30.0)
    moved = {m["move_id"]: m for m in r.json()["moves"]}
    assert moved[move]["undone"] and moved[f"{move}:undo"]["undo_of"] == move
    _refused(await client.post(f"/accounting/older-stock/moves/{move}/undo", headers=auth["headers"]), 409,
             "older_stock.undo.done")

    r = await _move(client, auth, lots, "1130-P")
    later = r.json()["result"]["move_id"]
    await _new_inventory_account(client, auth, "1131")
    assert (await _move(client, auth, lots[:1], "1131")).status_code == 200
    events = await _events(session, auth)
    detail = _refused(await client.post(f"/accounting/older-stock/moves/{later}/undo", headers=auth["headers"]),
                      409, "older_stock.undo.moved_since")
    assert detail["params"]["sku"] == "OLD-00000"
    assert await _events(session, auth) == events
    _refused(await client.post("/accounting/older-stock/moves/nothing/undo", headers=auth["headers"]), 404,
             "older_stock.undo.no_move")


async def test_the_placement_adjustment_can_be_reversed_and_booked_again(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0], short=10.0)
    await _startup(session)
    (placement,) = (await _view(client, auth))["placements"]
    assert placement["adjustment"] == 10.0 and placement["booked"] is True and placement["count"] == 1
    path = f"/accounting/older-stock/placements/{placement['pid']}/adjustment"

    r = await client.post(path, headers=auth["headers"], json={"booked": False})
    assert r.status_code == 200, r.text
    assert r.json()["placements"][0]["booked"] is False
    assert await _net(session, auth, "1130-OB") == (20.0,)
    events = await _events(session, auth)
    assert (await client.post(path, headers=auth["headers"], json={"booked": False})).status_code == 200
    assert await _events(session, auth) == events

    r = await client.post(path, headers=auth["headers"], json={"booked": True})
    assert r.status_code == 200, r.text
    assert await _net(session, auth, "1130-OB") == (30.0,)
    toggles = await _entries(session, auth, f"je:auto:older-stock-placed:{placement['pid']}:t")
    assert [_lines(t) for t in toggles] == [{"3200": (10.0, 0.0), "1130-OB": (0.0, 10.0)},
                                            {"1130-OB": (10.0, 0.0), "3200": (0.0, 10.0)}]
    assert await _accounts(session, auth, lots) == ["1130-OB"]
    _refused(await client.post("/accounting/older-stock/placements/nothing/adjustment", headers=auth["headers"],
                               json={"booked": False}), 404, "older_stock.no_adjustment")


async def test_older_stock_routes_need_manage_accounting(session, client, auth):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from test_helpers import make_authed_token

    cid, viewer = auth["company_id"], uuid.uuid4()
    session.add(User(id=viewer, email=f"viewer-{viewer.hex[:8]}@test.co", name="Viewer", auth_hash="x",
                     is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=viewer, company_id=cid, role="viewer", is_active=True))
    await session.commit()
    headers = {"Authorization": f"Bearer {await make_authed_token(session, str(viewer), str(cid), 'viewer')}"}
    for method, path, body in (("get", "/accounting/older-stock", None),
                               ("post", "/accounting/older-stock/move", {"item_ids": ["x"], "code": "1130-P",
                                                                         "key": "k"}),
                               ("post", "/accounting/older-stock/moves/m/undo", None),
                               ("post", "/accounting/older-stock/placements/p/adjustment", {"booked": False})):
        r = await getattr(client, method)(path, headers=headers, **({"json": body} if body else {}))
        assert r.status_code == 403, (path, r.status_code, r.text)


# --- Settings > Accounting > Older stock -----------------------------------------------


_VIEW = {
    "lots": [{"item_id": "item:old-1", "sku": "OLD-1", "name": "Older lot", "account": "1130-OB", "value": 30.0,
              "placement": "p1"},
             {"item_id": "item:old-2", "sku": "OLD-2", "name": "", "account": "1130-OB", "value": 1234.5,
              "placement": "p1"}],
    "placements": [{"pid": "p1", "day": "2026-10-14", "count": 2, "account": "1130-OB", "adjustment": 10.0,
                    "booked": True}],
    "moves": [{"move_id": "m1", "day": "2026-10-14", "code": "1130-P", "count": 1, "value": 5.0, "undone": False,
               "undo_of": None}],
    "candidates": [{"code": "1130-OB", "name": "Inventory opening", "account_type": "asset"},
                   {"code": "1130-P", "name": "Inventory purchased", "account_type": "asset"}],
    "accounts": ["1130-OB"],
    "currency": "USD",
}
_EMPTY = {**_VIEW, "lots": [], "placements": [], "moves": [], "accounts": []}


@pytest.fixture
async def ui_client():
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _cookies() -> dict:
    from test_helpers import make_test_token

    return {"celerp_token": make_test_token(role="owner")}


async def _tab(ui_client, view=None, error=None, query: str = "") -> tuple[str, AsyncMock]:
    company = {"name": "Test Corp", "current_role": "owner", "settings": {}}
    get = AsyncMock(return_value=view, side_effect=error)
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_bank_accounts", new=AsyncMock(return_value={"items": []})), \
         patch("ui.api_client.get_older_stock", new=get):
        r = await ui_client.get(f"{_TAB}{query}", cookies=_cookies())
    assert r.status_code == 200
    return r.content.decode(), get


async def test_the_older_stock_tab_lists_placed_lots_and_its_empty_state(ui_client):
    body, get = await _tab(ui_client, _VIEW, query="&account=1130-OB&q=OLD")
    assert get.await_args.args[1] == {"account": "1130-OB", "q": "OLD"}
    assert f'href="{_TAB}"' in body
    assert 'id="older-stock-table"' in body and "data-operation-key" in body
    assert "bulk-select-all" in body and 'value="item:old-1"' in body
    assert "OLD-1" in body and "Older lot" in body and ">--<" in body
    assert "$1,234.50" in body
    # The shared bulk toolbar posts the ticked lots to the move route (its action list carries the URL).
    assert 'id="bulkbar-older-stock-table"' in body and "/settings/accounting/older-stock/move" in body
    assert "Move to account" in body
    assert "/settings/accounting/older-stock/moves/m1/undo" in body
    assert "/settings/accounting/older-stock/placements/p1/adjustment" in body
    assert ">—<" not in body  # an empty value shows "--", never a dash glyph

    body, _ = await _tab(ui_client, _EMPTY)
    assert "No older stock was placed automatically." in body
    assert "No moves yet." in body


async def test_the_older_stock_tab_shows_an_api_failure_as_an_error(ui_client):
    from ui.api_client import APIError

    body, _ = await _tab(ui_client, error=APIError(500, "The books could not be read"))
    assert 'class="error-banner"' in body and "The books could not be read" in body


async def test_the_bulk_move_re_renders_the_card_with_a_flash(ui_client):
    from ui.api_client import APIError

    moved = {**_VIEW, "result": {"moved": 2, "value": 1264.5, "code": "1130-P", "move_id": "m2"}}
    move = AsyncMock(return_value=moved)
    with patch("ui.api_client.move_older_stock", new=move):
        r = await ui_client.post("/settings/accounting/older-stock/move", cookies=_cookies(),
                                 data={"selected": ["item:old-1", "item:old-2"], "code": "1130-P",
                                       "idempotency_key": "op-1"})
    assert r.status_code == 200
    body = r.content.decode()
    assert move.await_args.args[1:] == (["item:old-1", "item:old-2"], "1130-P", "op-1")
    assert 'id="older-stock-card"' in body and "Moved 2 lots to 1130-P." in body
    assert "hx-redirect" not in r.headers

    refused = AsyncMock(side_effect=APIError(409, "OLD-1 is on an invoice not yet shipped (doc:1)."))
    with patch("ui.api_client.move_older_stock", new=refused), \
         patch("ui.api_client.get_older_stock", new=AsyncMock(return_value=_VIEW)):
        r = await ui_client.post("/settings/accounting/older-stock/move", cookies=_cookies(),
                                 data={"selected": ["item:old-1"], "code": "1130-P", "idempotency_key": "op-2"})
    body = r.content.decode()
    assert r.status_code == 200 and 'id="older-stock-card"' in body
    assert "OLD-1 is on an invoice not yet shipped (doc:1)." in body


def test_older_stock_notices_translate():
    locales = Path(__file__).resolve().parents[1] / "ui" / "locales"
    keys = ("notice.older_stock_placed.title", "notice.older_stock_placed.body",
            "notice.older_stock_placed.body_adjusted", "notice.older_stock_unmapped.title",
            "notice.older_stock_unmapped.body_purchased", "notice.older_stock_unmapped.body_retained",
            "event.item.inventory_account.moved", "older_stock.tab", "older_stock.move",
            "older_stock.move.nothing_selected", "older_stock.move.too_many", "older_stock.move.no_key",
            "older_stock.move.not_placed", "older_stock.move.invoiced", "older_stock.undo.done",
            "older_stock.undo.moved_since", "older_stock.undo.no_move", "older_stock.no_adjustment",
            "inventory.account_moved_since")
    for lang in _LOCALES:
        catalog = json.loads((locales / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in keys if not catalog.get(k)]
        assert missing == [], (lang, missing)
        assert "notice.older_stock_unplaced.body" not in catalog, lang
        for key in keys:
            assert "—" not in catalog[key], (lang, key)
    en = json.loads((locales / "en.json").read_text(encoding="utf-8"))
    assert "Choose its inventory account" not in en["notice.older_stock_locked.body"]
