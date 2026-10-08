# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document created without a date is dated the company's today, never the server's.

The company's timezone decides the day; a company without one keeps UTC days."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from celerp.services.company_lock import locked_company
from test_posting_roles_older_stock import _clock

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("tz, instant, host_day, day", [
    # Oct 1 in New York while the server's own calendar already reads Oct 2.
    ("America/New_York", datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc), date(2026, 10, 2), "2026-10-01"),
    # No timezone: the UTC day, Oct 1, while the server reads Oct 2.
    (None, datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc), date(2026, 10, 2), "2026-10-01"),
])
async def test_a_new_document_is_dated_the_company_today(client, session, auth, monkeypatch, tz, instant, host_day, day):
    import datetime as datetime_module

    import celerp.services.business_time as business_time
    import celerp_docs.routes as doc_routes

    company = await locked_company(session, auth["company_id"])
    company.settings = {k: v for k, v in company.settings.items() if k != "timezone"} | ({"timezone": tz} if tz else {})
    await session.commit()
    _clock(monkeypatch, instant, host_day)
    # The docs routes hold their own date and datetime names: freeze those too.
    monkeypatch.setattr(doc_routes, "_date", datetime_module.date)
    monkeypatch.setattr(doc_routes, "datetime", business_time.datetime)
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "invoice", "line_items": []})
    assert r.status_code == 200, r.text
    got = (await client.get(f"/docs/{r.json()['id']}", headers=auth["headers"])).json()
    assert got["issue_date"] == day
