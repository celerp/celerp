# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Stock entered in Celerp is on the books the moment it exists.

A new item starts as a draft. Created as available, it is made available in the
same request, so its value is booked as opening stock before the response, and
anything that stops that entry (posting accounts, a locked day) stops the creation.
No other starting status can be entered by hand.

An import books the stock it brings in once, in one entry for the whole import; a
retry books nothing more, a draft row waits until it is made available, and a full
snapshot of another system's item records no account and books nothing. Migrated
stock keeps the account its source books held it in. With Accounting off nothing is
booked, and turning Accounting on later books the imported stock as opening stock.

Items from an accounting system arrive as drafts when they carry a cost and available
when they do not (test_connector_item_cost_rule). Items from an online store arrive
available and record the opening inventory account. The sample items a new company
starts with are on the books from the start, and removing or replacing them takes
exactly their value off again, never the value of an import that replaces them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from stock_books import assert_books_carry_stock
from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_draft_stock import _inactive, _settings, _unmapped, _wrong_type
from test_posting_roles_older_stock import _make_available, _without_accounting
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_RE = "3200"


# --- helpers ---------------------------------------------------------------------------

async def _events(session, company_id) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id))


async def _opening_entries(session, company_id) -> list[dict]:
    """Posted entries that bring stock onto the books against retained earnings, or take it off."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry"))).scalars()
    return [r.state for r in rows if (r.state or {}).get("status") == "posted"
            and any(e.get("account") == _RE for e in r.state.get("entries") or [])]


async def _items_by_sku(session, company_id, sku: str) -> list[Projection]:
    session.expire_all()
    return list((await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.state["sku"].as_string() == sku))).scalars())


async def _create(client, auth, **fields):
    body = {"sku": f"IN-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 2, "sell_by": "piece", **fields}
    return await client.post("/items", headers=auth["headers"], json=body)


async def _import_rows(client, auth, rows: list[dict], key: str | None = None):
    return await client.post("/items/import/rows", headers=auth["headers"], json={
        "rows": rows, "upsert": False, "idempotency_key": key or f"op-{uuid.uuid4().hex[:8]}"})


def _row(sku: str, cost: float | None, qty: int = 2) -> dict:
    row = {"sku": sku, "name": f"Imported {sku}", "sell_by": "piece", "quantity": str(qty), "location_name": "Main"}
    if cost is not None:
        row["cost_price"] = str(cost)
    return row


async def _raw(client, auth, *records: dict):
    return await client.post("/items/import/batch", headers=auth["headers"], json={"records": list(records)})


def _raw_record(event_type: str, cost: float, source: str = "csv", status: str = "available") -> dict:
    key = uuid.uuid4().hex[:8]
    return {"entity_id": f"item:raw-{key}", "event_type": event_type, "source": source,
            "idempotency_key": f"raw-{key}",
            "data": {"sku": f"RAW-{key}", "name": "Raw", "quantity": 1, "sell_by": "piece",
                     "status": status, "cost_total": cost}}


# --- Creating an item ------------------------------------------------------------------


async def test_an_item_created_without_a_status_is_a_draft_with_nothing_booked(session, client, auth):
    r = await _create(client, auth, cost_total=200.0)
    assert r.status_code == 200, r.text
    state = await _state(session, auth, r.json()["id"])
    assert (state["status"], state.get(_FIELD)) == ("draft", None)
    assert await _opening_entries(session, auth["company_id"]) == []
    await assert_books_carry_stock(session, auth["company_id"])


async def test_an_item_created_available_is_booked_as_opening_stock_before_the_response(session, client, auth):
    r = await _create(client, auth, cost_total=200.0, status="available")
    assert r.status_code == 200, r.text
    state = await _state(session, auth, r.json()["id"])
    assert (state["status"], state.get(_FIELD)) == ("available", "1130-OB")
    # No report is opened: the entry was posted by the create itself.
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 200}
    assert await _account_net(session, auth["company_id"], _RE) == -200.0



async def test_a_component_created_available_is_booked_and_a_build_takes_it_off_the_books(session, client, auth):
    """Components are goods the company holds like any other stock: booked as they enter,
    and their cost moves off the books into what is made from them."""
    gold = (await _create(client, auth, quantity=100, sell_by="gram", cost_total=8000.0, status="available",
                          inventory_type="component")).json()["id"]
    assert (await _state(session, auth, gold)).get(_FIELD) == "1130-OB"
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 8000}
    ring = (await _create(client, auth, quantity=0, status="available")).json()["id"]
    r = await client.put(f"/manufacturing/items/{ring}/recipe", headers=auth["headers"], json={
        "output_qty": 1, "components": [{"item_id": gold, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    r = await client.post(f"/manufacturing/items/{ring}/build", headers=auth["headers"],
                          json={"quantity": 2, "complete": True})
    assert r.status_code == 200, r.text
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 800, "1130-OB": 7200}

@pytest.mark.parametrize("problem", [_unmapped, _inactive, _wrong_type])
async def test_an_item_created_available_is_not_created_when_the_opening_account_cannot_take_it(
        session, client, auth, problem):
    await problem(session, auth)
    before = await _events(session, auth["company_id"])
    r = await _create(client, auth, cost_total=200.0, status="available", sku="NOPE-1")
    assert r.status_code == 409, r.text
    assert "Settings > Accounting > Posting accounts" in r.json()["detail"]["message"]
    await session.rollback()  # the refused request's work ends with it, as its own session would
    assert await _events(session, auth["company_id"]) == before
    assert await _items_by_sku(session, auth["company_id"], "NOPE-1") == []


async def test_an_item_created_available_on_a_locked_day_is_not_created(session, client, auth):
    await _settings(session, auth, timezone="UTC", lock_date=datetime.now(timezone.utc).date().isoformat())
    before = await _events(session, auth["company_id"])
    r = await _create(client, auth, cost_total=200.0, status="available", sku="LOCKED-1")
    assert r.status_code == 422, r.text
    assert "Period is locked through" in r.json()["detail"]
    assert await _events(session, auth["company_id"]) == before
    assert await _items_by_sku(session, auth["company_id"], "LOCKED-1") == []


@pytest.mark.parametrize("status", ["sold", "merged", "expired", "archived", "disposed", "memo_out", "returned",
                                    "fulfilled"])
async def test_an_item_cannot_be_created_in_a_status_only_an_action_reaches(session, client, auth, status):
    before = await _events(session, auth["company_id"])
    r = await _create(client, auth, cost_total=200.0, status=status)
    assert r.status_code == 422, r.text
    assert "draft or available" in r.json()["detail"]
    assert await _events(session, auth["company_id"]) == before


async def test_a_retried_available_create_makes_one_item_booked_once(session, client, auth):
    body = {"cost_total": 200.0, "status": "available", "sku": "RETRY-1", "idempotency_key": "create-retry-1"}
    first = await _create(client, auth, **body)
    second = await _create(client, auth, **body)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]
    assert len(await _items_by_sku(session, auth["company_id"], "RETRY-1")) == 1
    assert len(await _opening_entries(session, auth["company_id"])) == 1
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 200}


# --- Imports ---------------------------------------------------------------------------


async def test_an_import_books_its_stock_once_in_one_entry(session, client, auth):
    rows = [_row("IMP-A", 50.0), _row("IMP-B", 25.0), _row("IMP-C", 10.0)]
    r = await _import_rows(client, auth, rows, key="imp-once")
    assert r.status_code == 200 and not r.json()["errors"], r.text
    for sku in ("IMP-A", "IMP-B", "IMP-C"):
        [lot] = await _items_by_sku(session, auth["company_id"], sku)
        assert lot.state.get(_FIELD) == "1130-OB"
    assert len(await _opening_entries(session, auth["company_id"])) == 1
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 170}

    retry = await _import_rows(client, auth, rows, key="imp-once")
    assert retry.status_code == 200, retry.text
    assert retry.json()["created"] == 0
    assert len(await _opening_entries(session, auth["company_id"])) == 1
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 170}


async def test_imported_stock_with_no_cost_records_its_account_and_books_no_money(session, client, auth):
    r = await _import_rows(client, auth, [_row("IMP-ZERO", None)])
    assert r.status_code == 200 and not r.json()["errors"], r.text
    [lot] = await _items_by_sku(session, auth["company_id"], "IMP-ZERO")
    assert lot.state.get(_FIELD) == "1130-OB"
    assert await _opening_entries(session, auth["company_id"]) == []
    await assert_books_carry_stock(session, auth["company_id"])


async def test_an_imported_draft_is_booked_only_when_it_is_made_available(session, client, auth):
    draft = _raw_record("item.created", 40.0, status="draft")
    r = await _raw(client, auth, draft)
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    state = await _state(session, auth, draft["entity_id"])
    assert (state["status"], state.get(_FIELD)) == ("draft", None)
    assert await _opening_entries(session, auth["company_id"]) == []
    await _make_available(client, auth, draft["entity_id"])
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 40}


async def test_an_import_the_opening_account_cannot_take_writes_nothing(session, client, auth):
    await _unmapped(session, auth)
    before = await _events(session, auth["company_id"])
    r = await _import_rows(client, auth, [_row("IMP-NOPE", 50.0)])
    assert r.status_code == 409, r.text
    await session.rollback()  # the refused request's work ends with it, as its own session would
    assert await _events(session, auth["company_id"]) == before
    assert await _items_by_sku(session, auth["company_id"], "IMP-NOPE") == []


async def test_a_raw_batch_books_its_local_creates_and_leaves_snapshots_unplaced(session, client, auth):
    local, snapshot = _raw_record("item.created", 30.0), _raw_record("item.snapshot", 5.0, source="import:bundle")
    r = await _raw(client, auth, local, snapshot)
    assert r.status_code == 200 and r.json()["created"] == 2, r.text
    assert (await _state(session, auth, local["entity_id"])).get(_FIELD) == "1130-OB"
    assert (await _state(session, auth, snapshot["entity_id"])).get(_FIELD) is None
    [entry] = await _opening_entries(session, auth["company_id"])
    assert sum(e["credit"] for e in entry["entries"] if e["account"] == _RE) == 30.0
    await assert_books_carry_stock(session, auth["company_id"], unplaced={snapshot["entity_id"]})


async def test_a_snapshot_followed_by_a_refused_create_of_the_same_item_books_nothing(session, client, auth):
    snapshot = _raw_record("item.snapshot", 70.0, source="import:bundle")
    create = {**_raw_record("item.created", 70.0), "entity_id": snapshot["entity_id"]}
    r = await _raw(client, auth, snapshot, create)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], len(r.json()["errors"])) == (1, 1), r.text
    assert (await _state(session, auth, snapshot["entity_id"])).get(_FIELD) is None
    assert await _opening_entries(session, auth["company_id"]) == []


async def test_a_raw_snapshot_alone_books_nothing_and_records_no_account(session, client, auth):
    snapshot = _raw_record("item.snapshot", 5.0, source="import:bundle")
    r = await _raw(client, auth, snapshot)
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert (await _state(session, auth, snapshot["entity_id"])).get(_FIELD) is None
    assert await _opening_entries(session, auth["company_id"]) == []


async def test_an_import_with_accounting_off_books_nothing_until_accounting_is_turned_on(session, client, auth):
    await _without_accounting(session, auth)
    r = await _import_rows(client, auth, [_row("IMP-OFF", 30.0)])
    assert r.status_code == 200 and not r.json()["errors"], r.text
    [lot] = await _items_by_sku(session, auth["company_id"], "IMP-OFF")
    assert lot.state.get(_FIELD) is None
    await assert_books_carry_stock(session, auth["company_id"])
    await _startup(session)
    [lot] = await _items_by_sku(session, auth["company_id"], "IMP-OFF")
    assert lot.state.get(_FIELD) == "1130-OB"
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 60}


async def test_migrated_stock_keeps_its_source_account_and_the_import_books_no_opening_stock(session):
    from decimal import Decimal

    from celerp.importers.schema import CIFItem
    from test_posting_roles_migration_sinks import _PROVENANCE, _import_chart, _staged_context, _via

    context = await _staged_context(session)
    await _import_chart(context)
    item = CIFItem(**_PROVENANCE, source_type="InventoryItem", source_external_id="item-ingress", sku="WID-9",
                   name="Widget", status="available", total_cost=Decimal("40"))
    result = await _via(context, "items", [item])
    assert result.errors == []
    row = await session.get(Projection, (context.company_id, result.mappings[0].target_entity_id))
    assert row.state[_FIELD] == "130"
    assert await _opening_entries(session, context.company_id) == []


# --- Connectors ------------------------------------------------------------------------


@pytest.fixture
def connector_session(session, monkeypatch):
    """The connector services open their own session; point it at the test session."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _local():
        yield session

    monkeypatch.setattr("celerp.db.SessionLocal", _local)
    return session


