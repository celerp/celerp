# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Accounting > Revenue recognition, the page: the status block, each ended month's runs with
Post or Reverse, a locked month offered its first open day as an explicit choice, automatic
posting turned on and off, and the deferred revenue roll-forward.

The API is patched: these tests read what the page draws and what it sends. The API's own
behavior is tested in test_revenue_recognition.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from test_income_accounts import _ui_api, ui_client  # noqa: F401 (fixture)
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

SID = "rev:doc-1:line-1"
_RUNS = {
    "runs": [
        {"schedule_id": SID, "segment": 0, "month": "2026-02", "amount": 280.0, "status": "outstanding", "je_id": None},
        {"schedule_id": SID, "segment": 0, "month": "2026-01", "amount": 310.0, "status": "posted",
         "je_id": f"je:auto:{SID}:0:2026-01:1"},
    ],
    "schedules": 1,
    "documents": {SID: {"doc_id": "doc-1", "ref": "INV-0042", "line": 1}},
    "summary": {"outstanding": 1, "months": ["2026-02"], "lapsed": None, "message": None},
    "enabled": False,
}
_ROLL = {"opening": 0.0, "deferred": 900.0, "recognized": 310.0, "adjusted": 0.0, "closing": 590.0,
         "liability": 590.0, "outstanding": [], "void_source": []}
_SETTING = {"enabled": False, "by": None, "at": None, "lapsed": None}


def _api(runs=None, roll=None, **more):
    return _ui_api(get_recognition=AsyncMock(return_value=runs or _RUNS),
                   get_recognition_roll_forward=AsyncMock(return_value=roll or _ROLL),
                   get_recognition_setting=AsyncMock(return_value=_SETTING), **more)


def _locked() -> APIError:
    refusal = {"message": "The books are locked through 2026-02-28.", "message_key": "recognition.err_month_locked",
               "params": {"lock_date": "2026-02-28", "first_open": "2026-03-01"}}
    return APIError(409, refusal["message"], refusal)


async def test_recognition_empty_state(ui_client):
    """Accounting > Revenue recognition with no schedule shows the empty state and how to start."""
    empty = {"runs": [], "schedules": 0, "documents": {},
             "summary": {"outstanding": 0, "months": [], "lapsed": None, "message": None}, "enabled": False}
    with _api(runs=empty):
        r = await ui_client.get("/accounting/recognition")
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "No revenue is deferred. Set an item or agreement to recognize over its service period to start." in r.text


async def test_recognition_page_lists_runs_by_month(ui_client):
    """Each ended month heads its runs, newest first: the document and line each run came from,
    its amount and status, Post on a month with outstanding runs and Reverse on one posted. The
    status block says which months are outstanding; the roll-forward agrees with the account."""
    with _api():
        r = await ui_client.get("/accounting/recognition")
    assert r.status_code == 200, r.text
    text = r.text
    assert text.index("2026-02") < text.index("2026-01")
    assert "INV-0042" in text and 'href="/docs/doc-1"' in text
    assert 'hx-post="/accounting/recognition/2026-02/post"' in text
    assert 'hx-post="/accounting/recognition/2026-01/reverse"' in text
    assert 'hx-post="/accounting/recognition/2026-02/reverse"' not in text
    assert "Outstanding runs: 1, in 2026-02." in text
    assert "Deferred revenue roll-forward" in text and "590.00" in text
    assert "Agrees with the deferred revenue accounts." in text


async def test_recognition_page_shows_lapse(ui_client):
    """A lapsed authorization heads the page with the summary's message."""
    lapsed = {**_RUNS, "enabled": True, "summary": {**_RUNS["summary"], "lapsed": {"by": "u", "at": "2026-02-01"},
                                                    "message": "Automatic recognition stopped: authorization lapsed"}}
    with _api(runs=lapsed):
        r = await ui_client.get("/accounting/recognition")
    assert "Automatic recognition stopped: authorization lapsed" in r.text


async def test_recognition_post_locked_month_offers_first_open_day(ui_client):
    """Post on a locked month is refused with the lock date, and the redrawn runs offer
    "Post dated <first open day>"; that choice sends the day as post_on."""
    refused = AsyncMock(side_effect=_locked())
    with _api(post_recognition_month=refused):
        r = await ui_client.post("/accounting/recognition/2026-02/post", data={})
    assert r.status_code == 200, r.text
    refused.assert_awaited_once()
    assert refused.await_args.args[1:] == ("2026-02", None)
    assert "locked through 2026-02-28" in r.text
    assert "Post dated 2026-03-01" in r.text and 'value="2026-03-01"' in r.text

    posted = AsyncMock(return_value={"runs": []})
    with _api(post_recognition_month=posted):
        r = await ui_client.post("/accounting/recognition/2026-02/post", data={"on": "2026-03-01"})
    assert posted.await_args.args[1:] == ("2026-02", "2026-03-01")
    assert "Runs of 2026-02 posted." in r.text


async def test_recognition_reverse_month(ui_client):
    """Reverse sends the month and says the runs are outstanding again; a refusal is said where it happened."""
    reversed_ = AsyncMock(return_value={"runs": []})
    with _api(reverse_recognition_month=reversed_):
        r = await ui_client.post("/accounting/recognition/2026-01/reverse", data={})
    assert reversed_.await_args.args[1:] == ("2026-01", None)
    assert "Runs of 2026-01 reversed. They are outstanding again." in r.text

    refusal = {"message": "No recognition run of 2026-01 is posted, so there is nothing to reverse.",
               "message_key": "recognition.err_run_not_posted", "params": {"month": "2026-01"}}
    with _api(reverse_recognition_month=AsyncMock(side_effect=APIError(409, refusal["message"], refusal))):
        r = await ui_client.post("/accounting/recognition/2026-01/reverse", data={})
    assert "nothing to reverse" in r.text


async def test_recognition_automatic_posting_toggle(ui_client):
    """The status block turns automatic posting on and off, here and in Settings > Accounting."""
    with _api():
        r = await ui_client.get("/accounting/recognition")
    assert "Automatic posting is off." in r.text and "Post recognition automatically" in r.text

    turned = AsyncMock(return_value={"enabled": True})
    with _api(set_recognition_setting=turned):
        r = await ui_client.post("/accounting/recognition/setting", data={"enabled": "1"})
    assert turned.await_args.args[1:] == (True,)
    assert "Automatic posting is on." in r.text or "Automatic posting turned on." in r.text

    with _api():
        r = await ui_client.get("/settings/accounting?tab=recognition")
    assert r.status_code == 200 and "Post recognition automatically" in r.text


async def test_recognition_roll_forward_lists_void_sources(ui_client):
    """A schedule whose source document is void is listed with its posted runs and Reverse on each month;
    a closing that differs from the account says by how much."""
    roll = {**_ROLL, "closing": 590.0, "liability": 600.0,
            "void_source": [{"schedule_id": SID, "doc_id": "doc-1",
                             "posted": [_RUNS["runs"][1]]}]}
    with _api(roll=roll):
        r = await ui_client.get("/accounting/recognition?from=2026-01-01&to=2026-03-31")
    text = r.text
    assert "Source document void" in text and 'hx-post="/accounting/recognition/2026-01/reverse"' in text
    assert "Differs from the deferred revenue accounts by 10.00." in text
