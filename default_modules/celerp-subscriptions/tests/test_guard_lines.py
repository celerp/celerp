# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The small guards of the subscriptions module that no journey reaches on its own: each test
here fails when its guard is deleted (fix-round mutation sweep over every guard line)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agreement_kit import company_id, member, register
from celerp_subscriptions import billing
from celerp_subscriptions.periods import Schedule, ScheduleError


def _company(*terms: dict) -> SimpleNamespace:
    return SimpleNamespace(settings={"payment_terms": list(terms)})


def test_an_attempt_with_no_creation_time_is_never_retried():
    assert billing.retryable_attempt_time(None) is False
    assert billing.retryable_attempt_time(datetime.now(timezone.utc)) is True
    assert billing.retryable_attempt_time(datetime.now(timezone.utc) - billing.RECOVERY_RETRY_AGE * 2) is False


def test_the_due_date_comes_from_the_named_payment_term_only():
    day = date(2026, 3, 1)
    company = _company({"name": "Net 30", "days": 30}, {"name": "Net 7", "days": 7}, {"days": 90})
    assert billing.compute_due_date(day, "Net 7", company) == (day + timedelta(days=7)).isoformat()
    assert billing.compute_due_date(day, "Net 30", company) == (day + timedelta(days=30)).isoformat()
    # No term chosen: no due date, even beside a saved term that has no name.
    assert billing.compute_due_date(day, None, company) is None
    assert billing.compute_due_date(day, "", company) is None
    assert billing.compute_due_date(day, "Net 60", company) is None


@pytest.mark.asyncio
async def test_a_consent_recorded_by_no_member_names_no_role(client, session):
    """An unattended write stores its consent as by "None"; that names nobody, so it holds no
    role, while a member's own id gives that member's role."""
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    _, uid = await member(client, session, hd, "manager")
    assert await billing._member_role(session, cid, uid) == "manager"
    for nobody in ("None", "", None, "not-a-member-id"):
        assert await billing._member_role(session, cid, nobody) is None


@pytest.mark.parametrize("state, key", [
    ({"anchor_date": ""}, "subscriptions.err_schedule_date"),
    ({"anchor_date": "9999-12-01"}, "subscriptions.err_interval_calendar"),
    ({"anchor_date": "9990-01-01", "frequency": "custom", "custom_interval_days": 36500},
     "subscriptions.err_interval_calendar"),
], ids=["no-start", "months-past-the-calendar", "days-past-the-calendar"])
def test_a_schedule_the_calendar_cannot_hold_is_refused_by_its_reason(state, key):
    try:
        Schedule.from_state(state)
    except ScheduleError as exc:
        assert exc.key == key
    else:
        raise AssertionError(f"{state} built a schedule")


def test_a_period_past_the_end_of_the_calendar_does_not_exist():
    schedule = Schedule.from_state({"anchor_date": "9990-01-01", "frequency": "annually"})
    assert schedule.exists(5) and not schedule.exists(20)


def test_nothing_is_due_before_the_first_bill_date():
    schedule = Schedule.from_state({"anchor_date": "2026-08-01", "frequency": "monthly"})
    assert schedule.latest_due(date(2026, 7, 15)) is None
    assert schedule.latest_due(date(2026, 8, 1)) == 0


def test_an_agreement_whose_term_holds_no_period_has_none_due():
    # Cancelled at the end of period 0, then its term start moved past that period.
    schedule = Schedule.from_state({"anchor_date": "2026-01-01", "frequency": "monthly",
                                    "term_start": "2026-03-01", "cancel_at_period_end": 0})
    assert schedule.first() is None
    assert schedule.latest_due(date(2026, 4, 5)) is None
    assert schedule.due(date(2026, 4, 5)) == []


@pytest.mark.asyncio
async def test_a_run_by_no_member_reads_as_a_former_member(session):
    from ui.i18n import t
    from celerp_subscriptions import summary

    for nobody in ("None", "not-a-member-id", str(uuid.uuid4())):
        assert await summary._member_name(session, nobody) == t("subscriptions.runs_former_member")


def test_a_document_no_agreement_made_has_no_period_badge():
    from celerp_subscriptions.ui_routes import doc_period_badge

    period = {"service_start": "2026-03-01", "service_end": "2026-03-31"}
    assert doc_period_badge({}) is None
    assert doc_period_badge(period) is None
    assert doc_period_badge({"source_agreement": "doc:SUB-1", "service_start": "2026-03-01"}) is None
    assert doc_period_badge({**period, "source_agreement": "doc:SUB-1", "entity_id": "doc:INV-1"}) is not None
