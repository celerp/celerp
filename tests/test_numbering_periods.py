# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Document numbering follows one period rule.

A counter resets only when the pattern shows a period (year, month, day) and the company's
own calendar day has moved into a later one. A pattern without a year never resets, a
saved next number is kept, the period never moves back, and the preview on the Numbering
tab is the reference the next document gets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from celerp_docs import sequences
from celerp_docs.sequences import get_all_sequences, next_doc_ref, update_sequence
from company_today_support import freeze as _freeze_business_day


def freeze(monkeypatch, instant: datetime) -> None:
    """Freeze the clock wherever numbering could read it: the business day, and any clock of
    the numbering module's own."""
    _freeze_business_day(monkeypatch, instant)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(sequences, "datetime", Frozen, raising=False)


def _company(**settings) -> SimpleNamespace:
    return SimpleNamespace(settings=settings)


def _preview(company, doc_type: str = "invoice") -> str:
    return next(s["preview"] for s in get_all_sequences(company) if s["doc_type"] == doc_type)


def test_pattern_without_year_never_resets(monkeypatch):
    company = _company(sequences={"invoice": {
        "prefix": "INV", "pattern": "{PREFIX}-{####}", "next": 42, "year": 2026, "month": 12, "day": 31}})
    freeze(monkeypatch, datetime(2027, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert next_doc_ref(company, "invoice") == "INV-0042"


def test_saved_next_without_stored_day_is_kept(monkeypatch):
    freeze(monkeypatch, datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))
    company = _company()
    update_sequence(company, "invoice", next_num=500)
    assert next_doc_ref(company, "invoice") == "INV-2610-0500"


def test_saved_next_after_the_period_turned_is_kept(monkeypatch):
    """A counter last used in September, set to 500 in October: October's documents start at 500."""
    company = _company(sequences={"invoice": {
        "prefix": "INV", "pattern": "{PREFIX}-{YY}{MM}-{####}", "next": 7, "year": 2026, "month": 9, "day": 30}})
    freeze(monkeypatch, datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))
    assert update_sequence(company, "invoice", next_num=500)["preview"] == "INV-2610-0500"
    assert _preview(company) == "INV-2610-0500"
    assert next_doc_ref(company, "invoice") == "INV-2610-0500"
    freeze(monkeypatch, datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
    assert next_doc_ref(company, "invoice") == "INV-2611-0001"


def test_numbering_uses_company_business_day(monkeypatch):
    """18:00 UTC on 31 October is already 1 November in Bangkok."""
    company = _company(timezone="Asia/Bangkok")
    freeze(monkeypatch, datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc))
    assert next_doc_ref(company, "invoice") == "INV-2611-0001"


def test_numbering_never_moves_back_a_period(monkeypatch):
    company = _company(timezone="Asia/Bangkok")
    freeze(monkeypatch, datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc))
    assert next_doc_ref(company, "invoice") == "INV-2611-0001"
    company.settings["timezone"] = "America/Los_Angeles"
    freeze(monkeypatch, datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc))
    assert next_doc_ref(company, "invoice") == "INV-2611-0002"


def test_day_token_without_month_resets_yearly(monkeypatch):
    """{DD} alone cannot tell 5 January from 5 February, so it counts through the year."""
    company = _company(sequences={"invoice": {"prefix": "INV", "pattern": "{PREFIX}-{DD}-{##}"}})
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    refs = []
    for offset in range(40):
        freeze(monkeypatch, start + timedelta(days=offset))
        refs.append(next_doc_ref(company, "invoice"))
    assert len(set(refs)) == len(refs), refs


@pytest.mark.parametrize("pattern", ["{PREFIX}-{YY}{MM}-{####}", "{PREFIX}-{YYYY}-{####}",
                                     "{PREFIX}-{YY}{MM}{DD}-{###}", "{PREFIX}-{####}"])
def test_preview_equals_next_ref(monkeypatch, pattern):
    company = _company(timezone="Asia/Bangkok")
    freeze(monkeypatch, datetime(2026, 12, 31, 10, 0, tzinfo=timezone.utc))
    update_sequence(company, "invoice", pattern=pattern)
    next_doc_ref(company, "invoice")
    next_doc_ref(company, "invoice")
    # 1 January in Bangkok: every period token moves on.
    freeze(monkeypatch, datetime(2027, 1, 1, 1, 0, tzinfo=timezone.utc))
    shown = _preview(company)
    edited = update_sequence(company, "invoice", prefix="INV")["preview"]
    assert shown == edited == next_doc_ref(company, "invoice")


async def _bad_timezone(session, company_id) -> dict:
    """Store a timezone the app cannot resolve, as older data may hold, and return the counters."""
    from celerp.services.company_lock import locked_company

    session.expire_all()
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "timezone": "Mars/Olympus"}
    await session.commit()
    return dict(company.settings.get("sequences") or {})


async def _counters(session, company_id) -> dict:
    from celerp.models.company import Company

    session.expire_all()
    return dict((await session.get(Company, company_id)).settings.get("sequences") or {})


@pytest.mark.asyncio
async def test_numbering_bad_timezone_refuses_issue_with_the_message(client, session):
    from celerp.models.projections import Projection
    from test_money_boundary import _company as _api_company

    auth = await _api_company(session, "USD")
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}]})
    assert r.status_code == 200, r.text
    draft_id = r.json()["id"]
    before = await _bad_timezone(session, auth["company_id"])
    message = "Mars/Olympus isn't a time zone Celerp recognizes. Choose one from the list."

    r = await client.post(f"/docs/{draft_id}/finalize", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == message
    session.expire_all()
    draft = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": draft_id})
    assert draft.state["status"] == "draft"

    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == message
    assert await _counters(session, auth["company_id"]) == before


@pytest.mark.asyncio
async def test_numbering_preview_bad_timezone_returns_the_message(client, session):
    from test_money_boundary import _company as _api_company

    auth = await _api_company(session, "USD")
    await _bad_timezone(session, auth["company_id"])
    r = await client.get("/docs/sequences", headers=auth["headers"])
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "Mars/Olympus isn't a time zone Celerp recognizes. Choose one from the list."
