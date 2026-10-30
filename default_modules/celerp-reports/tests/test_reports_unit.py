# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from celerp.services.business_time import business_today
from celerp_reports import routes as reports


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        # The company's settings: none saved, so its business day is the UTC day.
        return None


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt):
        if len(stmt.selected_columns) == 2:
            # Aging: each document's state with the name it shows, worked out by the
            # report's own name expression over these rows.
            return _Res(await _shown_names(self.rows, stmt.selected_columns[1]))
        return _Res(self.rows)


async def _shown_names(rows, shown) -> list:
    """(state, *shown*) for each document of *rows*, *shown* run as SQL over a table that
    holds *rows* as one company's records, so a contact row's name counts as it does live."""
    from sqlalchemy import insert, select, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from celerp.models.projections import Projection

    company_id = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("CREATE TABLE projections (company_id CHAR(32) NOT NULL, "
                                    "entity_id TEXT NOT NULL, entity_type VARCHAR(64) NOT NULL, "
                                    "state JSON NOT NULL, PRIMARY KEY (company_id, entity_id))"))
            await conn.execute(insert(Projection.__table__), [
                {"company_id": company_id, "entity_id": p.entity_id, "entity_type": p.entity_type,
                 "state": p.state} for p in rows])
            result = await conn.execute(select(Projection.state, shown)
                                        .where(Projection.entity_type == "doc")
                                        .order_by(Projection.entity_id))
            return [tuple(row) for row in result.all()]
    finally:
        await engine.dispose()


def _proj(entity_id: str, entity_type: str, state: dict, location_id=None):
    return SimpleNamespace(entity_id=entity_id, entity_type=entity_type, state=state, location_id=location_id)


@pytest.mark.asyncio
async def test_aging_shows_a_party_by_its_contacts_name_over_the_name_the_document_holds():
    """The fake runs the report's own name expression, so a renamed contact shows by its
    current name, a document whose contact has no record by the name it holds, and a supplier
    document by its older supplier fields."""
    today = business_today(None)
    owed = {"doc_type": "invoice", "status": "final", "total": 40, "amount_outstanding": 40,
            "due_date": today.isoformat(), "date": today.isoformat()}
    session = _FakeSession([
        _proj("contact:1", "contact", {"name": "Live Name"}),
        _proj("doc:1", "doc", {**owed, "contact_id": "contact:1", "contact_name": "Old Name"}),
        _proj("doc:2", "doc", {**owed, "contact_id": "contact:gone", "contact_name": "Held Name"}),
        _proj("doc:3", "doc", {**owed, "doc_type": "bill", "supplier_id": "contact:1"}),
    ])
    ar = await reports.ar_aging(company_id=uuid.uuid4(), session=session)
    ap = await reports.ap_aging(company_id=uuid.uuid4(), session=session)
    assert sorted(line["customer_name"] for line in ar["lines"]) == ["Held Name", "Live Name"]
    assert [line.get("supplier_name") for line in ap["lines"]] == ["Live Name"]


@pytest.mark.asyncio
async def test_reports_unit_branches_heavy():
    today = business_today(None)
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


# ---------------------------------------------------------------------------
# Open credits in aging: an open credit note lowers what its customer owes
# ---------------------------------------------------------------------------

def _days_ago(n: int) -> str:
    return (business_today(None) - timedelta(days=n)).isoformat()


def _invoice_state(total: float, outstanding: float, status: str, due_days_ago: int, contact: str = "c1") -> dict:
    return {"doc_type": "invoice", "status": status, "contact_id": contact, "contact_name": "Customer",
            "total": total, "amount_outstanding": outstanding, "due_date": _days_ago(due_days_ago),
            "issue_date": _days_ago(due_days_ago + 30)}


def _credit_state(total: float, outstanding: float, status: str, issued_days_ago: int, contact: str = "c1", **extra) -> dict:
    return {"doc_type": "credit_note", "status": status, "contact_id": contact, "contact_name": "Customer",
            "total": total, "amount_outstanding": outstanding, "issue_date": _days_ago(issued_days_ago),
            "due_date": _days_ago(400), **extra}


async def _ar(rows) -> dict:
    return await reports.ar_aging(company_id=uuid.uuid4(), session=_FakeSession(
        [_proj(f"doc:{i}", "doc", s) for i, s in enumerate(rows)]))


@pytest.mark.asyncio
async def test_ar_aging_open_credit_note_is_negative_in_its_issue_date_bucket():
    # Issued 45 days ago: the credit sits in 31-60 (by issue date, not its due date).
    ar = await _ar([_credit_state(50.0, 50.0, "final", 45)])
    (line,) = ar["lines"]
    assert line["customer_id"] == "c1"
    assert line["d60"] == -50.0 and line["total"] == -50.0
    assert line["d90plus"] == 0.0
    assert ar["buckets"]["31-60"] == -50.0


@pytest.mark.asyncio
async def test_ar_aging_credit_note_partly_applied_shows_remainder():
    ar = await _ar([_invoice_state(100.0, 100.0, "final", 10),
                    _credit_state(100.0, 40.0, "partial", 0, credited=60.0)])
    (line,) = ar["lines"]
    assert line["d30"] == 100.0 and line["current"] == -40.0
    assert line["total"] == 60.0


@pytest.mark.asyncio
async def test_ar_aging_credit_note_from_invoice_settles_and_leaves_remainder():
    # A 100 credit note against a 70 invoice: the invoice is settled, 30 stays on the note.
    ar = await _ar([{**_invoice_state(70.0, 0.0, "paid", 5), "credited": 70.0},
                    _credit_state(100.0, 30.0, "partial", 0, credited=70.0)])
    (line,) = ar["lines"]
    assert line["total"] == -30.0 and line["current"] == -30.0


@pytest.mark.asyncio
async def test_ar_aging_names_invoices_without_a_contact_as_unlinked():
    """Invoices that link no contact share one line, named as unlinked whatever name each one carries."""
    first, second = ({**_invoice_state(10.0, 10.0, "final", 5, contact=""), "contact_name": name}
                     for name in ("Apex", "Northgate"))
    for rows in ([first, second], [second, first]):
        (line,) = (await _ar(rows))["lines"]
        assert (line["customer_id"], line["customer_name"], line["total"]) == ("unlinked", "Unlinked Invoices", 20.0)


@pytest.mark.asyncio
async def test_ar_aging_excludes_closed_and_void_credit_notes():
    ar = await _ar([_credit_state(50.0, 0.0, "paid", 3), _credit_state(50.0, 50.0, "void", 3),
                    _credit_state(50.0, 50.0, "draft", 3), _invoice_state(20.0, 20.0, "final", -5)])
    (line,) = ar["lines"]
    assert line["total"] == 20.0 and line["current"] == 20.0
