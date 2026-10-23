# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every API decision about "today" uses the company's business day, never the server's.

The clock is frozen years away from the machine's own date, at an instant where the
company's day is ahead of the UTC day (Bangkok) or behind it (Honolulu), so a surface
that still reads the server's date or the UTC day answers for the wrong day."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from celerp.models.company import Company, User
from celerp.models.projections import Projection
from company_today_support import CASES, freeze

pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture(params=CASES)
async def co(request, client, session, monkeypatch):
    """A registered company in the case's timezone, on the frozen clock."""
    tz, instant, day = request.param
    email = f"today-{uuid.uuid4().hex[:8]}@test.example"
    r = await client.post("/auth/register", json={"company_name": "Today Co", "email": email,
                                                  "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.patch("/companies/me", headers=headers, json={"settings": {"timezone": tz}})
    assert r.status_code == 200, r.text
    company_id = uuid.UUID((await client.get("/companies/me", headers=headers)).json()["id"])
    user_id = (await session.execute(select(User.id).where(User.email == email))).scalar_one()
    freeze(monkeypatch, instant)
    return SimpleNamespace(tz=tz, instant=instant, day=day, headers=headers, id=company_id, user_id=user_id)


async def _seed(session, co, entity_id: str, entity_type: str, state: dict) -> None:
    session.add(Projection(company_id=co.id, entity_id=entity_id, entity_type=entity_type, version=1,
                           updated_at=datetime.now(timezone.utc), state=state))
    await session.commit()


async def _invoices_due(session, co) -> None:
    """One unpaid invoice due yesterday (overdue) and one due today (not yet), in company days."""
    for name, due in (("late", co.day - timedelta(days=1)), ("due-today", co.day)):
        await _seed(session, co, f"doc:{name}", "doc", {
            "doc_type": "invoice", "status": "final", "doc_number": name, "currency": "USD",
            "contact_id": "contact:c", "line_items": [], "total": 50, "amount_outstanding": 50,
            "due_date": due.isoformat()})


async def test_business_today_is_the_company_day(co, session):
    from celerp.services.business_time import business_date_of, business_today, company_today

    assert business_today(co.tz) == co.day
    assert business_date_of(None, co.tz) == co.day.isoformat()
    assert await company_today(session, co.id) == co.day


async def test_overdue_documents_are_those_due_before_the_company_day(co, client, session):
    await _invoices_due(session, co)
    listed = (await client.get("/docs?doc_type=invoice&overdue_only=1", headers=co.headers)).json()["items"]
    assert [x["id"] for x in listed] == ["doc:late"]
    summary = (await client.get("/docs/summary?doc_type=invoice", headers=co.headers)).json()
    assert summary["overdue_count"] == 1
    csv = (await client.get("/docs/export/csv?doc_type=invoice&overdue_only=1", headers=co.headers)).text
    assert "late" in csv and "due-today" not in csv


async def test_dashboard_counts_overdue_by_the_company_day(co, client, session):
    await _invoices_due(session, co)
    await _seed(session, co, "mfg:late", "mfg_order", {"status": "in_progress",
                                                       "due_date": (co.day - timedelta(days=1)).isoformat()})
    await _seed(session, co, "mfg:today", "mfg_order", {"status": "in_progress", "due_date": co.day.isoformat()})
    kpis = (await client.get("/dashboard/kpis", headers=co.headers)).json()
    assert kpis["sales"]["invoices_overdue"] == 1
    assert kpis["manufacturing"]["orders_overdue"] == 1


async def test_aging_counts_days_past_due_from_the_company_day(co, client, session):
    await _invoices_due(session, co)
    lines = (await client.get("/reports/ar-aging", headers=co.headers)).json()["lines"]
    assert sum(line["current"] for line in lines) == pytest.approx(50)
    assert sum(line["d30"] for line in lines) == pytest.approx(50)


async def test_overdue_reminder_uses_the_company_day(co, session):
    from celerp.services.reorder import _detect_overdue

    await _invoices_due(session, co)
    found = await _detect_overdue(session, await session.get(Company, co.id))
    assert [x["entity_id"] for x in found] == ["doc:late"]


async def _items_expiring(session, co, window: int) -> None:
    for name, offset in (("inside", window), ("outside", window + 1)):
        await _seed(session, co, f"item:{name}", "item", {
            "sku": name, "name": name, "status": "available", "quantity": 1,
            "expires_at": (co.day + timedelta(days=offset)).isoformat()})


async def test_expiring_report_counts_from_the_company_day(co, client, session):
    await _items_expiring(session, co, 30)
    body = (await client.get("/reports/expiring?days=30", headers=co.headers)).json()
    assert [line["name"] for line in body["lines"]] == ["inside"]


async def test_expiring_reminder_counts_from_the_company_day(co, session):
    from celerp.services.reorder import _EXPIRING_WINDOW_DAYS, _detect_expiring

    await _items_expiring(session, co, _EXPIRING_WINDOW_DAYS)
    found = await _detect_expiring(session, await session.get(Company, co.id))
    assert [x["name"] for x in found] == ["inside"]


async def _subscription(client, co) -> str:
    r = await client.post("/docs", headers=co.headers, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:c", "frequency": "weekly",
        "line_items": [{"description": "Support", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]})
    assert r.status_code in {200, 201}, r.text
    return r.json()["id"]


async def test_subscription_dates_count_from_the_company_day(co, client):
    sub_id = await _subscription(client, co)
    week = (co.day + timedelta(weeks=1)).isoformat()
    r = await client.post(f"/subscriptions/{sub_id}/activate", headers=co.headers)
    assert r.status_code == 200, r.text
    assert r.json()["next_run_date"] == week
    assert (await client.post(f"/subscriptions/{sub_id}/pause", headers=co.headers)).status_code == 200
    r = await client.post(f"/subscriptions/{sub_id}/resume", headers=co.headers)
    assert r.status_code == 200, r.text
    assert r.json()["next_run_date"] == week
    r = await client.post(f"/subscriptions/{sub_id}/generate", headers=co.headers)
    assert r.status_code == 200, r.text
    made = (await client.get(f"/docs/{r.json()['doc_id']}", headers=co.headers)).json()
    assert made["issue_date"] == co.day.isoformat()


async def test_ai_usage_month_starts_at_the_company_month(co, client, session):
    from zoneinfo import ZoneInfo

    import uuid

    from celerp.models.ai import AIUsage

    month_start = datetime(co.day.year, co.day.month, 1, tzinfo=ZoneInfo(co.tz)).astimezone(timezone.utc)
    for when in (month_start - timedelta(hours=1), month_start + timedelta(hours=1)):
        session.add(AIUsage(company_id=co.id, user_id=co.user_id, operation_id=uuid.uuid4(),
                            credits=1, created_at=when))
    await session.commit()
    users = (await client.get("/ai/usage-stats", headers=co.headers)).json()["users"]
    assert [u["query_count"] for u in users] == [1]


async def test_repaired_payment_without_a_date_posts_on_the_company_day(co):
    from celerp_admin.routes import _payment_day

    assert _payment_day({}, None, co.tz) == co.day.isoformat()


async def test_undated_legacy_credit_note_posts_from_the_company_day(co, session, monkeypatch):
    from unittest.mock import AsyncMock

    import celerp.services.auto_je as auto_je
    import celerp_docs.legacy_credit_notes as legacy

    open_day = AsyncMock(return_value=None)
    monkeypatch.setattr(legacy, "_open_day", open_day)
    monkeypatch.setattr(legacy, "_posted_entry", AsyncMock(return_value=True))
    monkeypatch.setattr(auto_je, "true_up_credit_note_rate", AsyncMock())
    await legacy._post(session, co.id, "doc:cn", {"doc_type": "credit_note"}, {}, "credit note")
    assert open_day.await_args.args[2] == co.day.isoformat()
