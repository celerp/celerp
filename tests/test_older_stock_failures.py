# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Placing older stock never fails silently.

An unexpected error placing a company's older stock rolls the placement back whole and
leaves one standing notice that names the kind of error, never its text, which can
carry company data. The notice commits with whatever asked for the placement: an
import, a restore, or the request boundary that placed stock for a refused sale in its
own transaction. A restore that is itself rolled back leaves no company and no notice.
The Older stock tab shows a standing failure with a Retry action and a prefilled
bug report carrying the kind of error only.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.notification import Notification
from celerp.services import lot_origin
from migration_support import maker, real_client, real_engine  # noqa: F401
from test_older_stock_placed_on_arrival import _demo, _owner, _restored_company
from test_older_stock_placement import _TAB, _VIEW, _accounts, _older_company, _tab, ui_client  # noqa: F401
from test_older_stock_reconciliation import _records as _placement_records
from test_older_stock_triggers import _posting_accounts_set

pytestmark = pytest.mark.asyncio

_SECRET = "Customer ACME owes 4,000 on lot SKU-9"


async def _failed(session, company_id) -> list[Notification]:
    session.expire_all()
    title = lot_origin._notice(lot_origin.FAILED_NOTICE)[0]
    return list((await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.title == title))).scalars())


def _boom(*a, **k):
    async def fail(*a, **k):
        raise RuntimeError(_SECRET)
    return fail


def _told(notices: list[Notification]) -> None:
    """One standing notice naming the kind of error and never its text."""
    assert len(notices) == 1, notices
    n = notices[0]
    assert n.priority == "high" and n.action_url == lot_origin.OLDER_STOCK_PATH
    assert n.i18n["params"] == {"error": "RuntimeError"}
    assert _SECRET not in f"{n.title} {n.body} {n.i18n}"


def _import(n: int = 1) -> list[dict]:
    out = []
    for _ in range(n):
        eid = f"item:{uuid.uuid4()}"
        out.append({"entity_id": eid, "event_type": "item.snapshot", "source": "csv", "idempotency_key": f"snap:{eid}",
                    "data": {"sku": f"F-{uuid.uuid4().hex[:6]}", "name": "Imported lot", "quantity": 2,
                             "sell_by": "piece", "status": "available", "cost_total": 10.0}})
    return out


async def test_a_failure_upgrading_an_older_company_is_told_and_the_import_goes_through(session, client, monkeypatch):
    auth = await _owner(client)
    await _demo(session, client, auth)
    monkeypatch.setattr(lot_origin, "_normalize", _boom())

    r = await client.post("/items/import/batch", headers=auth["headers"], json={"records": _import()})

    assert r.status_code == 200, r.text
    _told(await _failed(session, auth["company_id"]))


async def test_a_failure_placing_for_a_refused_sale_is_told_and_the_sale_refused(session, client, auth, monkeypatch):
    lots = await _older_company(session, client, auth, [100.0])
    await _posting_accounts_set(session, auth)
    monkeypatch.setattr(lot_origin, "_place", _boom())
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": [
        {"entity_id": lots[0], "name": "Lot", "quantity": 1, "unit_price": 150.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text

    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=auth["headers"], json={})

    assert r.status_code == 409, r.text
    assert "has no inventory account yet" in r.json()["detail"]["message"]
    assert await _accounts(session, auth, lots) == [None]
    _told(await _failed(session, auth["company_id"]))


async def test_trying_again_places_the_stock_and_withdraws_the_failure(session, client, auth, monkeypatch):
    lots = await _older_company(session, client, auth, [100.0, 50.0])
    await _posting_accounts_set(session, auth)
    with monkeypatch.context() as m:
        m.setattr(lot_origin, "_place", _boom())
        r = await client.post("/accounting/older-stock/retry", headers=auth["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["failed"] == {"error": "RuntimeError"}
        assert r.json()["result"] == {"placed": False}
    _told(await _failed(session, auth["company_id"]))

    r = await client.post("/accounting/older-stock/retry", headers=auth["headers"])

    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"placed": True} and r.json()["failed"] is None
    assert None not in await _accounts(session, auth, lots)
    assert await _failed(session, auth["company_id"]) == []
    assert len(await _placement_records(session, auth)) == 1
    r = await client.post("/accounting/older-stock/retry", headers=auth["headers"])
    assert r.json()["result"] == {"placed": True}
    assert len(await _placement_records(session, auth)) == 1


async def test_a_failure_restoring_an_older_backup_is_told(real_client, real_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lot_origin, "_normalize", _boom())
    new, _, _ = await _restored_company(real_client, real_engine, monkeypatch)
    async with maker(real_engine)() as s:
        _told(await _failed(s, new["company_id"]))


async def test_a_restore_rolled_back_after_a_failure_leaves_no_notice(real_client, real_engine, monkeypatch):  # noqa: F811
    """The failure notice is written in the restore's own transaction, so a restore that
    is then rolled back leaves neither the company nor a notice about it."""
    from celerp.services import company_backup

    place = company_backup.place_company_stock

    async def place_then_fail(session, company_id, **kw):
        await place(session, company_id, **kw)
        raise RuntimeError("the restore fails after placing")

    monkeypatch.setattr(lot_origin, "_normalize", _boom())
    monkeypatch.setattr(company_backup, "place_company_stock", place_then_fail)
    title = lot_origin._notice(lot_origin.FAILED_NOTICE)[0]
    async with maker(real_engine)() as s:
        before = len((await s.execute(select(Notification.id).where(Notification.title == title))).all())
    with pytest.raises(RuntimeError, match="the restore fails after placing"):
        await _restored_company(real_client, real_engine, monkeypatch)
    async with maker(real_engine)() as s:
        assert len((await s.execute(select(Notification.id).where(Notification.title == title))).all()) == before


async def test_the_older_stock_tab_offers_try_again_and_a_report_without_the_error_text(ui_client):  # noqa: F811
    from urllib.parse import parse_qs, unquote, urlsplit

    body, _ = await _tab(ui_client, {**_VIEW, "failed": {"error": "RuntimeError"}})

    assert "Older stock could not be placed" in body
    assert 'hx-post="/settings/accounting/older-stock/retry"' in body and ">Retry<" in body
    href = unquote(body.split('href="https://github.com/celerp/celerp/issues/new?', 1)[1].split('"', 1)[0])
    fields = parse_qs(urlsplit("?" + href.replace("&amp;", "&")).query)
    assert fields["template"] == ["bug_report.yml"] and fields["area"] == ["Accounting"]
    assert "RuntimeError" in fields["current"][0]

    body, _ = await _tab(ui_client, {**_VIEW, "failed": None})
    assert "older-stock/retry" not in body


async def test_trying_again_from_the_tab_re_renders_the_card(ui_client):  # noqa: F811
    from unittest.mock import AsyncMock, patch

    from test_older_stock_placement import _cookies

    retry = AsyncMock(return_value={**_VIEW, "failed": None, "result": {"placed": True}})
    ui_client.cookies.update(_cookies())
    with patch("ui.api_client.retry_older_stock", new=retry):
        r = await ui_client.post("/settings/accounting/older-stock/retry")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'id="older-stock-card"' in body and "Older stock was placed." in body
