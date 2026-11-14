# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The collection transition table (collect.TABLE): every phase x event cell is defined, does what
its action says, and the phases and events are read as the table names them.

Initial conditions: no database. The invoice is a stand-in row whose collection the test sets;
writing its collection, opening an attempt and building the attempt request are stand-ins that
record what was asked of them, and the stop reason and Cloud's offer are whatever the cell's
event needs.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from celerp.services import collection
from celerp_subscriptions import collect

# The reference: what a pass does in each cell, written out independently of collect.TABLE.
_W = {"stop": "cancel", "reduced": "cancel_replace", "raised": "wait", "off": "wait", "none": "wait"}
EXPECTED = {
    "new": {"stop": "stop", "reduced": "reopen", "raised": "reopen", "off": "stop", "none": "ask"},
    "asked": {"stop": "cancel", "reduced": "cancel_replace", "raised": "ask", "off": "ask", "none": "ask"},
    "submitted": _W, "processing": _W, "requires_action": _W, "unknown": _W,
    "cancel_queued": dict.fromkeys(collect.EVENTS, "send_cancel"),
    "cancel_refused": dict.fromkeys(collect.EVENTS, "wait"),
    "failed": {**dict.fromkeys(collect.EVENTS, "next_when_due"), "off": "wait"},
    "replace": {**dict.fromkeys(collect.EVENTS, "next"), "off": "wait"},
    "succeeded": dict.fromkeys(collect.EVENTS, "wait"),
    "ended": {**dict.fromkeys(collect.EVENTS, "tell"), "stop": "stop"},
    "settled": dict.fromkeys(collect.EVENTS, "wait"),
}

COLLECTION = {
    "new": {"state": "pending", "asked": False},
    "asked": {"state": "pending", "asked": True},
    **{s: {"state": s} for s in ("submitted", "processing", "requires_action", "unknown")},
    "cancel_queued": {"state": "submitted", "cancel": "queued", "reason": "invoice_settled"},
    "cancel_refused": {"state": "processing", "cancel": "refused"},
    "failed": {"state": "failed", "retry_eligible_at": "2026-07-01T00:00:00+00:00"},
    "replace": {"state": "canceled", "replace": True},
    "succeeded": {"state": "succeeded"},
    "ended": {"state": "paid"},
    "settled": {"state": "unverifiable"},
}
WHY = {"stop": "invoice_settled", "reduced": "amount_changed", "raised": None, "off": "collection_off", "none": None}


def test_every_cell_is_defined_and_names_an_action():
    assert set(collect.TABLE) == {(p, e) for p in collect.PHASES for e in collect.EVENTS}
    assert set(collect.TABLE.values()) <= set(collect._ACTIONS)
    assert set(EXPECTED) == set(collect.PHASES)


class _Pass:
    """The stand-ins one cell runs against; ``writes`` holds each collection change, ``opened``
    each attempt number opened."""

    def __init__(self, monkeypatch, current: dict):
        self.row = SimpleNamespace(entity_id="doc:INV", state={
            "collection": {"attempt_number": 1, "amount_minor": 3000, **current}})
        self.writes: list[dict] = []
        self.opened: list[int] = []
        self.told: list[str] = []

        async def set_attempt(session, company_id, row, change, *, drop=()):
            self.writes.append(change)
            row.state["collection"] = {**{k: v for k, v in row.state["collection"].items() if k not in drop}, **change}

        async def open_attempt(session, company_id, row, n):
            self.opened.append(n)
            row.state["collection"] = {"state": "pending", "asked": False, "attempt_number": n, "amount_minor": 1355}

        async def attempt_request(session, company_id, row, current):
            return "attempt", {"attempt_number": current["attempt_number"]}, current.get("asked") is True

        async def locked(session, company_id, doc_id):
            return self.row

        async def event(session, company_id, row, current, phase):
            return "none", None  # what the pass finds once the cell's action made the next attempt

        async def notify_once(session, company_id, category, title, body, **kw):
            self.told.append(title)

        from celerp.notifications import service
        monkeypatch.setattr(service, "notify_once", notify_once)
        monkeypatch.setattr(collection, "outstanding_minor", lambda state: 1000)
        monkeypatch.setattr(collection, "set_attempt", set_attempt)
        monkeypatch.setattr(collection, "open_attempt", open_attempt)
        monkeypatch.setattr(collect, "_attempt_request", attempt_request)
        monkeypatch.setattr(collect, "_locked", locked)
        monkeypatch.setattr(collect, "_event", event)


