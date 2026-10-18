# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The frozen clock the company-today tests share: an instant years away from the machine's
date where the company's day is ahead of the UTC day (Bangkok) or behind it (Honolulu)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

CASES = [
    pytest.param(("Asia/Bangkok", datetime(2031, 3, 31, 20, 0, tzinfo=timezone.utc), date(2031, 4, 1)),
                 id="company-day-ahead"),
    pytest.param(("Pacific/Honolulu", datetime(2031, 4, 1, 5, 0, tzinfo=timezone.utc), date(2031, 3, 31)),
                 id="company-day-behind"),
]


def freeze(monkeypatch, instant: datetime) -> None:
    """Freeze the one clock every business-day decision reads."""
    import celerp.services.business_time as business_time

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(business_time, "datetime", Frozen)
