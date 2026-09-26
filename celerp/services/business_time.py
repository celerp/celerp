# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Small shared business-time primitives."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def business_timezone(timezone_name: str | None) -> ZoneInfo:
    """Resolve an IANA business timezone, defaulting an unset value to UTC."""
    if timezone_name is not None and not isinstance(timezone_name, str):
        raise ValueError("business timezone must be an IANA timezone name")
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid business timezone: {name!r}") from exc


def business_date_at(instant: datetime, timezone_name: str | None) -> str:
    """Return the calendar day of an aware instant in an IANA business timezone."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("business-date instant must be timezone-aware")
    return instant.astimezone(business_timezone(timezone_name)).date().isoformat()
