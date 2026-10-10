# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Automatic entries carry the business day of the operation they record.

A bill without its own date, goods returned on a credit note, a return undone, a
production run movement, an audit or write-off adjustment, goods received on a purchase
order, landed cost capitalised on receipt, and goods returned to a supplier are each dated the
company's calendar day of the operation, never the server's own date. A recorded
date or timestamp wins over the clock, a period lock through that day refuses the
entry with the usual message, and posting the same entry again later or rebuilding
the books never re-dates it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services import auto_je
from celerp.services.company_lock import locked_company
from test_money_boundary import _bill, _company, _item
from test_posting_roles_older_stock import _BANGKOK, _NEW_YORK, _clock
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def _books_in(session, auth, tz: str, **settings) -> None:
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "timezone": tz, **settings}
    await session.commit()


async def _bill_without_a_date(session, client, auth, instant) -> str:
    await auto_je.create_for_bill_conversion(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b1",
        doc=_bill(100.0, [(100.0, "expense")]),
    )
    return "je:auto:doc:b1:bill"


async def _return_received(session, client, auth, instant) -> str:
    await auto_je.create_for_return_received(
        session, company_id=auth["company_id"], user_id=auth["user_id"], cn_id="doc:cn1",
        lot_costs={auth["lot"]: 10.0}, je_suffix="r1", received_at=instant.isoformat(),
    )
    return "je:auto:doc:cn1:return:r1"


async def _return_undone(session, client, auth, instant) -> str:
    await auto_je.create_for_return_undone(
        session, company_id=auth["company_id"], user_id=auth["user_id"], cn_id="doc:cn1",
        lot_costs={auth["lot"]: 10.0}, unique_suffix="u1", undone_at=instant.isoformat(),
    )
    return "je:auto:doc:cn1:return:undo:u1"


async def _run_issued(session, client, auth, instant) -> str:
    await auto_je.create_for_mfg_movement(
        session, company_id=auth["company_id"], user_id=auth["user_id"], order_id="mo:1", movement="issue:k1",
        memo="Components issued", wip_code="1130-WIP", wip=Decimal("10"), lots={"1130-P": Decimal("-10")},
        day=await auto_je.entry_day(session, auth["company_id"], instant.isoformat()),
    )
    return "je:auto:mo:1:issue:k1"


async def _stock_adjusted(session, client, auth, instant) -> str:
    await auto_je.create_for_audit_adjustment(
        session, company_id=auth["company_id"], user_id=auth["user_id"], list_id="list:a1",
        shrinkage={"1130-P": 10.0}, overage={},
    )
    return "je:auto:list:a1:audit:0"


async def _landed_capitalised(session, client, auth, instant) -> str:
    await auto_je.create_for_landed_capitalisation(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b2",
        landed_by_kind={"freight": 5.0}, landed_by_account={"1130-P": 5.0}, receive_suffix="r1",
    )
    return "je:auto:doc:b2:landed-cap:r1"


async def _auto_entry_of(session, auth, prefix: str) -> str:
    rows = (await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_id.startswith(prefix)))).scalars().all()
    assert len(rows) == 1, rows
    return rows[0]


async def _order_received(session, client, auth, instant) -> str:
    from test_receipt_accounting import _doc, _finalize
    from test_receive_selected_lines import _post

    order = await _doc(client, auth, "purchase_order", [{"sku": "BD-PO", "name": "Goods", "quantity": 2, "unit_price": 5.0}])
    r = await _post(client, auth, order, {"po_line_index": 0, "sku": "BD-PO", "quantity_received": 2})
    assert r.status_code == 200, r.text
    return await _auto_entry_of(session, auth, f"je:auto:{order}:rcv:")


async def _returned_to_supplier(session, client, auth, instant) -> str:
    from test_receipt_accounting import _doc, _finalize
    from test_receive_selected_lines import _post

    bill = await _doc(client, auth, "bill", [{"sku": "BD-RTN", "name": "Goods", "quantity": 2, "unit_price": 5.0}])
    await _finalize(client, auth, bill)
    r = await _post(client, auth, bill, {"po_line_index": 0, "sku": "BD-RTN", "quantity_received": 2})
    assert r.status_code == 200, r.text
    parcel = (await session.get(Projection, {"company_id": auth["company_id"], "entity_id": bill})).state["received_item_ids"][0]
    r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": parcel, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    return await _auto_entry_of(session, auth, f"je:auto:{bill}:rtn:")


_PATHS = [_bill_without_a_date, _return_received, _return_undone, _run_issued, _stock_adjusted, _landed_capitalised]
_IDS = ["bill", "return-received", "return-undone", "manufacturing", "line-adjustment", "landed-capitalisation"]
# Posted by a request, so dated by the clock alone.
_REQUESTS = [_order_received, _returned_to_supplier]


async def _setup(session, client) -> dict:
    auth = await _company(session, "USD")
    auth["lot"] = await _item(client, auth, 10.0, 1)
    return auth


