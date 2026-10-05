# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A draft becomes stock in one step, with its value on the books.

A draft is not stock: it records no inventory account and nothing is booked for it.
Making it available records the opening inventory account in use at that moment and
books the draft's value there in the same operation, so it can be sold at once and
the books already agree with the stock without any report being opened. Returning
clean stock to draft takes its value off the books the same way, and making it
available again books it once. If the posting accounts cannot take the entry, or the
day is locked, nothing changes. Every entry is dated the business day the operation
started, even when the clock passes midnight part way through it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from test_cost_restatement import _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lot_origin import _books_match_lots, _remap
from test_posting_roles_lots import _lot, _new_inventory_account
from test_posting_roles_older_stock import _as_older_release, _draft, _sold
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio

_FIELD = "inventory_account_code"
_REPAIR = "Settings > Accounting > Posting accounts"
_LOCKED = "Period is locked through"


async def _move(client, auth, path: str, lot: str):
    return await client.post(f"/items/bulk/{path}", headers=auth["headers"], json={"entity_ids": [lot]})


async def _make_available(client, auth, lot: str) -> None:
    r = await _move(client, auth, "make-available", lot)
    assert r.status_code == 200, r.text


async def _to_draft(client, auth, lot: str) -> None:
    r = await _move(client, auth, "revert-to-draft", lot)
    assert r.status_code == 200, r.text


async def _lot_state(session, auth, lot: str) -> tuple[str, str | None]:
    session.expire_all()
    state = await _state(session, auth, lot)
    return state["status"], state.get(_FIELD)


async def _events(session, auth) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))


