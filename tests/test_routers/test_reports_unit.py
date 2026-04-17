# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from celerp_reports import routes as reports


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _):
        return _Res(self.rows)


def _proj(entity_id: str, entity_type: str, state: dict, location_id=None):
    return SimpleNamespace(entity_id=entity_id, entity_type=entity_type, state=state, location_id=location_id)


@pytest.mark.asyncio
async def test_reports_unit_branches_heavy():
    today = date.today()
    rows = [
        _proj("doc:1", "doc", {"doc_type": "invoice", "status": "partial", "contact_id": "c1", "contact_name": "C1", "total": 100, "amount_outstanding": 100, "due_date": (today - timedelta(days=10)).isoformat(), "date": today.isoformat(), "line_items": [{"item_id": "i1", "name": "Item1", "quantity": 2, "line_total": 100, "cost_total": 20}], "cost_total": 20}),
        _proj("doc:2", "doc", {"doc_type": "invoice", "status": "final", "contact_id": "c2", "customer_name": "C2", "total": 50, "due_date": (today - timedelta(days=70)).isoformat(), "date": (today - timedelta(days=40)).isoformat(), "line_items": [{"entity_id": "i2", "description": "Item2", "quantity": 1, "price": 50}], "cost_total": 10}),
        _proj("doc:3", "doc", {"doc_type": "purchase_order", "status": "final", "contact_id": "s1", "contact_name": "S1", "total": 80, "expected_delivery": (today - timedelta(days=100)).isoformat(), "date": (today - timedelta(days=100)).isoformat(), "line_items": [{"item_id": "p1", "name": "P1", "quantity": 4, "line_total": 80}]}),
        _proj("doc:4", "doc", {"doc_type": "purchase_order", "status": "void", "total": 99}),
        _proj("item:1", "item", {"sku": "E1", "name": "Exp", "expires_at": (today + timedelta(days=5)).isoformat(), "status": "ok"}),
        _proj("item:2", "item", {"sku": "E2", "name": "Bad", "expires_at": "not-a-date", "status": "ok"}),
    ]
    session = _FakeSession(rows)
    cid = uuid.uuid4()

    # helper branches
    assert reports._parse_d(None) == 0
    assert reports._parse_d("bad") == 0
    assert reports._in_range(None, None, None) is True
    assert reports._in_range("2026-01-01", "2026-01-02", None) is False
    assert reports._in_range("2026-01-03", None, "2026-01-02") is False

    ar = await reports.ar_aging(company_id=cid, session=session)
    assert len(ar["lines"]) >= 1

    ap = await reports.ap_aging(company_id=cid, session=session)
    assert len(ap["lines"]) >= 1

    s_customer = await reports.sales_report(group_by="customer", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    s_item = await reports.sales_report(group_by="item", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    s_daily = await reports.sales_report(group_by="period", period="daily", date_from=None, date_to=None, company_id=cid, session=session)
    s_weekly = await reports.sales_report(group_by="period", period="weekly", date_from=None, date_to=None, company_id=cid, session=session)
    s_monthly = await reports.sales_report(group_by="period", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    assert s_customer["group_by"] == "customer"
    assert s_item["group_by"] == "item"
    assert s_daily["group_by"] == "period"
    assert s_weekly["group_by"] == "period"
    assert s_monthly["group_by"] == "period"

    p_supplier = await reports.purchases_report(group_by="supplier", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    p_item = await reports.purchases_report(group_by="item", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    p_daily = await reports.purchases_report(group_by="period", period="daily", date_from=None, date_to=None, company_id=cid, session=session)
    p_weekly = await reports.purchases_report(group_by="period", period="weekly", date_from=None, date_to=None, company_id=cid, session=session)
    p_monthly = await reports.purchases_report(group_by="period", period="monthly", date_from=None, date_to=None, company_id=cid, session=session)
    assert p_supplier["group_by"] == "supplier"
    assert p_item["group_by"] == "item"
    assert p_daily["group_by"] == "period"
    assert p_weekly["group_by"] == "period"
    assert p_monthly["group_by"] == "period"

    exp = await reports.expiring_items(days=30, company_id=cid, session=session)
    assert exp["count"] == 1
    assert exp["lines"][0]["sku"] == "E1"