async def _entry(session, auth, je_id: str) -> dict | None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id})
    return row.state if row is not None else None


async def _created(session, auth, je_id: str) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == je_id,
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalar_one()


@pytest.mark.parametrize("case", [_NEW_YORK, _BANGKOK], ids=["business-day-behind", "business-day-ahead"])
@pytest.mark.parametrize("post", [*_PATHS, *_REQUESTS], ids=[*_IDS, "order-received", "returned-to-supplier"])
async def test_the_entry_is_dated_the_business_day_of_the_operation(session, client, monkeypatch, post, case):
    tz, instant, host_day = case
    auth = await _setup(session, client)
    await _books_in(session, auth, tz)
    _clock(monkeypatch, instant, host_day)
    je_id = await post(session, client, auth, instant)
    await session.commit()
    entry = await _entry(session, auth, je_id)
    assert entry is not None and entry["status"] == "posted"
    business_day = instant.astimezone(ZoneInfo(tz)).date().isoformat()
    assert business_day != host_day.isoformat()
    assert entry["ts"] == business_day


@pytest.mark.parametrize("post", _PATHS, ids=_IDS)
async def test_a_lock_through_the_business_day_refuses_the_entry(session, client, monkeypatch, post):
    tz, instant, host_day = _NEW_YORK  # Oct 1 there, Oct 2 on the server
    auth = await _setup(session, client)
    await _books_in(session, auth, tz, lock_date="2026-10-01")
    _clock(monkeypatch, instant, host_day)
    with pytest.raises(HTTPException) as refused:
        await post(session, client, auth, instant)
    assert refused.value.status_code == 422
    assert refused.value.detail == t("error.period_locked", "en", date="2026-10-01")


@pytest.mark.parametrize("post", _PATHS, ids=_IDS)
async def test_posting_again_on_a_later_day_changes_nothing(session, client, monkeypatch, post):
    tz, first, host_day = _BANGKOK  # Oct 2 there
    auth = await _setup(session, client)
    await _books_in(session, auth, tz)
    _clock(monkeypatch, first, host_day)
    je_id = await post(session, client, auth, first)
    await session.commit()
    before = await _entry(session, auth, je_id)
    later = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)
    _clock(monkeypatch, later, later.date())
    await post(session, client, auth, first)
    await session.commit()
    assert await _created(session, auth, je_id) == 1
    assert await _entry(session, auth, je_id) == before
    assert before["ts"] == "2026-10-02"


@pytest.mark.parametrize("post", _PATHS, ids=_IDS)
async def test_rebuilding_the_books_later_keeps_the_entry_date(session, client, monkeypatch, post):
    tz, first, host_day = _BANGKOK
    auth = await _setup(session, client)
    await _books_in(session, auth, tz)
    _clock(monkeypatch, first, host_day)
    je_id = await post(session, client, auth, first)
    await session.commit()
    later = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)
    _clock(monkeypatch, later, later.date())
    await ProjectionEngine.rebuild(session, auth["company_id"])
    await session.commit()
    assert (await _entry(session, auth, je_id))["ts"] == "2026-10-02"


async def test_a_bill_dated_only_by_when_it_was_finalized_posts_on_that_business_day(session, client, monkeypatch):
    auth = await _setup(session, client)
    await _books_in(session, auth, "Asia/Bangkok")
    _clock(monkeypatch, datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 9).date())
    await auto_je.create_for_bill_conversion(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b9",
        doc=_bill(100.0, [(100.0, "expense")], finalized_at="2026-09-15T20:00:00+00:00"),
    )
    await session.commit()
    assert (await _entry(session, auth, "je:auto:doc:b9:bill"))["ts"] == "2026-09-16"


async def test_a_bill_with_its_own_date_keeps_it(session, client, monkeypatch):
    auth = await _setup(session, client)
    await _books_in(session, auth, "Asia/Bangkok")
    _clock(monkeypatch, datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 9).date())
    await auto_je.create_for_bill_conversion(
        session, company_id=auth["company_id"], user_id=auth["user_id"], doc_id="doc:b8",
        doc=_bill(100.0, [(100.0, "expense")], issue_date="2026-09-03",
                  finalized_at="2026-09-15T20:00:00+00:00"),
    )
    await session.commit()
    assert (await _entry(session, auth, "je:auto:doc:b8:bill"))["ts"] == "2026-09-03"


@pytest.mark.parametrize("post", [_return_received, _return_undone], ids=["return-received", "return-undone"])
async def test_a_return_handled_later_posts_on_the_day_it_was_recorded(session, client, monkeypatch, post):
    auth = await _setup(session, client)
    await _books_in(session, auth, "Asia/Bangkok")
    _clock(monkeypatch, datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 9).date())
    je_id = await post(session, client, auth, datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc))
    await session.commit()
    assert (await _entry(session, auth, je_id))["ts"] == "2026-09-16"