async def test_an_item_with_a_cost_from_an_accounting_system_arrives_as_a_draft_with_nothing_booked(
        connector_session, auth):
    import celerp.connectors.upsert as connector
    from celerp_inventory.routes import ItemCreate

    session = connector_session
    outcome = await connector.upsert_item(str(auth["company_id"]), ItemCreate(
        sku="QB-1", name="From the books", sell_by="piece", quantity=3, cost_price=20.0, sale_price=None,
        idempotency_key="quickbooks:item-1"))
    assert outcome == "created"
    [lot] = await _items_by_sku(session, auth["company_id"], "QB-1")
    assert (lot.state["status"], lot.state.get(_FIELD)) == ("draft", None)
    assert await _opening_entries(session, auth["company_id"]) == []
    await assert_books_carry_stock(session, auth["company_id"])

    again = await connector.upsert_item(str(auth["company_id"]), ItemCreate(
        sku="QB-1", name="From the books, renamed", sell_by="piece", quantity=3, cost_price=20.0, sale_price=None,
        idempotency_key="quickbooks:item-1"))
    assert again == "updated"
    [lot] = await _items_by_sku(session, auth["company_id"], "QB-1")
    assert lot.state["status"] == "draft"


async def test_an_available_item_from_an_accounting_system_stays_available_on_update(connector_session, client, auth):
    import celerp.connectors.upsert as connector
    from celerp_inventory.routes import ItemCreate

    session = connector_session
    await connector.upsert_item(str(auth["company_id"]), ItemCreate(
        sku="QB-2", name="From the books", sell_by="piece", quantity=1, cost_price=20.0, sale_price=None,
        idempotency_key="quickbooks:item-2"))
    [lot] = await _items_by_sku(session, auth["company_id"], "QB-2")
    await _make_available(client, auth, lot.entity_id)
    await connector.upsert_item(str(auth["company_id"]), ItemCreate(
        sku="QB-2", name="Renamed", sell_by="piece", quantity=1, cost_price=20.0, sale_price=None,
        idempotency_key="quickbooks:item-2"))
    [lot] = await _items_by_sku(session, auth["company_id"], "QB-2")
    assert (lot.state["status"], lot.state["name"]) == ("available", "Renamed")
    assert await assert_books_carry_stock(session, auth["company_id"]) == {"1130-P": 0, "1130-OB": 20}


