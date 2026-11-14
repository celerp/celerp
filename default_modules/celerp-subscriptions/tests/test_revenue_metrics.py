# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Revenue metrics: MRR, ARR, churn and cohorts from the agreements' own history.

Initial conditions per test: a newly registered company (books in USD, UTC) signed in to the UI
(owner_ui), its API calls answered in process. Each agreement step runs on a frozen business
day, and the ledger events it wrote are dated that day (noon UTC), as they would be had the step
happened then, so the metrics read a history spread over months.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from agreement_kit import agreement, freeze_day, generate_due, member, revoke
from test_helpers import ui_signed_in

pytestmark = pytest.mark.asyncio


def _line(description: str, quantity: float, price: float) -> dict:
    return {"description": description, "quantity": quantity, "unit_price": price, "line_total": quantity * price}


async def _on(session, monkeypatch, day: str, step):
    """Run ``step`` (a coroutine function) on business day ``day``; what it wrote is dated then."""
    freeze_day(monkeypatch, day)
    before = (await session.execute(text("SELECT coalesce(max(id), 0) FROM ledger"))).scalar()
    out = await step()
    await session.execute(text("UPDATE ledger SET ts = :ts WHERE id > :id"),
                          {"ts": datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc), "id": before})
    await session.flush()
    return out


async def _metrics(api, **params):
    return await api.get("/subscriptions/metrics", params=params)


def _says(r, key: str, **params) -> bool:
    detail = r.json().get("detail")
    return isinstance(detail, dict) and detail.get("message_key") == key and detail.get("params") == params