async def _entries(session, auth, lot: str) -> list[dict]:
    """The posted entries that moved this lot's value between draft and stock."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(f"je:auto:{lot}:", autoescape=True)))).scalars()
    return [r.state for r in rows if r.state.get("status") == "posted"]


async def _settings(session, auth, **changes) -> None:
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, **changes}
    await session.commit()


# --- Making a draft available books it, with no report needed ---------------------------


async def test_a_draft_made_available_is_booked_and_sells_with_no_report_opened(session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    assert await _lot_state(session, auth, draft) == ("draft", None)
    assert await _account_net(session, auth["company_id"], "1130-OB") == 0.0
    await _make_available(client, auth, draft)
    assert await _lot_state(session, auth, draft) == ("available", "1130-OB")
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 200.0}
    await _sold(client, auth, (draft, 1))
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 100.0}


async def test_a_draft_records_the_opening_account_in_use_when_it_is_made_available(session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    await _remap(session, auth, "inventory_opening", await _new_inventory_account(client, auth, "1132"))
    await _make_available(client, auth, draft)
    assert await _lot_state(session, auth, draft) == ("available", "1132")
    assert await _books_match_lots(session, auth, "1130-OB", "1132") == {"1130-OB": 0.0, "1132": 200.0}
    await _sold(client, auth, (draft, 2))
    assert await _books_match_lots(session, auth, "1130-OB", "1132") == {"1130-OB": 0.0, "1132": 0.0}


# --- An opening account that cannot take the entry stops the whole step ---------------


async def _unmapped(session, auth) -> None:
    company = await locked_company(session, auth["company_id"])
    roles = {k: v for k, v in company.settings["posting_roles"].items() if k != "inventory_opening"}
    company.settings = {**company.settings, "posting_roles": roles}
    await session.commit()


async def _opening_account(session, auth, **values) -> None:
    from celerp_accounting.models import Account
    from sqlalchemy import update

    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "1130-OB")
                          .values(**values))
    await session.commit()


async def _inactive(session, auth) -> None:
    await _opening_account(session, auth, is_active=False)


async def _wrong_type(session, auth) -> None:
    await _opening_account(session, auth, account_type="expense")


async def _new_draft(session, client, auth) -> str:
    return await _draft(client, auth, 200.0, 2)


async def _upgraded_draft(session, client, auth) -> str:
    draft = await _draft(client, auth, 200.0, 2)
    await _as_older_release(session, auth, [draft], [])
    await _startup(session)
    return draft


@pytest.mark.parametrize("problem", [_unmapped, _inactive, _wrong_type])
@pytest.mark.parametrize("made", [_new_draft, _upgraded_draft])
async def test_a_draft_stays_a_draft_when_the_opening_account_cannot_take_it(session, client, auth, made, problem):
    draft = await made(session, client, auth)
    await problem(session, auth)
    before = await _events(session, auth)
    r = await _move(client, auth, "make-available", draft)
    assert r.status_code == 409, r.text
    assert _REPAIR in r.json()["detail"]["message"]
    assert await _events(session, auth) == before
    assert await _lot_state(session, auth, draft) == ("draft", None)
    assert await _entries(session, auth, draft) == []


# --- Back to draft takes the value off; available again books it once --------------------


async def test_returning_stock_to_draft_takes_its_value_off_and_making_it_available_again_books_it_once(
        session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    await _make_available(client, auth, draft)
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 200.0}
    await _to_draft(client, auth, draft)
    assert await _lot_state(session, auth, draft) == ("draft", "1130-OB")
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 0.0}
    await _make_available(client, auth, draft)
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 200.0}
    await _to_draft(client, auth, draft)
    await _make_available(client, auth, draft)
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 200.0}
    assert await _account_net(session, auth["company_id"], "3200") == -200.0


# --- A period lock stops the whole step -------------------------------------------------


async def test_a_locked_day_refuses_making_a_draft_available_and_changes_nothing(session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    await _settings(session, auth, timezone="UTC", lock_date=datetime.now(timezone.utc).date().isoformat())
    before = await _events(session, auth)
    r = await _move(client, auth, "make-available", draft)
    assert r.status_code == 422, r.text
    assert _LOCKED in r.json()["detail"]
    assert await _events(session, auth) == before
    assert await _lot_state(session, auth, draft) == ("draft", None)
    assert await _entries(session, auth, draft) == []


async def test_a_locked_day_refuses_returning_stock_to_draft_and_its_value_stays(session, client, auth):
    draft = await _draft(client, auth, 200.0, 2)
    await _make_available(client, auth, draft)
    await _settings(session, auth, timezone="UTC", lock_date=datetime.now(timezone.utc).date().isoformat())
    before = await _events(session, auth)
    r = await _move(client, auth, "revert-to-draft", draft)
    assert r.status_code == 422, r.text
    assert _LOCKED in r.json()["detail"]
    assert await _events(session, auth) == before
    assert await _lot_state(session, auth, draft) == ("available", "1130-OB")
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 200.0}


async def test_an_opening_entry_in_a_locked_period_does_not_leave_new_stock_unbooked(session, client, auth):
    # Opening stock booked before the lock; a draft made available afterwards is still booked.
    lot = await _lot(client, auth, 30.0)
    row = await session.scalar(select(Projection).where(
        Projection.company_id == auth["company_id"],
        Projection.entity_id.startswith(f"je:auto:{lot}:made-available:")))
    assert row is not None and row.state["status"] == "posted"
    row.state = {**row.state, "ts": "2026-01-15"}
    await session.commit()
    await _settings(session, auth, timezone="UTC", lock_date="2026-01-31")
    draft = await _draft(client, auth, 200.0, 2)
    await _make_available(client, auth, draft)
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 230.0}
    r = await client.get("/accounting/balance-sheet", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books_match_lots(session, auth, "1130-OB") == {"1130-OB": 230.0}


# --- One business day per operation ------------------------------------------------------

_BEFORE = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc)
_AFTER = datetime(2026, 10, 2, 0, 0, 1, tzinfo=timezone.utc)


def _midnight(monkeypatch, passes_after: str) -> None:
    """The clock reads one second before midnight until the operation's first
    ``passes_after`` event lands, and one second after midnight from then on."""
    import celerp.events.engine as event_engine
    import celerp.services.auto_je as auto_je
    import celerp.services.business_time as business_time
    import celerp.services.lot_origin as lot_origin
    import celerp_docs.routes as docs_routes
    import celerp_inventory.routes as inventory_routes
    import celerp_manufacturing.routes as manufacturing_routes
    from celerp.projections.engine import ProjectionEngine

    now = {"instant": _BEFORE}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["instant"].astimezone(tz) if tz is not None else now["instant"].replace(tzinfo=None)

    for module in (event_engine, auto_je, business_time, lot_origin, docs_routes, inventory_routes,
                   manufacturing_routes):
        monkeypatch.setattr(module, "datetime", Clock, raising=False)
    apply = ProjectionEngine.apply_event

    async def applied(session, entry):
        transition = await apply(session, entry)
        if entry.event_type == passes_after:
            now["instant"] = _AFTER
        return transition

    monkeypatch.setattr(ProjectionEngine, "apply_event", staticmethod(applied))


async def _entry_day(session, auth, je_id: str) -> str:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
    assert row is not None and row.state["status"] == "posted", je_id
    return row.state["ts"]


async def test_making_a_draft_available_at_midnight_books_it_on_the_day_it_started(
        session, client, auth, monkeypatch):
    await _settings(session, auth, timezone="UTC")
    draft = await _draft(client, auth, 200.0, 2)
    _midnight(monkeypatch, "item.status.set")
    await _make_available(client, auth, draft)
    [entry] = await _entries(session, auth, draft)
    assert entry["ts"] == "2026-10-01"


async def test_an_audit_adjusted_at_midnight_is_booked_on_the_day_it_started(session, client, auth, monkeypatch):
    h = auth["headers"]
    await _settings(session, auth, timezone="UTC")
    r = await client.post("/companies/me/locations", headers=h, json={"name": "Counted", "type": "warehouse"})
    assert r.status_code == 200, r.text
    loc = r.json()["id"]
    r = await client.post("/items", headers=h, json={
        "sku": f"AUD-{uuid.uuid4().hex[:6]}", "name": "Lot", "quantity": 10, "sell_by": "piece",
        "status": "available", "cost_total": 100.0, "location_id": loc})
    assert r.status_code == 200, r.text
    lot = r.json()["id"]
    audit = (await client.post("/lists/audit", headers=h, json={"location_id": loc})).json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=h)).status_code == 200
    r = await client.patch(f"/lists/{audit}/line/{lot}", headers=h, json={"counted_qty": 8})
    assert r.status_code == 200, r.text
    _midnight(monkeypatch, "item.quantity.adjusted")
    r = await client.post(f"/lists/{audit}/adjust", headers=h)
    assert r.status_code == 200, r.text
    assert await _entry_day(session, auth, f"je:auto:{audit}:audit:0") == "2026-10-01"


async def test_a_write_off_at_midnight_is_booked_on_the_day_it_started(session, client, auth, monkeypatch):
    h = auth["headers"]
    await _settings(session, auth, timezone="UTC")
    lot = await _lot(client, auth, 30.0, qty=3)
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [lot]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h, json={"item_id": lot, "qty_out": 1,
                                                                         "account": "6950"})
    assert r.status_code == 200, r.text
    _midnight(monkeypatch, "item.written_off")
    r = await client.post(f"/lists/{wo}/write-off", headers=h)
    assert r.status_code == 200, r.text
    assert await _entry_day(session, auth, f"je:auto:{wo}:writeoff:0") == "2026-10-01"


async def test_a_run_completed_at_midnight_is_booked_on_the_day_it_started(session, client, auth, monkeypatch):
    h = auth["headers"]
    await _settings(session, auth, timezone="UTC")
    raw = await _lot(client, auth, 40.0, qty=20, sku=f"RAW-{uuid.uuid4().hex[:6]}")
    product = await _lot(client, auth, 0.0, qty=0, sku=f"FG-{uuid.uuid4().hex[:6]}")
    r = await client.put(f"/manufacturing/items/{product}/recipe", headers=h, json={
        "output_qty": 2, "components": [{"item_id": raw, "quantity": 5}], "labor": [], "overhead": []})
    assert r.status_code == 200, r.text
    order = (await client.post(f"/manufacturing/items/{product}/build", headers=h, json={"quantity": 2})).json()["id"]
    _midnight(monkeypatch, "item.consumed")
    r = await client.post(f"/manufacturing/{order}/complete", headers=h, json={"idempotency_key": "c"})
    assert r.status_code == 200, r.text
    # Completing issued the components and received the output, each entry on the day it started.
    for movement in ("issue:c:issue", "receive:c:receive"):
        assert await _entry_day(session, auth, f"je:auto:{order}:{movement}") == "2026-10-01"
