# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A period's email reads one way everywhere, over every reader and every stored state
send_census finds in the code.

Every state the code can store in a send record, fresh or old, with its document emailed or
not and with the next pass due to send it or not, goes through send_status, and each status it
answers has a summary kind, a mark on its period and a History line. Only a cut-off send is
offered as Send again. Every reader of the stored state outside History asks in_flight before
it treats a send as cut off, so a send still with the mail server is never sent twice. A new
reader or a new stored state turns these red without anyone editing a list."""
from __future__ import annotations

import ast
import itertools
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_census  # noqa: E402

_EN = json.loads((send_census.ROOT / "ui/locales/en.json").read_text(encoding="utf-8"))


def _statuses() -> set[str]:
    """Every answer send_status gives over every stored state the census finds."""
    from celerp_subscriptions import billing
    now = datetime.now(timezone.utc)
    ats = (None, now.isoformat(), (now - billing.SEND_TIMEOUT - timedelta(minutes=1)).isoformat())
    agreements = ({"status": "active", "generation_mode": "auto_invoice", "auto_send": True},
                  {"status": "active", "generation_mode": "auto_invoice", "auto_send": False})
    docs = (None, {"status": "final"}, {"status": "sent", "sent_via": "email"})
    found = set()
    for state, at, stored, doc in itertools.product(send_census.written_states(), ats, agreements, docs):
        send = {"state": state, **({"at": at} if at else {})}
        found.add(billing.send_status({**stored, "periods": {"0": {"state": billing.BILLED, "send": send}}},
                                      0, date.today(), doc))
    return found


def test_every_state_a_send_record_can_hold_is_one_the_census_can_read():
    states = send_census.written_states()
    assert states and not [s for s in states if s.startswith("?")], states
    assert {"sending", "unconfirmed"} <= states


def test_every_status_a_send_can_read_has_a_summary_kind_a_period_mark_and_a_history_line():
    from celerp_subscriptions import summary
    from ui.routes.subscriptions import _SEND_BADGE
    statuses = _statuses()
    assert {"sending", "unconfirmed", "sent"} <= statuses, statuses
    for status in statuses:
        kind = summary._SEND_KINDS[status]
        assert kind in summary.KINDS and f"subscriptions.summary_{kind}" in _EN, (status, kind)
        assert status == "sent" or _SEND_BADGE[status][0] in _EN, status
    for state in send_census.written_states():
        assert f"subscriptions.history_send_{state}" in _EN, state


def test_only_a_cut_off_send_is_offered_as_a_new_message():
    from celerp_subscriptions import billing
    assert {s for s in _statuses() if billing.sends_again(s, {}, set())} == {"unconfirmed"}


def test_no_reader_outside_billing_and_history_reads_a_stored_send_state():
    assert send_census.raw_outside_owner() == []


def test_every_reader_of_a_stored_send_asks_whether_it_is_still_in_flight():
    """A reader of the stored state that can take "sending" for a cut-off send (it names
    "sending" or UNCONFIRMED) asks in_flight first; History describes what an event recorded,
    not how the email reads now, so it is not one of them."""
    open_ = []
    for where, fn_name, how, owner in send_census.readers():
        if "raw" not in how.split(",") or owner == "history" or fn_name == "in_flight":
            continue
        path, line = where.rsplit(":", 1)
        tree = ast.parse((send_census.ROOT / path).read_text(encoding="utf-8"))
        fn = next(f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and f.lineno == int(line))
        nodes = list(ast.walk(fn))
        names_sending = any((isinstance(n, ast.Constant) and n.value == "sending")
                            or (isinstance(n, ast.Name) and n.id == "UNCONFIRMED") for n in nodes)
        asks = any(isinstance(n, ast.Call) and send_census._callee(n) == "in_flight" for n in nodes)
        if names_sending and not asks:
            open_.append(where)
    assert open_ == []
