# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Moving stock between draft and available while other requests touch it.

Each case holds one request open just before it commits, with every lock it took
still held, sends the second request on its own connection, waits until Postgres
reports it blocked, then lets the first commit. The only acceptable outcome is the
serial one: a draft's value is booked once when it becomes stock and taken off once
when it goes back, the loser of a race is a no-op or a clean refusal, and a draft
never ends up reserved or sold. After every case the books carry exactly the stock
recorded on them.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from migration_support import auth, maker
from stock_books import assert_books_carry_stock
from test_posting_roles_race_pg import _until_blocked

pytestmark = pytest.mark.asyncio


class _HeldCommit:
    """Holds the commit of the next request's session until released."""

    def __init__(self):
        self.armed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def wrap(self, session) -> None:
        if not self.armed:
            return
        self.armed = False
        commit = session.commit

        async def held():
            self.reached.set()
            await self.release.wait()
            await commit()

        session.commit = held


@pytest_asyncio.fixture
async def race(committed_engine, monkeypatch):
    """An API client whose requests commit for real in the test's own database, and the
    hold on the next request's commit."""
    import celerp.db
    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app

    hold = _HeldCommit()

    async def _session():
        async with maker(committed_engine)() as s:
            hold.wrap(s)
            yield s

    @asynccontextmanager
    async def _session_ctx():
        async with maker(committed_engine)() as s:
            yield s

    monkeypatch.setattr(celerp.db, "engine", committed_engine)
    app.dependency_overrides[get_session] = _session
    try:
        with patch("celerp.gateway.client._client", MagicMock()), \
             patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
             patch("celerp.middleware.get_session_ctx", _session_ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c, hold
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _company(engine) -> tuple[uuid.UUID, str]:
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User
    from test_helpers import make_authed_token, provision_company_books

    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with maker(engine)() as s:
        s.add(Company(id=cid, name="Draft Race", slug=f"draft-race-{cid.hex[:8]}", settings={"currency": "USD"}))
        s.add(User(id=uid, email=f"owner-{cid.hex[:8]}@example.com", name="Owner", auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="owner", is_active=True))
        await provision_company_books(s, cid)
        await s.commit()
        return cid, await make_authed_token(s, str(uid), str(cid), "owner")


async def _ok(client, tok: str, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, headers=auth(tok), json=body or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _draft(client, tok: str, cost: float, sku: str | None = None, qty: float = 1) -> str:
    return (await _ok(client, tok, "/items", {"sku": sku or f"DR-{uuid.uuid4().hex[:6]}", "name": "Lot",
                                               "quantity": qty, "sell_by": "piece", "cost_total": cost,
                                               "allow_splitting": True}))["id"]


async def _available(client, tok: str, cost: float, **kw) -> str:
    lot = await _draft(client, tok, cost, **kw)
    await _ok(client, tok, "/items/bulk/make-available", {"entity_ids": [lot]})
    return lot


async def _race(engine, client, hold, first, second):
    """Send ``first`` and hold it at its commit; send ``second`` until it blocks; let
    ``first`` commit. Returns both responses."""
    hold.armed = True
    one = asyncio.create_task(first())
    await asyncio.wait_for(hold.reached.wait(), timeout=30)
    two = asyncio.create_task(second())
    await _until_blocked(engine, two)
    hold.release.set()
    return await asyncio.wait_for(one, timeout=30), await asyncio.wait_for(two, timeout=30)


async def _state(engine, cid, lot: str) -> dict:
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        return (await s.get(Projection, {"company_id": cid, "entity_id": lot})).state


async def _draft_entries(engine, cid, lot: str) -> list[str]:
    """The kinds of posted entries that moved this lot between draft and stock."""
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith(f"je:auto:{lot}:", autoescape=True)))).scalars()
        return sorted(r.entity_id.split(":")[-2] for r in rows if r.state.get("status") == "posted")


async def _books(engine, cid) -> dict:
    async with maker(engine)() as s:
        return await assert_books_carry_stock(s, cid)


def _move(client, tok, path: str, lot: str):
    return lambda: client.post(f"/items/bulk/{path}", headers=auth(tok), json={"entity_ids": [lot]})


async def test_concurrent_make_available_books_once(committed_engine, race):
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _draft(client, tok, 100.0)

    one, two = await _race(committed_engine, client, hold,
                           _move(client, tok, "make-available", lot), _move(client, tok, "make-available", lot))

    assert one.status_code == 200, one.text
    # The second request found the lot already available: nothing left to do.
    assert two.status_code == 200, two.text
    assert two.json()["updated"] == 0
    assert (await _state(committed_engine, cid, lot))["status"] == "available"
    assert await _draft_entries(committed_engine, cid, lot) == ["made-available"]
    books = await _books(committed_engine, cid)
    assert sum(books.values()) == 100


