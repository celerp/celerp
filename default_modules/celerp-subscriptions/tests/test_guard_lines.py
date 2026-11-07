# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The small guards of the subscriptions module that no journey reaches on its own: each test
here fails when its guard is deleted (fix-round mutation sweep over every guard line)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from celerp_subscriptions import billing


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
