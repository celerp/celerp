# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Delete removes a draft that was a mistake, never stock.

Only a draft that never became stock and is used nowhere can be deleted, and it goes
without a trace. Anything else refuses the whole selection with the action that fits
instead: Revert to Draft, Archive, or Write Off Stock. Deleting draft documents never
deletes an item.

Undoing an import takes off the books exactly the opening stock that import booked,
in the same step that removes its items, or refuses and changes nothing.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_helpers import sell_item
from test_posting_roles_draft_stock import _settings
from test_posting_roles_ingress import _import_rows, _items_by_sku, _row
from test_posting_roles_kept_stock import _available, _bulk_status, _ok, _write_off
from test_posting_roles_older_stock import _without_accounting
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_ACTIONS = ("Revert to Draft", "Archive", "Write Off Stock")


# --- helpers ---------------------------------------------------------------------------

async def _draft(client, auth, cost: float = 40.0) -> str:
    return (await _ok(client, auth, "POST", "/items", {
        "sku": f"DEL-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 1, "sell_by": "piece",
        "cost_total": cost}))["id"]


async def _delete(client, auth, *lots: str):
    return await client.post("/items/bulk/delete", headers=auth["headers"], json={"entity_ids": list(lots)})


async def _exists(session, auth, lot: str) -> bool:
    session.expire_all()
    return await session.get(Projection, {"company_id": auth["company_id"], "entity_id": lot}) is not None


async def _events(session, auth, entity_id: str) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == entity_id))


async def _refused(session, client, auth, *lots: str) -> None:
    """Deleting ``lots`` is refused whole, names the actions that fit, and changes nothing."""
    books = await assert_books_carry_stock(session, auth["company_id"])
    r = await _delete(client, auth, *lots)
    assert r.status_code == 409, r.text
    assert all(action in r.json()["detail"] for action in _ACTIONS), r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    for lot in lots:
        assert await _exists(session, auth, lot)
    assert await assert_books_carry_stock(session, auth["company_id"]) == books


# --- Delete is for drafts that were a mistake ------------------------------------------

async def test_a_draft_that_never_became_stock_is_deleted_without_a_trace(session, client, auth):
    lot = await _draft(client, auth)
    r = await _delete(client, auth, lot)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1
    assert not await _exists(session, auth, lot)
    assert await _events(session, auth, lot) == 0
    await assert_books_carry_stock(session, auth["company_id"])


async def test_available_stock_is_not_deleted(session, client, auth):
    await _refused(session, client, auth, await _available(client, auth, 100.0))


@pytest.mark.parametrize("status", ["archived", "expired"])
async def test_retired_stock_kept_on_the_books_is_not_deleted(session, client, auth, status):
    lot = await _available(client, auth, 100.0)
    if status == "archived":
        assert (await _bulk_status(client, auth, lot, "archived")).status_code == 200
    else:
        await _ok(client, auth, "POST", "/items/bulk/expire", {"entity_ids": [lot]})
    assert (await _state(session, auth, lot))["inventory_on_books"] is True
    await _refused(session, client, auth, lot)


async def test_sold_stock_is_not_deleted(session, client, auth):
    lot = await _available(client, auth, 100.0)
    await sell_item(client, auth["headers"], lot)
    await _refused(session, client, auth, lot)


async def test_written_off_stock_is_not_deleted(session, client, auth):
    lot = await _available(client, auth, 100.0)
    await _write_off(client, auth, lot, 1)
    await _refused(session, client, auth, lot)


async def test_a_draft_that_was_once_stock_keeps_its_history(session, client, auth):
    lot = await _available(client, auth, 100.0)
    await _ok(client, auth, "POST", "/items/bulk/revert-to-draft", {"entity_ids": [lot]})
    assert (await _state(session, auth, lot))["status"] == "draft"
    await _refused(session, client, auth, lot)


async def test_a_draft_on_a_document_is_not_deleted(session, client, auth):
    """A draft cannot be added to a document today, but a document an older release
    wrote can still name one, and deleting the draft would leave the line pointing
    at nothing."""
    lot = await _draft(client, auth)
    await emit_event(session, company_id=auth["company_id"], entity_id=f"doc:{uuid.uuid4()}",
                     entity_type="doc", event_type="doc.created",
                     data={"doc_type": "quotation", "status": "draft", "line_items": [
                         {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 50.0}]},
                     actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=str(uuid.uuid4()), metadata_={})
    await session.commit()
    await _refused(session, client, auth, lot)