async def test_concurrent_revert_reverses_once(committed_engine, race):
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100.0)

    one, two = await _race(committed_engine, client, hold,
                           _move(client, tok, "revert-to-draft", lot), _move(client, tok, "revert-to-draft", lot))

    assert one.status_code == 200, one.text
    assert two.status_code == 200, two.text
    assert two.json()["updated"] == 0
    assert (await _state(committed_engine, cid, lot))["status"] == "draft"
    assert await _draft_entries(committed_engine, cid, lot) == ["made-available", "returned-to-draft"]
    books = await _books(committed_engine, cid)
    assert sum(books.values()) == 0


async def _invoice_spanning(client, tok: str) -> tuple[str, str, str]:
    """An invoice for two units of a SKU whose line names lot ``a`` (one unit); its
    fulfilment draws the second unit from lot ``b``, which no document names.

    Finalizing the invoice takes the cost of both units off the books, ahead of the
    fulfilment that marks the lots sold, so the books agree with the lots again only
    once it is fulfilled."""
    sku = f"SPAN-{uuid.uuid4().hex[:6]}"
    a = await _available(client, tok, 60.0, sku=sku)
    b = await _available(client, tok, 40.0, sku=sku)
    doc = (await _ok(client, tok, "/docs", {"doc_type": "invoice", "total": 200.0, "line_items": [
        {"entity_id": a, "sku": sku, "name": sku, "quantity": 2, "unit_price": 100.0, "sell_by": "piece"}]}))["id"]
    await _ok(client, tok, f"/docs/{doc}/finalize")
    return doc, a, b


async def test_revert_racing_fulfil_never_leaves_circulated_draft(committed_engine, race):
    """Fulfilment draws lot ``b`` and holds its commit; Revert to Draft of ``b`` arrives
    meanwhile. Revert must see the fulfilment once it commits and refuse."""
    client, hold = race
    cid, tok = await _company(committed_engine)
    doc, a, b = await _invoice_spanning(client, tok)

    fulfil, revert = await _race(
        committed_engine, client, hold,
        lambda: client.post(f"/docs/{doc}/fulfill-lines", headers=auth(tok), json={"line_entity_ids": [a]}),
        _move(client, tok, "revert-to-draft", b))

    assert fulfil.status_code == 200, fulfil.text
    assert revert.status_code == 409, revert.text
    assert (await _state(committed_engine, cid, b))["status"] == "sold"
    assert await _draft_entries(committed_engine, cid, b) == ["made-available"]
    await _books(committed_engine, cid)


async def test_fulfil_racing_revert_loser_clean_conflict(committed_engine, race):
    """Revert to Draft of lot ``b`` holds its commit; a fulfilment that would draw ``b``
    arrives meanwhile. Once ``b`` is a draft it is not stock, so the fulfilment cannot
    draw it and refuses cleanly, leaving every lot as it was."""
    client, hold = race
    cid, tok = await _company(committed_engine)
    doc, a, b = await _invoice_spanning(client, tok)

    revert, fulfil = await _race(
        committed_engine, client, hold,
        _move(client, tok, "revert-to-draft", b),
        lambda: client.post(f"/docs/{doc}/fulfill-lines", headers=auth(tok), json={"line_entity_ids": [a]}))

    assert revert.status_code == 200, revert.text
    assert fulfil.status_code in (409, 422), fulfil.text
    assert (await _state(committed_engine, cid, a))["status"] == "available"
    assert (await _state(committed_engine, cid, b))["status"] == "draft"
    assert await _draft_entries(committed_engine, cid, b) == ["made-available", "returned-to-draft"]
    # The invoice already recognized the cost of both units when it was finalized, so
    # the books agree with the lots once it is fulfilled: bring b back and fulfil.
    await _ok(client, tok, "/items/bulk/make-available", {"entity_ids": [b]})
    await _ok(client, tok, f"/docs/{doc}/fulfill-lines", {"line_entity_ids": [a]})
    assert (await _state(committed_engine, cid, b))["status"] == "sold"
    await _books(committed_engine, cid)


async def test_reserve_racing_revert_rejected_from_locked_state(committed_engine, race):
    """Revert to Draft holds its commit; a reservation waits for the lot, then its own
    check reads the draft it finds and refuses."""
    client, hold = race
    cid, tok = await _company(committed_engine)
    lot = await _available(client, tok, 100.0)

    revert, reserve = await _race(
        committed_engine, client, hold,
        _move(client, tok, "revert-to-draft", lot),
        lambda: client.post(f"/items/{lot}/reserve", headers=auth(tok), json={"quantity": 1}))

    assert revert.status_code == 200, revert.text
    assert reserve.status_code == 422, reserve.text
    assert "make it available first" in reserve.json()["detail"]
    assert (await _state(committed_engine, cid, lot))["status"] == "draft"
    await _books(committed_engine, cid)