def _effect(action: str, event: str, n: int) -> tuple:
    """What each action leaves: (request kind or None, collection changes, attempts opened)."""
    asks = ("attempt", [], [])
    return {
        "wait": (None, [], []),
        "ask": asks,
        "stop": (None, [{"state": "stopped", "reason": WHY[event]}], []),
        "reopen": ("attempt", [], [n]),
        "cancel": ("cancel", [{"cancel": "queued", "reason": WHY[event]}], []),
        "cancel_replace": ("cancel", [{"cancel": "queued", "reason": WHY[event], "replace": True}], []),
        "send_cancel": ("cancel", [], []),
        "tell": (None, [{"state": "stopped", "reason": "owes_again"}], []),
        "next": (None, [{"state": "stopped", "reason": WHY[event]}], []) if event == "stop" else (
            "attempt", [], [n + 1]),
    }[action]


@pytest.mark.parametrize("phase", collect.PHASES)
@pytest.mark.parametrize("event", collect.EVENTS)
@pytest.mark.asyncio
async def test_each_cell_does_what_its_action_says(monkeypatch, phase, event):
    from celerp_subscriptions import billing
    from datetime import datetime, timezone
    monkeypatch.setattr(billing, "now", lambda: datetime(2026, 7, 2, tzinfo=timezone.utc))  # the retry is due
    run = _Pass(monkeypatch, COLLECTION[phase])
    assert collect.phase_of(run.row.state["collection"]) == phase
    action = collect.TABLE[phase, event]
    assert action == EXPECTED[phase][event]
    step = await collect._ACTIONS[action](None, "cid", run.row, dict(run.row.state["collection"]), event, WHY[event])
    expected = _effect("next" if action == "next_when_due" else action, event, 1)
    assert ((step or (None,))[0], run.writes, run.opened) == expected
    assert len(run.told) == (action == "tell")


@pytest.mark.asyncio
async def test_a_failed_attempt_before_its_retry_time_waits(monkeypatch):
    from celerp_subscriptions import billing
    from datetime import datetime, timezone
    monkeypatch.setattr(billing, "now", lambda: datetime(2026, 6, 30, tzinfo=timezone.utc))
    run = _Pass(monkeypatch, COLLECTION["failed"])
    assert await collect._next_when_due(None, "cid", run.row, run.row.state["collection"], "none", None) is None
    assert (run.writes, run.opened) == ([], [])


@pytest.mark.parametrize("state", sorted({*collection.CLOUD_STATES, "pending", "paid", "stopped"}))
@pytest.mark.parametrize("cancel", [None, "queued", "refused"])
@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("reason", [None, "invoice_settled", "mode_manual"])
def test_every_stored_collection_reads_as_one_phase(state, cancel, flag, reason):
    """``flag`` is ``asked`` on a pending attempt and ``replace`` on a cancelled one."""
    current = {"state": state, "cancel": cancel, "asked": flag, "replace": flag, "reason": reason}
    if state in collection.IN_FLIGHT | {"failed"} and cancel == "queued":
        expected = "cancel_queued"
    elif state in collection.IN_FLIGHT and cancel == "refused":
        expected = "cancel_refused"
    elif state == "pending":
        expected = "asked" if flag else "new"
    elif state in collection.IN_FLIGHT or state in ("failed", "succeeded"):
        expected = state
    elif state == "canceled" and flag:
        expected = "replace"
    elif state == "paid" or (state in ("stopped", "canceled") and reason == "invoice_settled"):
        expected = "ended"
    else:
        expected = "settled"
    assert collect.phase_of(current) == expected


@pytest.mark.parametrize("found, phase, expected", [
    ({"stop": "invoice_settled", "owed": 1000, "offered": False}, "submitted", ("stop", "invoice_settled")),
    ({"owed": 1355, "offered": False}, "submitted", ("reduced", "amount_changed")),
    ({"owed": 4000, "offered": False}, "asked", ("raised", None)),
    ({"owed": 1355, "offered": False}, "replace", ("off", "collection_off")),
    ({"owed": 1355, "offered": True}, "failed", ("none", None)),
    ({"owed": 3000, "offered": False}, "new", ("off", "collection_off")),
    ({"owed": 3000, "offered": True}, "new", ("none", None)),
])
@pytest.mark.asyncio
async def test_the_event_is_the_first_that_holds(monkeypatch, found, phase, expected):
    """Stop, then a balance that differs from what a live attempt asks (a failed or cancelled one
    asks nothing), then Cloud's offer."""
    async def stop_reason(session, company_id, state):
        return found.get("stop")

    monkeypatch.setattr(collect, "stop_reason", stop_reason)
    monkeypatch.setattr(collection, "outstanding_minor", lambda state: found["owed"])
    monkeypatch.setattr(collection, "offered", lambda: found["offered"])
    row = SimpleNamespace(entity_id="doc:INV", state={})
    assert await collect._event(None, "cid", row, {"amount_minor": 3000}, phase) == expected
