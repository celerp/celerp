# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A document or List stored without an issue_date shows, windows, sorts and exports by the
company's business day of its creation, never the server's UTC day. The demo company is in
Pacific/Pago_Pago (UTC-11) and the records were created at 05:00 UTC on 10 March, which is
still 9 March for the company."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from celerp.models.projections import Projection
from test_tax_acceptance_matrix import _demo_company

CREATED = datetime(2026, 3, 10, 5, 0, tzinfo=timezone.utc)
BUSINESS_DAY = "2026-03-09"


async def _dateless_records(client, session) -> dict:
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    r = await client.patch("/companies/me", headers=h, json={"settings": {"timezone": "Pacific/Pago_Pago"}})
    assert r.status_code == 200, r.text
    r = await client.post("/docs/import", headers=h, json={
        "entity_id": "doc:IMP-TZ", "event_type": "doc.created", "source": "csv", "idempotency_key": "doc:IMP-TZ",
        "data": {"doc_type": "invoice", "status": "draft", "currency": "USD",
                 "line_items": [{"description": "S", "quantity": 1, "unit_price": 5}]}})
    assert r.status_code == 200, r.text
    r = await client.post("/lists/import", headers=h, json={
        "entity_id": "list:IMP-TZ", "event_type": "list.created", "source": "csv", "idempotency_key": "list:IMP-TZ",
        "data": {"ref_id": "IMP-TZ-L", "list_type": "quotation", "status": "draft",
                 "line_items": [{"name": "S", "quantity": 1, "unit_price": 5, "line_total": 5}],
                 "subtotal": 5, "total": 5}})
    assert r.status_code == 200, r.text
    await session.execute(update(Projection).where(Projection.entity_id.in_(("doc:IMP-TZ", "list:IMP-TZ")))
                          .values(created_at=CREATED))
    await session.commit()
    return h


def _items(body):
    return body["items"] if isinstance(body, dict) else body


@pytest.mark.asyncio
async def test_dateless_document_uses_the_company_business_day(client, session):
    h = await _dateless_records(client, session)

    rows = _items((await client.get("/docs?doc_type=invoice&limit=100", headers=h)).json())
    assert next(d for d in rows if d["id"] == "doc:IMP-TZ")["issue_date"] == BUSINESS_DAY

    day = _items((await client.get(f"/docs?doc_type=invoice&date_from={BUSINESS_DAY}&date_to={BUSINESS_DAY}",
                                   headers=h)).json())
    assert [d["id"] for d in day] == ["doc:IMP-TZ"]
    utc_day = _items((await client.get("/docs?doc_type=invoice&date_from=2026-03-10&date_to=2026-03-10",
                                       headers=h)).json())
    assert utc_day == []

    r = await client.get("/docs/export/csv?doc_type=invoice&cols=entity_id,issue_date", headers=h)
    assert r.status_code == 200, r.text
    exported = {row["entity_id"]: row["issue_date"] for row in csv.DictReader(io.StringIO(r.text))}
    assert exported["doc:IMP-TZ"] == BUSINESS_DAY


@pytest.mark.asyncio
async def test_dateless_list_uses_the_company_business_day(client, session):
    h = await _dateless_records(client, session)

    assert (await client.get("/lists/list:IMP-TZ", headers=h)).json()["issue_date"] == BUSINESS_DAY
    page = await client.get("/lists/list:IMP-TZ/page?offset=0&limit=10", headers=h)
    assert page.status_code == 200, page.text
    assert page.json()["list"]["issue_date"] == BUSINESS_DAY

    rows = _items((await client.get(f"/lists?date_from={BUSINESS_DAY}&date_to={BUSINESS_DAY}", headers=h)).json())
    assert [(x["id"], x["date"]) for x in rows] == [("list:IMP-TZ", BUSINESS_DAY)]
    assert _items((await client.get("/lists?date_from=2026-03-10&date_to=2026-03-10", headers=h)).json()) == []

    r = await client.get("/lists/export/csv?cols=id,date", headers=h)
    assert r.status_code == 200, r.text
    exported = {row["id"]: row["date"] for row in csv.DictReader(io.StringIO(r.text))}
    assert exported["list:IMP-TZ"] == BUSINESS_DAY
