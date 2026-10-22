# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Older stock is placed wherever it would otherwise hold the user up.

Besides every start and every writer bringing such stock in, placing runs when the
company turns Accounting on, and when a request is refused only because a lot it needs
is not placed yet: a sale, a shipment or a move of older stock. The refused request is
rolled back whole, the stock is placed the one canonical way
(lot_origin.place_company_stock) in its own transaction, and the request runs again,
once. Two such requests at the same time place the stock once and both go through.
Only a writer proven to touch nothing outside the database before such a refusal runs
again (lot_origin.rerun_after_placing), and never one that committed any write before
it was refused, by whatever means, so a retry cannot do anything twice.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from ui.i18n import t

from celerp.models.ledger import LedgerEntry

from celerp.models.projections import Projection
from migration_support import auth as bearer, maker
from stock_books import assert_settled
from test_older_stock_placement import _accounts, _move, _older_company
from test_older_stock_reconciliation import _records
from test_posting_roles_lots import _sell
from test_posting_roles_race_pg_draft import _company, race  # noqa: F401  (race is a fixture)


async def _posting_accounts_set(session, auth) -> None:
    """The user set the posting accounts an older release lacked; nothing placed the stock since."""
    from celerp.services.account_roles import reconcile_company

    await reconcile_company(session, auth["company_id"], frozenset())
    await session.commit()


async def _ship(client, auth, inv: str, *lots: str):
    return await client.post(f"/docs/{inv}/fulfill-lines", headers=auth["headers"],
                             json={"line_entity_ids": list(lots)})


async def test_a_sale_of_unplaced_older_stock_places_it_and_goes_through(session, client, auth):
    lots = await _older_company(session, client, auth, [100.0, 50.0])
    await _posting_accounts_set(session, auth)

    inv = await _sell(client, auth, (lots[0], 1))
    assert None not in await _accounts(session, auth, lots)
    assert len(await _records(session, auth)) == 1
    r = await _ship(client, auth, inv, lots[0])
    assert r.status_code == 200, r.text
    await assert_settled(client, session, auth)


async def _ledger(session, auth, prefix: str) -> list[tuple[str, str]]:
    session.expire_all()
    return sorted((e.entity_id, e.event_type) for e in (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id.startswith(prefix)))).scalars())


