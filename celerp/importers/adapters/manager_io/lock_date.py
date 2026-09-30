# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The accounting lock date of a Manager business file.

Manager keeps it in one LockDate object: field 1 the date through which periods are
locked, field 2 whether locking is switched on. The date is carried only when locking is
on; a date left behind with locking switched off locks nothing in Manager, so it locks
nothing in Celerp either. A LockDate object that cannot be read refuses the file rather
than dropping a lock the user set.
"""

from __future__ import annotations

from datetime import date

from celerp.importers.adapters.base import ScanError
from celerp.importers.adapters.manager_io.protobuf import DecodeError, decode
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader

LOCK_DATE_TYPE = "4c5dac8f-2d5e-4634-a51b-0bbdd021a499"
# The object holds a date and a flag; anything larger is not a LockDate object.
_MAX_BYTES = 256

UNREADABLE_LOCK_DATE = (
    "Celerp cannot read the lock date in this Manager business file. Open it in Manager, check the "
    "lock date under Settings, save the file, and upload it again."
)


def read_lock_date(reader: ManagerReader) -> date | None:
    """The date through which the source's accounting periods are locked, or None when
    locking is off or no lock date was ever set."""
    rows = reader.conn.execute(
        "SELECT CASE WHEN length(Content) <= ? THEN Content END, length(Content) FROM Objects WHERE ContentType = ?",
        (_MAX_BYTES, LOCK_DATE_TYPE),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1 or int(rows[0][1] or 0) > _MAX_BYTES:
        raise ScanError(UNREADABLE_LOCK_DATE)
    try:
        message = decode(bytes(rows[0][0] or b""))
        locked, through = message.bool(2), message.date(1)
    except DecodeError as exc:
        raise ScanError(UNREADABLE_LOCK_DATE) from exc
    if not locked:
        return None
    if through is None:
        raise ScanError(UNREADABLE_LOCK_DATE)
    return through
