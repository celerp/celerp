# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Period decisions follow the company's business day, never the server's.

The demo company keeps its books in Los Angeles while the server's clock reads the next
day. At 05:00 UTC on 1 June it is still 31 May in the company: May has not ended, so a
manual run through May is refused, the background run prepares April only, and a
reminder due on 1 June is told as coming up, not as due. Three hours later, 1 June in Los
Angeles, each goes ahead. The tax year ending 30 June closes the same way.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime

import pytest

from fa_fixtures import FA
from test_runs import _contributed, _managed_from, _notices, _run
from test_tax_books import _setup
from test_year_end import _accept_all

TZ = "America/Los_Angeles"


class _Stop(Exception):
    pass


def _clock(monkeypatch, instant: str, server_day: str) -> None:
    """Now is ``instant`` (UTC) and the server's local calendar reads ``server_day``."""
    from celerp.services import business_time
    from celerp_fixed_assets import runs, yearend

    fixed = datetime.fromisoformat(instant)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    class _Server(date):
        @classmethod
        def today(cls):
            return date.fromisoformat(server_day)

    monkeypatch.setattr(business_time, "datetime", _Now)
    for module in (runs, yearend):
        monkeypatch.setattr(module, "date", _Server)


async def _in_los_angeles(fa: FA) -> None:
    await fa.ok(await fa.client.patch("/companies/me", headers=fa.h, json={"settings": {"timezone": TZ}}))


async def _loop_once(fa: FA) -> None:
    """One pass of the background loop, over this company only."""
    from celerp_fixed_assets import runs

    @asynccontextmanager
    async def this_session():
        yield fa.session

    async def stop(_seconds):
        raise _Stop

    real = runs.tick

    async def ours(session, company_id, *args, **kwargs):
        return await real(session, company_id, *args, **kwargs) if company_id == fa.auth["company_id"] else None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("celerp.db.get_session_ctx", this_session)
        mp.setattr(runs, "tick", ours)
        mp.setattr(runs.asyncio, "sleep", stop)
        with pytest.raises(_Stop):
            await runs.loop()


async def _ready(fa: FA) -> list[str]:
    return [n.body for n in await _notices(fa) if n.title == "Depreciation ready to post"]


async def test_f04_manual_run_waits_for_the_company_month_end(fa, monkeypatch):
    await _managed_from(fa)
    await _contributed(fa)
    await _run(fa, "2026-03")
    await _in_los_angeles(fa)
    body = {"month": "2026-05", "preview": True, "op_key": "may"}
    _clock(monkeypatch, "2026-06-01T05:00:00+00:00", "2026-06-01")
    early = await fa.post("/runs", body)
    assert early.status_code == 422 and early.json()["detail"] == {
        "message": "2026-05 has not ended yet. Depreciation runs for a month once it has ended, so the latest you "
                   "can run through is 2026-04.",
        "params": {"fill": {"field": "month", "value": "2026-04", "action": "fixed_assets.action.review_through"}}}, \
        early.text
    _clock(monkeypatch, "2026-06-01T08:00:00+00:00", "2026-06-01")
    assert (await fa.post("/runs", body)).status_code == 200


async def test_f04_background_run_and_reminders_use_the_company_day(fa, monkeypatch):
    await _managed_from(fa)
    car = await _contributed(fa)
    await _run(fa, "2026-03")
    reminder = await fa.ok(await fa.post(f"/assets/{car}/reminders", {
        "kind": "insurance", "insurer": "Mutual", "policy": "P-1", "due": "2026-06-01"}))
    await _in_los_angeles(fa)

    _clock(monkeypatch, "2026-06-01T05:00:00+00:00", "2026-06-01")
    await _loop_once(fa)
    assert await _ready(fa) == ["Depreciation for April 2026 is ready to post."]
    assert (await fa.state(reminder["id"]))["told"] == ["soon:2026-06-01"]

    _clock(monkeypatch, "2026-06-01T08:00:00+00:00", "2026-06-01")
    await _loop_once(fa)
    assert await _ready(fa) == ["Depreciation for May 2026 is ready to post."]
    assert (await fa.state(reminder["id"]))["told"] == ["soon:2026-06-01", "due:2026-06-01"]


async def test_f04_tax_year_closes_once_it_has_ended_in_the_company(fa, zz, monkeypatch):
    await _setup(fa)
    await _accept_all(fa, 2025)
    await fa.ok(await fa.post("/tax-years/zz_asset/2025/close", {}))
    await _accept_all(fa, 2026)
    await _in_los_angeles(fa)
    _clock(monkeypatch, "2026-07-01T05:00:00+00:00", "2026-07-01")
    early = await fa.post("/tax-years/zz_asset/2026/close", {})
    assert early.status_code == 409 and "cannot close yet" in early.text, early.text
    _clock(monkeypatch, "2026-07-01T08:00:00+00:00", "2026-07-01")
    closed = await fa.ok(await fa.post("/tax-years/zz_asset/2026/close", {}))
    assert closed["status"] == "closed"