async def test_a_sale_run_again_after_placing_writes_its_document_and_entry_once(session, client, auth):
    import celerp.middleware as middleware

    lots = await _older_company(session, client, auth, [100.0, 50.0])
    await _posting_accounts_set(session, auth)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
        {"entity_id": lots[0], "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    docs, entries = await _ledger(session, auth, inv), await _ledger(session, auth, f"je:auto:{inv}:")
    placed = AsyncMock(side_effect=middleware._placed)

    with patch.object(middleware, "_placed", placed):
        r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"], json={})

    assert r.status_code == 200, r.text
    placed.assert_awaited_once()  # refused once for the unplaced lot, then run again
    finalized = [e for e in await _ledger(session, auth, inv) if e not in docs]
    assert [t for _, t in finalized] == ["doc.finalized"]
    assert await session.scalar(select(func.count()).select_from(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "doc")) == 1
    (je,) = {e for e, _ in await _ledger(session, auth, f"je:auto:{inv}:") if (e, _) not in entries}
    assert je == f"je:auto:{inv}:fin"
    await assert_settled(client, session, auth)


async def _write_then_refuse(engine, wrote: str | None, *, marked: bool = True):
    """POST /write once through OlderStockMiddleware, on a database whose commits are
    real: the endpoint makes the write named by ``wrote`` and commits it, then is refused
    for an unplaced lot. Returns the response, how many times the endpoint ran, and the
    placing mock."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text, update

    import celerp.middleware as middleware
    from celerp.models.company import Company
    from celerp.services.account_roles import LotOriginError
    from celerp.services.lot_origin import rerun_after_placing

    cid, _tok = await _company(engine)
    calls: list[int] = []
    app = FastAPI()
    company = Company.id == cid

    async def write(request: Request):
        request.state.company_id = cid
        calls.append(len(calls))
        async with maker(engine)() as s:
            if wrote == "orm":
                row = await s.get(Company, cid)
                row.name = f"{row.name} {len(calls)}"
            elif wrote == "text":
                await s.execute(text("UPDATE companies SET name = name WHERE id = :id"), {"id": cid})
            elif wrote == "core":
                await s.execute(update(Company).where(company).values(name=Company.name))
            elif wrote == "reads":
                await s.execute(text("SELECT name FROM companies WHERE id = :id"), {"id": cid})
                async with s.begin_nested():
                    await s.execute(select(Company.name).where(company).with_for_update())
            await s.commit()
        if wrote == "connection":
            async with engine.begin() as conn:
                await conn.execute(update(Company).where(company).values(name=Company.name))
        raise LotOriginError("OLD-1")

    app.post("/write")(rerun_after_placing(write) if marked else write)
    placed = AsyncMock(return_value=True)
    with patch.object(middleware, "_placed", placed):
        async with AsyncClient(transport=ASGITransport(app=middleware.OlderStockMiddleware(app)),
                               base_url="http://test") as c:
            r = await c.post("/write", json={})
    return r, calls, placed


def _refused_as_it_was(r) -> None:
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "posting.older_stock.no_account"


@pytest.mark.parametrize("wrote", ["orm", "text", "core", "connection"])
async def test_a_request_that_committed_before_its_refusal_is_never_run_again(committed_engine, wrote):
    """Whatever wrote it, the session's objects, a text or Core statement, or a
    connection of its own, a committed write keeps the request from running again."""
    r, calls, placed = await _write_then_refuse(committed_engine, wrote)

    placed.assert_awaited_once()
    assert len(calls) == 1
    _refused_as_it_was(r)


@pytest.mark.parametrize("wrote", [None, "reads"])
async def test_a_request_that_kept_nothing_runs_again_after_placing(committed_engine, wrote):
    """Reads, row locks and a savepoint released inside the transaction keep nothing, so
    the request runs again once its stock is placed."""
    r, calls, placed = await _write_then_refuse(committed_engine, wrote)

    placed.assert_awaited_once()
    assert len(calls) == 2


async def test_a_writer_not_proven_safe_to_run_again_places_the_stock_and_returns_its_refusal(committed_engine):
    """Only a writer marked rerun_after_placing runs again: any other may have sent a
    notice, written a file or called out before it was refused, which no rollback undoes."""
    r, calls, placed = await _write_then_refuse(committed_engine, None, marked=False)

    placed.assert_awaited_once()
    assert len(calls) == 1
    _refused_as_it_was(r)


def test_the_writers_run_again_are_exactly_those_proven_safe():
    """Each writer marked here has its run again proven to touch nothing outside the
    database (test_a_writer_run_again_touches_nothing_outside_the_database). The walk
    covers the app's routes and every router a module defines, mounted or not."""
    from celerp.ai.tools import _api_routes
    from celerp.main import app
    from celerp.services.lot_origin import RERUN_AFTER_PLACING
    from test_module_write_routes_guarded import _module_routers

    routes = [r for _, _, r in _api_routes(app)] + [r for router in _module_routers() for r in router.routes]
    marked = {f"{e.__module__}:{e.__name__}" for r in routes
              if getattr(e := getattr(r, "endpoint", None), RERUN_AFTER_PLACING, False)}
    assert marked == {"celerp_docs.routes:finalize_doc", "celerp_accounting.routes:move_older_stock"}


@pytest.fixture
def outside():
    """Every way a request reaches outside the database, recorded instead of done: a
    request out (httpx), mail (smtplib), and a file written, replaced or renamed."""
    import builtins
    import os
    import smtplib
    from pathlib import Path

    import httpx

    seen: list[str] = []
    real_open = builtins.open

    def record(name):
        def sink(*args, **kwargs):
            seen.append(name)
            raise AssertionError(f"{name} during a request run again")
        return sink

    def opened(file, mode="r", *args, **kwargs):
        if any(c in mode for c in "wax+"):
            seen.append(f"open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", record("httpx async request")), \
            patch.object(httpx.HTTPTransport, "handle_request", record("httpx request")), \
            patch.object(smtplib.SMTP, "__init__", record("smtplib.SMTP")), \
            patch.object(smtplib.SMTP_SSL, "__init__", record("smtplib.SMTP_SSL")), \
            patch.object(builtins, "open", opened), \
            patch.object(Path, "write_bytes", record("Path.write_bytes")), \
            patch.object(Path, "write_text", record("Path.write_text")), \
            patch.object(os, "replace", record("os.replace")), \
            patch.object(os, "rename", record("os.rename")):
        yield seen


@pytest.mark.parametrize("writer", ["finalize", "move"])
async def test_a_writer_run_again_touches_nothing_outside_the_database(session, client, auth, outside, writer):
    """Refused for unplaced stock, run again once it is placed: neither run sends a request
    or mail or writes a file, and the only notice is the one placing leaves, so the run
    again repeats nothing."""
    import celerp.middleware as middleware
    from celerp.models.notification import Notification

    lots = await _older_company(session, client, auth, [100.0, 50.0])
    await _posting_accounts_set(session, auth)
    if writer == "finalize":
        r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
            {"entity_id": lots[0], "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]})
        assert r.status_code == 200, r.text
        inv = r.json()["id"]
    notices = select(Notification.title).where(Notification.company_id == auth["company_id"])
    before = set((await session.execute(notices)).scalars())
    placed = AsyncMock(side_effect=middleware._placed)
    outside.clear()

    with patch.object(middleware, "_placed", placed):
        if writer == "finalize":
            r = await client.post(f"/docs/{inv}/finalize", headers=auth["headers"], json={})
        else:
            r = await _move(client, auth, [lots[0]], "1130-P")

    assert r.status_code == 200, r.text
    placed.assert_awaited_once()  # refused once for the unplaced lot, then run again
    assert outside == []
    session.expire_all()
    new = [title for title in (await session.execute(notices)).scalars() if title not in before]
    assert new == [t("notice.older_stock_placed.title", "en")]


async def test_moving_unplaced_older_stock_places_it_first(session, client, auth):
    lots = await _older_company(session, client, auth, [100.0, 50.0])
    await _posting_accounts_set(session, auth)

    r = await _move(client, auth, [lots[0]], "1130-P")
    assert r.status_code == 200, r.text
    assert (await _accounts(session, auth, lots))[0] == "1130-P"
    assert len(await _records(session, auth)) == 1
    await assert_settled(client, session, auth)


async def test_a_refusal_placing_cannot_lift_is_returned_as_it_was(session, client, auth):
    """The stock falls short of what the accounts hold and retained earnings cannot take
    the opening stock: placing waits on that setting, so the sale is refused as before
    and nothing is placed."""
    from celerp_accounting.models import Account
    from sqlalchemy import update

    lots = await _older_company(session, client, auth, [100.0], short=40.0)
    await _posting_accounts_set(session, auth)
    await session.execute(update(Account).where(Account.company_id == auth["company_id"], Account.code == "3200")
                          .values(is_active=False))
    await session.commit()
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
        {"entity_id": lots[0], "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert "has no inventory account yet" in r.json()["detail"]["message"]
    assert r.headers["X-Celerp-Fix"] == "/settings/accounting?tab=posting-accounts"
    assert await _accounts(session, auth, lots) == [None]
    assert await _records(session, auth) == []


async def test_turning_accounting_on_places_older_stock_left_unplaced(session, client, auth):
    lots = await _older_company(session, client, auth, [30.0, 20.0])
    await _posting_accounts_set(session, auth)
    r = await client.post("/companies/me/modules/celerp-accounting/disable", headers=auth["headers"])
    assert r.status_code == 200, r.text

    r = await client.post("/companies/me/modules/celerp-accounting/enable", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert None not in await _accounts(session, auth, lots)
    assert len(await _records(session, auth)) == 1
    await assert_settled(client, session, auth)


async def test_two_sales_of_unplaced_older_stock_at_once_place_it_once_and_both_go_through(
        committed_engine, race):  # noqa: F811
    from celerp.accounting_roles import INVENTORY_ORIGIN_KEY, INVENTORY_ORIGIN_SCHEMA, LOT_ACCOUNT_FIELD
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry
    from test_posting_roles_race_pg_draft import _available, _ok

    client, _hold = race
    cid, tok = await _company(committed_engine)
    lots = [await _available(client, tok, 100.0), await _available(client, tok, 60.0)]
    async with maker(committed_engine)() as s:  # as an older release left them: no account recorded
        for lot in lots:
            row = await s.get(Projection, {"company_id": cid, "entity_id": lot})
            row.state = {k: v for k, v in row.state.items() if k != LOT_ACCOUNT_FIELD}
        await s.execute(LedgerEntry.__table__.delete().where(
            LedgerEntry.company_id == cid, LedgerEntry.event_type == "item.inventory_account.recorded"))
        company = await s.get(Company, cid)
        company.settings = {**company.settings, INVENTORY_ORIGIN_KEY: INVENTORY_ORIGIN_SCHEMA}
        await s.commit()
    docs = [(await _ok(client, tok, "/docs", {"doc_type": "invoice", "line_items": [
        {"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]}))["id"]
        for lot in lots]

    one, two = await asyncio.wait_for(asyncio.gather(
        *(client.post(f"/docs/{doc}/finalize", headers=bearer(tok), json={}) for doc in docs)), timeout=60)

    assert one.status_code == 200, one.text
    assert two.status_code == 200, two.text
    async with maker(committed_engine)() as s:
        assert await s.scalar(select(func.count()).select_from(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "older_stock_placement")) == 1
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == cid, Projection.entity_id.in_(lots)))).scalars().all()
        assert all(r.state.get(LOT_ACCOUNT_FIELD) for r in rows)
