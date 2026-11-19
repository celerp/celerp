# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Asking the client about bank lines under one request key, on real PostgreSQL connections
against committed data in a database of the test's own.

The key is claimed in the Ask's own transaction before any question or notice is written, so
another request under a claimed key is refused before it writes anything. The same Ask sent twice
at once and its email lifecycle are covered in test_bank_questions_races_pg.py."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import bank_statement_helpers as B
from celerp.services.email import EmailResult
from race_support import _until_waiting_or_done
from test_bank_complete_race_pg import own_client  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _setup(c, engine):
    from test_helpers import grant_permission, invite_user

    co = await B.company(c)
    bk = await B.bank(c, co["h"])
    rows = [("2026-01-05", "WITHDRAWAL", "-200.00"), ("2026-01-06", "TRANSFER", "-50.00")]
    closing = f"{sum(Decimal(a) for _, _, a in rows):.2f}"
    await B.statement(c, co, bk["id"], rows, opening="0.00", closing=closing, start="2026-01-01", end="2026-01-31")
    await grant_permission(c, co["h"], "answer_bank_questions", "viewer")
    async with AsyncSession(engine) as s:
        await invite_user(c, s, co["h"], f"viewer-{uuid.uuid4().hex[:8]}@example.com", "viewer")
    lines = {l["description"]: l["id"] for l in B.all_lines(await B.groups(c, co, bk["id"]))}
    return co, bk, lines


def _ask(c, co, bk, line_ids, key):
    return c.post(f"{B.API}/bank/{bk['id']}/ask", headers=co["h"],
                  json={"line_ids": line_ids, "note": "What was this for?", "idempotency_key": key})


def _sent(monkeypatch):
    calls = []

    async def fake(to, subject, body_html, body_text="", **kw):
        calls.append(kw["operation_id"])
        return EmailResult(True, "sent")
    monkeypatch.setattr("celerp.services.email.send_email", fake)
    return calls


def _held_notice(monkeypatch, entered: asyncio.Event, go: asyncio.Event) -> None:
    """The first Ask stops at its first notice, its questions written and not committed."""
    from celerp.notifications import service

    real = service.notify_once

    async def held(*a, **kw):
        if not entered.is_set():
            entered.set()
            await go.wait()
        return await real(*a, **kw)
    monkeypatch.setattr(service, "notify_once", held)


async def _state(engine, co) -> tuple[dict, int]:
    from celerp.models.notification import Notification
    from celerp_accounting.models import BankStatementLine

    cid = uuid.UUID(co["company_id"])
    async with AsyncSession(engine) as s:
        asked = {l.description: (l.client_query or {}).get("asks") for l in (await s.execute(
            select(BankStatementLine).where(BankStatementLine.company_id == cid))).scalars()}
        notices = (await s.execute(select(func.count()).select_from(Notification).where(
            Notification.company_id == cid, Notification.category == "bank_questions"))).scalar_one()
    return asked, notices


async def _race(engine, c, co, bk, monkeypatch, first_ids, second_ids, key):
    entered, go = asyncio.Event(), asyncio.Event()
    _held_notice(monkeypatch, entered, go)
    first = asyncio.create_task(_ask(c, co, bk, first_ids, key))
    await asyncio.wait_for(entered.wait(), timeout=30)
    second = asyncio.create_task(_ask(c, co, bk, second_ids, key))
    await _until_waiting_or_done(engine, second)
    go.set()
    return await asyncio.wait_for(asyncio.gather(first, second), timeout=30)


async def test_another_request_under_a_key_being_claimed_writes_nothing(committed_engine, own_client, monkeypatch):
    calls = _sent(monkeypatch)
    co, bk, lines = await _setup(own_client, committed_engine)
    a, b = await _race(committed_engine, own_client, co, bk, monkeypatch, [lines["WITHDRAWAL"]],
                       [lines["TRANSFER"]], B.key())
    assert a.status_code == 200, a.text
    assert b.status_code == 409 and b.json()["detail"]["message_key"] == "request.key_reused", b.text
    asked, notices = await _state(committed_engine, co)
    assert asked == {"WITHDRAWAL": 1, "TRANSFER": None}
    assert notices == 1 and len(calls) == 1

