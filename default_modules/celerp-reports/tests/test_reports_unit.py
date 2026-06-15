# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

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


# ---------------------------------------------------------------------------
# Aging drilldown link generation
# ---------------------------------------------------------------------------

def _fake_aging_data(as_of: str, customer_id: str = "c1") -> dict:
    """Minimal aging API response for _aging_view testing."""
    return {
        "as_of": as_of,
        "lines": [
            {
                "customer_id": customer_id,
                "customer_name": "Acme Corp",
                "current": 100.0,
                "d30": 200.0,
                "d60": 0.0,
                "d90": 50.0,
                "d90plus": 300.0,
                "total": 650.0,
            }
        ],
        "buckets": {"current": 100.0, "1-30": 200.0, "31-60": 0.0, "61-90": 50.0, "90+": 300.0},
    }


def test_aging_drilldown_links_ar():
    """AR aging cells with non-zero values link to /docs with correct due_date ranges."""
    from datetime import date, timedelta
    from ui.routes.reports import _aging_view
    today = date.today()
    data = _fake_aging_data(today.isoformat(), "cust-123")
    html = str(_aging_view(data, "AR"))

    # current bucket: due_from=today, no due_to
    assert f"due_from={today.isoformat()}" in html
    # d30 bucket: due_from=today-30, due_to=today-1
    assert f"due_from={(today - timedelta(days=30)).isoformat()}" in html
    assert f"due_to={(today - timedelta(days=1)).isoformat()}" in html
    # d90 bucket present
    assert f"due_from={(today - timedelta(days=90)).isoformat()}" in html
    # d90plus: due_to=today-91, no due_from
    assert f"due_to={(today - timedelta(days=91)).isoformat()}" in html
    # doc_type=invoice for AR
    assert "doc_type=invoice" in html
    # contact_id embedded
    assert "contact_id=cust-123" in html
    # zero bucket (d60=0) must NOT be a link
    assert "d60" not in html or f"due_from={(today - timedelta(days=60)).isoformat()}" not in html


def test_aging_drilldown_links_ap():
    """AP aging cells link with doc_type=purchase_order."""
    from datetime import date
    from ui.routes.reports import _aging_view
    today = date.today()
    data = {
        "as_of": today.isoformat(),
        "lines": [
            {
                "supplier_id": "sup-456",
                "supplier_name": "Vendor Co",
                "current": 500.0,
                "d30": 0.0,
                "d60": 0.0,
                "d90": 0.0,
                "d90plus": 0.0,
                "total": 500.0,
            }
        ],
        "buckets": {},
    }
    html = str(_aging_view(data, "AP"))
    assert "doc_type=purchase_order" in html
    assert f"due_from={today.isoformat()}" in html
    assert "contact_id=sup-456" in html


def test_aging_drilldown_zero_cells_not_linked():
    """Cells with zero amount render plain text, not links."""
    from datetime import date
    from ui.routes.reports import _aging_view
    today = date.today()
    data = _fake_aging_data(today.isoformat())
    html = str(_aging_view(data, "AR"))
    # d60=0 - its specific due range should NOT appear as a link
    from datetime import timedelta
    d60_due_from = (today - timedelta(days=60)).isoformat()
    d60_due_to = (today - timedelta(days=31)).isoformat()
    # The d60 range params should not appear since that bucket is 0
    assert f"due_from={d60_due_from}&due_to={d60_due_to}" not in html


def test_aging_drilldown_unlinked_contact_no_contact_id():
    """Rows with unlinked contact_id do not include contact_id in the drilldown URL."""
    from datetime import date
    from ui.routes.reports import _aging_view
    today = date.today()
    data = {
        "as_of": today.isoformat(),
        "lines": [
            {
                "customer_id": "unlinked",
                "customer_name": "Unlinked Invoices",
                "current": 0.0,
                "d30": 150.0,
                "d60": 0.0,
                "d90": 0.0,
                "d90plus": 0.0,
                "total": 150.0,
            }
        ],
        "buckets": {},
    }
    html = str(_aging_view(data, "AR"))
    assert "contact_id=unlinked" not in html
    assert "doc_type=invoice" in html
