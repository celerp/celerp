# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Small shared business-time primitives."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ui.i18n import t


def business_timezone(timezone_name: str | None) -> ZoneInfo:
    """Resolve an IANA business timezone, defaulting an unset value to UTC."""
    if timezone_name is not None and not isinstance(timezone_name, str):
        raise ValueError(t("error.timezone_not_text"))
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(t("error.invalid_timezone", value=name)) from exc


def business_date_at(instant: datetime, timezone_name: str | None) -> str:
    """Return the calendar day of an aware instant in an IANA business timezone."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("business-date instant must be timezone-aware")
    return instant.astimezone(business_timezone(timezone_name)).date().isoformat()


def business_date_of(recorded: object, timezone_name: str | None) -> str:
    """The business day of an operation, as YYYY-MM-DD.

    A recorded calendar date is that day; a recorded timestamp with an offset is its day in
    the business timezone; one without an offset is the day it names. With nothing recorded
    the operation is happening now, so it is today in the business timezone. Raises
    ValueError, with a message for the user, for an unusable timezone or an unreadable date.
    """
    if not recorded:
        return business_date_at(datetime.now(timezone.utc), timezone_name)
    raw = str(recorded)
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00")) if "T" in raw else None
        day = date.fromisoformat(raw[:10])
    except ValueError:
        raise ValueError(f"{recorded} is not a date. Enter it as YYYY-MM-DD.") from None
    if instant is not None and instant.utcoffset() is not None:
        return business_date_at(instant, timezone_name)
    return day.isoformat()