async def test_a_store_product_arrives_available_on_the_opening_account_with_no_money_booked(
        connector_session, auth):
    from celerp_inventory.services import upsert_external_product

    session = connector_session
    outcome, item_id = await upsert_external_product(
        str(auth["company_id"]), platform="shopify", product_id="1001", variation_id=None, sku="SHOP-1",
        name="Store product", sale_price=30.0, quantity=4, seed_quantity=True)
    assert outcome == "created"
    state = await _state(session, auth, item_id)
    assert (state["status"], state.get(_FIELD)) == ("available", "1130-OB")
    assert await _opening_entries(session, auth["company_id"]) == []
    await assert_books_carry_stock(session, auth["company_id"])


# --- Sample items ------------------------------------------------------------------------


async def _register(client, session) -> dict:
    from celerp.models.company import Company

    r = await client.post("/auth/register", json={
        "company_name": "SampleCo", "email": f"sample-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = await client.get("/companies/me", headers=headers)
    assert me.status_code == 200, me.text
    company_id = uuid.UUID(me.json()["id"])
    assert "posting_roles_schema" in ((await session.get(Company, company_id)).settings or {})
    return {"headers": headers, "company_id": company_id}


async def _sample_value(session, company_id) -> float:
    from celerp.services.lot_origin import held_value

    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == "item",
        Projection.entity_id.startswith("item:demo-")))).scalars()
    return round(float(sum(held_value(r) or 0 for r in rows)), 2)  # the books carry currency precision


