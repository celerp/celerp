# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A period's email reads one way everywhere, over every reader and every stored state
send_census finds in the code.

Only billing.send_state reads a send record's stored state or time. Every state the code can
store, fresh, cut off by age or with a time that cannot be read, with its document emailed or
not and with the next pass due to send it or not, read at every moment around its cut-off,
gives a state with the time it began; a reading that changes after someone read the summary
began after they read it, so the summary lists it again. Each state has a summary kind, a mark
on its period and a History line, and only a cut-off send is offered as Send again. A new
reader or a new stored state turns these red without anyone editing a list."""
from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_census  # noqa: E402

_EN = json.loads((send_census.ROOT / "ui/locales/en.json").read_text(encoding="utf-8"))
_T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def _records() -> list[dict]:
    """Every stored state the census finds, written at _T0, with no time, and with a time that
    cannot be read."""
    return [{"state": state, **at} for state in sorted(send_census.written_states())
            for at in ({"at": _T0.isoformat()}, {}, {"at": "not a time"})]


def _moments() -> list[datetime]:
    """Read times from the write through past the cut-off, on both sides of it."""
    from celerp_subscriptions import billing
    cut = _T0 + billing.SEND_TIMEOUT
    return [_T0, _T0 + timedelta(seconds=1), cut - timedelta(seconds=1), cut, cut + timedelta(seconds=1),
            cut + timedelta(days=1)]


def _readings():
    """(record, emailed, waits, now, state, since) over the whole table."""
    from celerp_subscriptions import billing
    for record, emailed, waits, now in itertools.product(_records(), (False, True), (False, True), _moments()):
        yield (record, emailed, waits, now, *billing.send_state(record, now, emailed=emailed, waits=waits))


def _listed(state, since, ack: str) -> bool:
    """summary.report's rule: a waiting email until it goes, any other until read after it began."""
    return state == "waiting" or (since or "") > ack


def test_every_state_a_send_record_can_hold_is_one_the_census_can_read():
    states = send_census.written_states()
    assert states and not [s for s in states if s.startswith("?")], states
    assert {"sending", "unconfirmed"} <= states


def test_a_send_record_reads_one_state_with_the_time_it_began_at_every_moment():
    """A written record always reads a state; one with a readable time began no later than now,
    and keeps the time it began while it reads the same."""
    from celerp_subscriptions import billing
    assert billing.send_state(None, _T0) == (None, None)
    seen: dict[tuple, tuple] = {}
    for record, emailed, waits, now, state, since in _readings():
        assert state is not None, record
        if since and record.get("at") == _T0.isoformat():
            assert datetime.fromisoformat(since) <= now, (record, now, since)
        key = (repr(record), emailed, waits, state)
        assert seen.setdefault(key, since) == since, (record, now, state, since)


def test_a_reading_that_changes_after_the_summary_was_read_is_listed_again():
    """Read at one moment and acknowledged then, a record that reads differently at a later
    moment is listed again, whatever its state, age, document and next pass (P2g-1)."""
    from celerp_subscriptions import billing
    for record, emailed, waits in itertools.product(_records(), (False, True), (False, True)):
        if record.get("at") != _T0.isoformat():
            continue
        for read_at, later in itertools.combinations(_moments(), 2):
            before = billing.send_state(record, read_at, emailed=emailed, waits=waits)
            after = billing.send_state(record, later, emailed=emailed, waits=waits)
            ack = read_at.isoformat()
            if after[0] != before[0]:
                assert _listed(*after, ack), (record, read_at, later, before, after)
            elif after[0] != "waiting":
                assert not _listed(*after, ack), (record, read_at, later, after)


def _statuses() -> set[str]:
    return {state for *_rest, state, _since in _readings()}


def test_every_status_a_send_can_read_has_a_summary_kind_a_period_mark_and_a_history_line():
    from celerp_subscriptions import billing, summary
    from ui.routes.subscriptions import _SEND_BADGE
    statuses = _statuses()
    assert {"sending", "unconfirmed", "sent"} <= statuses, statuses
    for status in statuses:
        kind = summary._SEND_KINDS[status]
        assert kind in summary.KINDS and f"subscriptions.summary_{kind}" in _EN, (status, kind)
        assert status == "sent" or _SEND_BADGE[status][0] in _EN, status
    for record in _records():
        assert f"subscriptions.history_send_{billing.send_written(record)}" in _EN, record


def test_only_a_cut_off_send_is_offered_as_a_new_message():
    from celerp_subscriptions import billing
    assert {s for s in _statuses() if billing.sends_again(s, {}, set())} == {"unconfirmed"}


def test_only_send_state_reads_a_send_records_stored_state_or_time():
    """The census finds send_state as the reader of a record's state and time, and no other."""
    assert [(w, f) for w, f, how, owner in send_census.readers() if owner == "owner" and "raw" in how]
    assert send_census.raw_outside_owner() == []
