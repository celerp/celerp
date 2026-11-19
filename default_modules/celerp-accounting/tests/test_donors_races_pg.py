# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Sending donor statements against other writers of the same company, on real PostgreSQL
connections against committed data in a database of the test's own.

Each donor's send records its attempt and commits before the email goes out, so no writer of
the company waits on the transport, and the same send pressed twice while the first is still
sending answers the stored attempt instead of emailing the donor again."""

from __future__ import annotations

import asyncio
import time

import pytest

import bank_statement_helpers as B
from income_books import PERIOD, _statement_donors
from test_bank_complete_race_pg import own_client  # noqa: F401
from test_bank_questions_races_pg import SLOW_S, _slow_transport

pytestmark = pytest.mark.asyncio


async def _setup(c) -> tuple[dict, dict]:
    co = await B.company(c)
    return co, await _statement_donors(c, {"headers": co["h"]})


def _send(c, co):
    return c.post("/donors/statements/send", headers=co["h"], json={**PERIOD, "confirm": True})


def _mine(r, donor: str) -> dict:
    assert r.status_code == 200, r.text
    return {row["contact_id"]: row for row in r.json()["results"]}[donor]


async def test_a_slow_statement_email_never_blocks_a_write_to_the_company(committed_engine, own_client, monkeypatch):
    c = own_client
    co, donors = await _setup(c)
    entered, calls = asyncio.Event(), []
    _slow_transport(monkeypatch, entered, calls)
    sending = asyncio.create_task(_send(c, co))
    await asyncio.wait_for(entered.wait(), 20)
    began = time.monotonic()
    other = await B.bank(c, co["h"], name="Other Bank")
    waited = time.monotonic() - began
    assert other["id"]
    assert waited < SLOW_S - 1, f"the write waited {waited:.1f}s on the email"
    assert not sending.done()
    assert _mine(await asyncio.wait_for(sending, 30), donors["email"])["state"] == "sent"


async def test_the_same_statement_send_pressed_twice_at_once_emails_once(committed_engine, own_client, monkeypatch):
    c = own_client
    co, donors = await _setup(c)
    entered, calls = asyncio.Event(), []
    _slow_transport(monkeypatch, entered, calls)
    first = asyncio.create_task(_send(c, co))
    await asyncio.wait_for(entered.wait(), 20)
    second = _mine(await _send(c, co), donors["email"])
    # Still with the first run's transport: it may have gone out, so it is not sent again.
    assert second["state"] == "unconfirmed" and second["repeat"] is True, second
    done = _mine(await asyncio.wait_for(first, 30), donors["email"])
    assert done["state"] == "sent" and len(calls) == 1
    again = _mine(await _send(c, co), donors["email"])
    assert again == {**done, "repeat": True} and len(calls) == 1