async def test_sample_items_are_on_the_books_from_the_start(session, client):
    owner = await _register(client, session)
    value = await _sample_value(session, owner["company_id"])
    assert value > 0
    books = await assert_books_carry_stock(session, owner["company_id"])
    assert float(sum(books.values())) == value


async def test_replacing_untouched_sample_items_leaves_the_books_equal_to_the_new_set(session, client):
    owner = await _register(client, session)
    r = await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["changes"]["demo_items_replaced"] > 0
    value = await _sample_value(session, owner["company_id"])
    books = await assert_books_carry_stock(session, owner["company_id"])
    assert float(sum(books.values())) == value
    assert await _account_net(session, owner["company_id"], _RE) == -value


async def test_the_first_import_takes_off_exactly_the_sample_value_and_books_its_own(session, client):
    owner = await _register(client, session)
    r = await _import_rows(client, owner, [_row("REAL-1", 50.0), _row("REAL-2", 25.0)])
    assert r.status_code == 200 and not r.json()["errors"], r.text
    assert await _sample_value(session, owner["company_id"]) == 0
    assert await assert_books_carry_stock(session, owner["company_id"]) == {"1130-P": 0, "1130-OB": 150}
    assert await _account_net(session, owner["company_id"], _RE) == -150.0


# --- What the system records about a lot is never entered -------------------------------