async def test_a_selection_with_one_item_that_cannot_go_deletes_nothing(session, client, auth):
    draft, stock = await _draft(client, auth), await _available(client, auth, 100.0)
    await _refused(session, client, auth, draft, stock)


async def test_deleting_draft_documents_never_deletes_an_item(session, client, auth):
    lot = await _draft(client, auth)
    r = await client.delete("/docs/bulk-draft", headers=auth["headers"], params={"doc_ids": lot})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
    assert await _exists(session, auth, lot)


# --- Undoing an import ------------------------------------------------------------------

async def _imported(client, auth, *rows: dict) -> str:
    r = await _import_rows(client, auth, list(rows))
    assert r.status_code == 200 and not r.json()["errors"], r.text
    return r.json()["batch_id"]


async def _undo(client, auth, batch_id: str):
    return await client.post(f"/items/import/batches/{batch_id}/undo", headers=auth["headers"])


async def _recognition(session, auth, batch_id: str) -> list[dict]:
    session.expire_all()
    return [r.state for r in (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:opening-stock:{batch_id}:")))).scalars()]


async def test_undoing_an_import_takes_off_exactly_the_stock_it_booked(session, client, auth):
    kept = await _available(client, auth, 25.0)
    batch = await _imported(client, auth, _row("UND-A", 30.0), _row("UND-B", 10.0, qty=3))
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 115}
    [entry] = await _recognition(session, auth, batch)
    assert entry["status"] == "posted"

    r = await _undo(client, auth, batch)
    assert r.status_code == 200, r.text
    assert await _items_by_sku(session, auth["company_id"], "UND-A") == []
    assert await _items_by_sku(session, auth["company_id"], "UND-B") == []
    [entry] = await _recognition(session, auth, batch)
    assert entry["status"] == "void"
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 25}
    assert await _exists(session, auth, kept)


async def test_an_undone_import_can_be_imported_again_and_is_booked_again(session, client, auth):
    batch = await _imported(client, auth, _row("UND-R", 30.0))
    assert (await _undo(client, auth, batch)).status_code == 200
    again = await _imported(client, auth, _row("UND-R", 30.0))
    [lot] = await _items_by_sku(session, auth["company_id"], "UND-R")
    assert lot.state["inventory_account_code"] == "1130-OB"
    assert [e["status"] for e in await _recognition(session, auth, again)] == ["posted"]
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 60}


async def test_an_import_booked_in_a_locked_period_is_not_undone(session, client, auth):
    batch = await _imported(client, auth, _row("UND-L", 30.0))
    [entry] = await _recognition(session, auth, batch)
    await _settings(session, auth, lock_date=entry["ts"])
    r = await _undo(client, auth, batch)
    assert r.status_code == 422 and "locked" in r.text, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    assert len(await _items_by_sku(session, auth["company_id"], "UND-L")) == 1
    assert [e["status"] for e in await _recognition(session, auth, batch)] == ["posted"]
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 60}


async def test_an_import_whose_items_are_on_a_document_is_not_undone(session, client, auth):
    batch = await _imported(client, auth, _row("UND-D", 30.0))
    [lot] = await _items_by_sku(session, auth["company_id"], "UND-D")
    await _ok(client, auth, "POST", "/docs", {"doc_type": "quotation", "line_items": [
        {"entity_id": lot.entity_id, "name": "Lot", "quantity": 1, "unit_price": 50.0, "sell_by": "piece"}]})
    r = await _undo(client, auth, batch)
    assert r.status_code == 409, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    assert len(await _items_by_sku(session, auth["company_id"], "UND-D")) == 1
    assert [e["status"] for e in await _recognition(session, auth, batch)] == ["posted"]


async def test_an_import_booked_later_by_turning_accounting_on_is_not_undone(session, client, auth):
    """Turning Accounting on booked the imported stock with the rest of the company's
    opening stock, not as the import's own entry, so there is no entry of the import's
    to take back and removing the items would leave their value on the books."""
    await _without_accounting(session, auth)
    batch = await _imported(client, auth, _row("UND-O", 30.0))
    await _startup(session)
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 60}
    r = await _undo(client, auth, batch)
    assert r.status_code == 409, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    assert len(await _items_by_sku(session, auth["company_id"], "UND-O")) == 1
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 60}