async def test_mrr_arr_churn_cohort_seeded(owner_ui, session, monkeypatch):
    """Four agreements over January to March 2026:
    A monthly, 3 seats at 10 from 1 Jan, 5 seats from 15 Feb (expansion 20);
    B annually at 120 from 1 Jan (MRR 10), cancelled 10 Feb (churn);
    C monthly at 20 from 1 Feb with a 10% discount and 7% tax (MRR 18, tax excluded), paused 5 Mar;
    D monthly at EUR 100 from 1 Mar at the agreement's rate 1.1 (MRR 110; activation needs the rate).
    Month ends: Jan MRR 40 (A 30, B 10), Feb 68 (A 50, C 18), Mar 160 (A 50, D 110)."""
    api = owner_ui.api
    a = await _on(session, monkeypatch, "2026-01-01", lambda: agreement(
        api, {}, start="2026-01-01", line_items=[_line("Team plan", 3, 10.0)]))
    b = await _on(session, monkeypatch, "2026-01-01", lambda: agreement(
        api, {}, start="2026-01-01", frequency="annually", line_items=[_line("Yearly plan", 1, 120.0)]))
    seats = (await api.get(f"/docs/{a}")).json()["line_items"][0]["line_id"]
    r = await _on(session, monkeypatch, "2026-02-15", lambda: api.post(f"/subscriptions/{a}/amend", json={
        "effective": "2026-02-15", "quantities": {seats: 5}, "request_key": uuid.uuid4().hex}))
    assert r.status_code == 200, r.text
    c = await _on(session, monkeypatch, "2026-02-01", lambda: agreement(
        api, {}, start="2026-02-01", discount=10, discount_type="percentage", tax_rate=7,
        line_items=[_line("Starter plan", 1, 20.0)]))
    r = await _on(session, monkeypatch, "2026-02-10", lambda: api.post(f"/subscriptions/{b}/cancel"))
    assert r.status_code == 200, r.text
    r = await _on(session, monkeypatch, "2026-03-05", lambda: api.post(f"/subscriptions/{c}/pause"))
    assert r.status_code == 200, r.text
    await _on(session, monkeypatch, "2026-03-01", lambda: agreement(
        api, {}, start="2026-03-01", currency="EUR", conversion_rate=1.1, line_items=[_line("Euro plan", 1, 100.0)]))

    freeze_day(monkeypatch, "2026-04-02")
    r = await _metrics(api, **{"from": "2026-01-01", "to": "2026-03-31"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "USD"
    months = {m["month"]: m for m in body["months"]}
    assert list(months) == ["2026-01", "2026-02", "2026-03"]
    jan, feb, mar = months["2026-01"], months["2026-02"], months["2026-03"]
    assert (jan["mrr"], jan["arr"], jan["active"], jan["new_mrr"]) == (40.0, 480.0, 2, 40.0)
    assert (feb["mrr"], feb["active"], feb["new_mrr"], feb["expansion_mrr"], feb["churned_mrr"]) == (68.0, 2, 18.0, 20.0, 10.0)
    assert (feb["logo_churn"], feb["revenue_churn"]) == (0.5, 0.25)  # 1 of 2; 10 of 40
    assert (mar["mrr"], mar["active"], mar["new_mrr"], mar["churned_mrr"]) == (160.0, 2, 110.0, 18.0)
    assert mar["contraction_mrr"] == 0.0 and mar["logo_churn"] == 0.5
    cohorts = {c["month"]: c for c in body["cohorts"]}
    assert cohorts["2026-01"]["size"] == 2 and cohorts["2026-01"]["active"] == [2, 1, 1]
    assert cohorts["2026-01"]["mrr"] == [40.0, 50.0, 50.0]
    assert cohorts["2026-02"]["size"] == 1 and cohorts["2026-02"]["active"] == [1, 0]
    assert cohorts["2026-03"]["size"] == 1 and cohorts["2026-03"]["mrr"] == [110.0]

    r = await _metrics(api, **{"from": "2026-03-01", "to": "2026-03-31", "currency": "EUR"})
    assert r.status_code == 200, r.text
    [only] = r.json()["months"]
    assert (only["mrr"], only["active"]) == (100.0, 1)

    page = await owner_ui.get("/revenue", params={"from": "2026-01-01", "to": "2026-03-31"})
    assert page.status_code == 200, page.text[:300]
    for shown in ("Monthly recurring revenue", "160.00", "480.00", "Cohorts", "50%"):
        assert shown in page.text, shown


async def test_revenue_metrics_empty_state(owner_ui, monkeypatch):
    """No agreements: the Revenue page says so and offers New subscriber; the metrics of a range
    with no data are zeros for every month, with the definitions still on the page."""
    freeze_day(monkeypatch, "2026-03-15")
    r = await _metrics(owner_ui.api, **{"from": "2026-01-01", "to": "2026-03-15"})
    assert r.status_code == 200, r.text
    assert [(m["month"], m["mrr"], m["active"], m["logo_churn"]) for m in r.json()["months"]] == [
        ("2026-01", 0.0, 0, None), ("2026-02", 0.0, 0, None), ("2026-03", 0.0, 0, None)]
    assert r.json()["cohorts"] == []
    page = await owner_ui.get("/revenue")
    assert page.status_code == 200, page.text[:300]
    assert "No recurring revenue yet. Add a subscriber or plan to see MRR." in page.text
    assert "New subscriber" in page.text and "Monthly recurring revenue" in page.text
    # Filtered to one currency, nothing in it is not "no revenue": the months show their zeros.
    page = await owner_ui.get("/revenue", params={"from": "2026-01-01", "to": "2026-03-15", "currency": "EUR"})
    assert page.status_code == 200 and "No recurring revenue yet" not in page.text and "2026-03" in page.text


async def test_metrics_requires_view_financial_reports(owner_ui, session):
    """A member whose role lacks view_financial_reports is refused the metrics (403) and the
    Revenue page."""
    owner = {}  # owner_ui.api already carries the owner's token
    headers, _uid = await member(owner_ui.api, session, owner, "manager")
    await revoke(owner_ui.api, owner, "view_financial_reports", "manager")
    r = await owner_ui.api.get("/subscriptions/metrics", headers=headers)
    assert r.status_code == 403, r.text
    async with ui_signed_in(headers["Authorization"].split()[1]) as ui:
        assert (await ui.get("/revenue")).status_code in (302, 303, 403)


@pytest.mark.parametrize(("params", "key", "named"), [
    ({"from": "2026-03-01", "to": "2026-02-01"}, "subscriptions.err_metrics_order", "from"),
    ({"from": "2015-01-01", "to": "2026-01-02"}, "subscriptions.err_metrics_range", "to"),
    ({"currency": "ZZZ"}, "subscriptions.err_metrics_currency", "currency"),
])
async def test_metrics_range_validation(owner_ui, monkeypatch, params, key, named):
    """From after to, a range above 10 years and an unknown currency are refused (422) naming the
    parameter."""
    freeze_day(monkeypatch, "2026-03-15")
    r = await _metrics(owner_ui.api, **params)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == key and detail["params"]["parameter"] == named, r.text


async def test_metrics_read_the_history_in_the_same_queries_and_agree_with_every_event_replayed(
        owner_ui, session, monkeypatch):
    """Agreement A (3 seats at 10 from 1 Jan; amended on 10 Feb to 5 seats from 15 Mar, which no
    later event but its bills' records follows) and B (20 from 1 Jan) bill each month; B's
    cancel is recorded after its March bill but dated 20 Feb, so it waits for that bill (MRR
    50 at each month end: Jan and Feb A 30 + B 20, Mar A 50). The events that only write the billing records are passed
    over, yet the answer equals the one measured by replaying every event of each agreement;
    two more months of bills add no query."""
    from sqlalchemy import event, select

    from celerp.models.ledger import LedgerEntry
    from celerp_subscriptions import billing, metrics

    api = owner_ui.api
    a = await _on(session, monkeypatch, "2026-01-01", lambda: agreement(
        api, {}, start="2026-01-01", line_items=[_line("Team plan", 3, 10.0)]))
    b = await _on(session, monkeypatch, "2026-01-01", lambda: agreement(
        api, {}, start="2026-01-01", line_items=[_line("Solo plan", 1, 20.0)]))
    seats = (await api.get(f"/docs/{a}")).json()["line_items"][0]["line_id"]

    async def bill(month: str, eids: tuple[str, ...]) -> None:
        for eid in eids:
            r = await _on(session, monkeypatch, f"{month}-01", lambda e=eid: generate_due(api, {}, e))
            assert r.status_code == 200, r.text

    for month in ("2026-01", "2026-02"):
        await bill(month, (a, b))
    r = await _on(session, monkeypatch, "2026-02-10", lambda: api.post(f"/subscriptions/{a}/amend", json={
        "effective": "2026-03-15", "quantities": {seats: 5}, "request_key": uuid.uuid4().hex}))
    assert r.status_code == 200, r.text
    await bill("2026-03", (a, b))
    r = await _on(session, monkeypatch, "2026-02-20", lambda: api.post(f"/subscriptions/{b}/cancel"))
    assert r.status_code == 200, r.text
    cid = (await session.execute(text("SELECT company_id FROM projections WHERE entity_id = :e"), {"e": a})).scalar()
    records = [e for e in (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id.in_((a, b)), LedgerEntry.event_type == "doc.updated"))).scalars()
        if set(e.data["fields_changed"]) <= billing.BILLING_RECORDS]
    assert records, "the bills wrote no billing records"

    async def every_event(session, company_id):
        rows = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_((a, b))).order_by(LedgerEntry.id))).scalars()
        out: dict[str, list] = {}
        for row in rows:
            out.setdefault(row.entity_id, []).append(row)
        return out

    statements: list[str] = []
    conn = (await session.connection()).sync_connection

    def record(_conn, _cursor, statement, *args):
        statements.append(statement)

    async def measured(start: str, end: str) -> tuple[dict, int]:
        statements.clear()
        event.listen(conn, "before_cursor_execute", record)
        try:
            body = await metrics.revenue_metrics(
                session, cid, datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date(), None)
        finally:
            event.remove(conn, "before_cursor_execute", record)
        return body, len(statements)

    freeze_day(monkeypatch, "2026-04-02")
    shown, queries = await measured("2026-01-01", "2026-03-31")
    months = {m["month"]: m for m in shown["months"]}
    assert (months["2026-01"]["mrr"], months["2026-02"]["mrr"], months["2026-03"]["mrr"]) == (50.0, 50.0, 50.0)
    with monkeypatch.context() as m:
        m.setattr(metrics, "_histories", every_event)
        assert (await metrics.revenue_metrics(session, cid, datetime(2026, 1, 1).date(),
                                              datetime(2026, 3, 31).date(), None)) == shown

    for month in ("2026-04", "2026-05"):
        await bill(month, (a,))
    freeze_day(monkeypatch, "2026-06-02")
    longer, more = await measured("2026-01-01", "2026-05-31")
    assert more == queries
    with monkeypatch.context() as m:
        m.setattr(metrics, "_histories", every_event)
        assert (await metrics.revenue_metrics(session, cid, datetime(2026, 1, 1).date(),
                                              datetime(2026, 5, 31).date(), None)) == longer