_FORGED = {_FIELD: "1130-P"}


@pytest.mark.parametrize("status", ["draft", "available"])
async def test_an_item_cannot_be_created_with_an_inventory_account(session, client, auth, status):
    before = await _events(session, auth["company_id"])
    r = await _create(client, auth, cost_total=200.0, status=status, **_FORGED)
    assert r.status_code == 422 and _FIELD in r.text, r.text
    await session.rollback()
    assert await _events(session, auth["company_id"]) == before
    assert await _opening_entries(session, auth["company_id"]) == []


@pytest.mark.parametrize("event_type", ["item.created", "item.snapshot"])
async def test_a_raw_import_cannot_carry_an_inventory_account(session, client, auth, event_type):
    record = _raw_record(event_type, 50.0)
    record["data"].update(_FORGED)
    r = await _raw(client, auth, record)
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert _FIELD in r.text, r.text
    assert await _items_by_sku(session, auth["company_id"], record["data"]["sku"]) == []
    assert await _opening_entries(session, auth["company_id"]) == []


async def test_an_import_row_cannot_carry_an_inventory_account(session, client, auth):
    r = await _import_rows(client, auth, [{**_row("FORGED-R", 50.0), **_FORGED}])
    assert r.status_code == 422 and _FIELD in r.text, r.text
    assert await _items_by_sku(session, auth["company_id"], "FORGED-R") == []
    assert await _opening_entries(session, auth["company_id"]) == []


async def test_an_edit_cannot_set_an_inventory_account(session, client, auth):
    item = (await _create(client, auth, cost_total=200.0, status="available")).json()["id"]
    before = await _events(session, auth["company_id"])
    for change in ({_FIELD: {"old": "1130-OB", "new": "1130-P"}},
                   {"attributes": {"old": {}, "new": _FORGED}}):
        r = await client.patch(f"/items/{item}", headers=auth["headers"], json={"fields_changed": change})
        assert r.status_code == 422 and _FIELD in r.text, r.text
    await session.rollback()
    assert await _events(session, auth["company_id"]) == before
    assert (await _state(session, auth, item))[_FIELD] == "1130-OB"


async def test_a_duplicated_item_starts_with_none_of_the_original_s_system_fields(session, client, auth):
    """Duplicate copies what a user entered; the copy is a new draft with no account,
    files or document links of its own, and the create accepts it."""
    from ui.routes.inventory import _duplicate_payload

    item = (await _create(client, auth, cost_total=200.0, status="available")).json()["id"]
    source = (await client.get(f"/items/{item}", headers=auth["headers"])).json()
    r = await client.post("/items", headers=auth["headers"],
                          json=_duplicate_payload(source, "DUP-1", can_set_prices=True))
    assert r.status_code == 200, r.text
    copy = await _state(session, auth, r.json()["id"])
    assert copy["status"] == "draft" and copy.get("cost_total") == 200.0
    assert _FIELD not in copy and _FIELD not in (copy.get("attributes") or {})
