# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company that used subscriptions on release 2.8.0 starts this release.

``fixtures/v280_subscriptions/templates.json`` is the 2.8.0 demo company with templates made
through 2.8.0's own API (``fixtures/v280_subscriptions/capture.py``), by name:
- ``custom_no_days``: Custom with the interval left at "--" (2.8.0 billed every 30 days),
  started 2026-09-01, billed 2026-10-01, next run 2026-10-31.
- ``custom_cleared``: Custom whose interval was cleared on the draft, activated 2026-10-01,
  next run 2026-10-31.
- ``custom_10``: Custom every 10 days, next run 2026-09-21.
- ``paused_now``: monthly from 2026-04-01, billed 2026-05-01, paused 2026-05-15 (next run
  2026-06-01).
- ``weekly_late``, ``month_end``, ``purchase``, ``clean_monthly``, ``never_generated``,
  ``paused_resumed``, ``cancelled``, ``draft``, ``custom_typed`` (a draft holding the interval
  as the text "14", which 2.8.0 could not activate).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import pre366
from company_today_support import freeze
from celerp.events.engine import emit_event
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "v280_subscriptions" / "templates.json"
_V280 = json.loads(FIXTURE.read_text())["companies"]["demo"]
_OLD = {name: next(p["state"] for p in _V280["projections"] if p["entity_id"] == eid)
        for name, eid in _V280["runs"].items()}


def _on(monkeypatch, day: str) -> None:
    freeze(monkeypatch, datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc))


async def _upgrade(session, monkeypatch, day: str, before_start=None) -> dict:
    """The 2.8.0 company after this release's first start on ``day``."""
    _on(monkeypatch, day)
    old = await pre366.load(session, "demo", FIXTURE)
    if before_start is not None:
        await before_start(session, old)
    await pre366.last_started_on_older_release(session, "2.8.0", semantics=2)
    await pre366.start()
    return old


async def _state(session, old, name) -> dict:
    session.expire_all()
    return (await session.get(Projection, {"company_id": old["company_id"], "entity_id": old["runs"][name]})).state


async def _notices(session, old) -> list[str]:
    from celerp.models.notification import Notification

    rows = (await session.execute(select(Notification).where(Notification.company_id == old["company_id"]))).scalars()
    return [f"{n.title}: {n.body}" for n in rows]


async def _page(monkeypatch, old, name) -> str:
    """The template's page as the UI renders it from the real API."""
    import httpx

    import celerp.main
    import ui.api_client as api
    from ui.app import app as ui_app

    def make(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = {**(headers or {}), **({"Authorization": f"Bearer {token}"} if token else {})}
        return httpx.AsyncClient(base_url="http://api", headers=merged, timeout=timeout,
                                 transport=httpx.ASGITransport(app=celerp.main.app), follow_redirects=follow_redirects)

    monkeypatch.setattr(api, "_local_client", make)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://localhost",
                                 cookies={"celerp_token": old["headers"]["Authorization"][7:]}) as c:
        r = await c.get(f"/subscriptions/{old['runs'][name]}")
    assert r.status_code == 200, r.status_code
    return r.text


def _url(old, name, action=""):
    return f"/subscriptions/{old['runs'][name]}{action}"


async def _generate(client, old, name):
    return await client.post(_url(old, name, "/generate-due"), headers=old["headers"],
                             json={"request_key": str(uuid.uuid4())})


async def _periods(client, old, name) -> dict:
    r = await client.get(_url(old, name, "/periods"), headers=old["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_every_template_keeps_the_next_date_it_had_on_2_8_0(client, session, monkeypatch):
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    for name, before in _OLD.items():
        state = await _state(session, old, name)
        if before.get("next_run_date") and before.get("status") in ("active", "paused"):
            assert (await _periods(client, old, name))["next_bill_date"] == before["next_run_date"], name
            assert "next_run_date" not in state, name
    assert not [n for n in await _notices(session, old) if "server log" in n]


async def test_custom_without_an_interval_bills_every_30_days_as_on_2_8_0(client, session, monkeypatch):
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    for name in ("custom_no_days", "custom_cleared"):
        assert (await _periods(client, old, name))["next_bill_date"] == "2026-10-31", name
        r = await _generate(client, old, name)
        assert r.status_code == 200, (name, r.text)
        # 2.8.0's next run after 2026-10-31: 30 days on.
        assert (await _periods(client, old, name))["next_bill_date"] == "2026-11-30", name
        assert (await _state(session, old, name))["custom_interval_days"] == 30, name


async def _unreadable_interval(session, old) -> None:
    # A template whose stored interval no release can read as days.
    await emit_event(session, company_id=old["company_id"], entity_id=old["runs"]["custom_no_days"],
                     entity_type="doc", event_type="doc.updated",
                     data={"fields_changed": {"custom_interval_days": {"new": "two weeks"}}},
                     actor_id=None, location_id=None, source="import", idempotency_key=str(uuid.uuid4()))
    await session.commit()


async def test_a_template_that_cannot_move_is_refused_by_name_and_says_why(client, session, monkeypatch):
    """Any template the move cannot read is refused with a 409 naming it and the reason, on
    every schedule action and on its page, never a 500; setting its interval moves it."""
    old = await _upgrade(session, monkeypatch, "2026-11-04", _unreadable_interval)
    ref = _OLD["custom_no_days"]["ref_id"]
    h = old["headers"]
    for path, body in (("/generate-due", {"request_key": str(uuid.uuid4())}),
                       ("/bill-early", {"request_key": str(uuid.uuid4())}),
                       ("/periods/0/skip", {"reason": "x"}), ("/periods/0/unskip", {}),
                       ("/periods/-1/reopen", {}), ("/pause", {})):
        r = await client.post(_url(old, "custom_no_days", path), headers=h, json=body)
        assert r.status_code == 409 and ref in r.text and "two weeks" in r.text, (path, r.status_code, r.text)
    r = await client.patch(_url(old, "custom_no_days", "/schedule"), headers=h, json={"cancel_at_period_end": True})
    assert r.status_code == 409 and ref in r.text, r.text
    periods = await _periods(client, old, "custom_no_days")
    assert "two weeks" in periods["not_moved"], periods
    assert "two weeks" in await _page(monkeypatch, old, "custom_no_days")
    notices = await _notices(session, old)
    assert any(ref in n and "two weeks" in n for n in notices), notices

    r = await client.patch(_url(old, "custom_no_days", "/schedule"), headers=h, json={"custom_interval_days": 30})
    assert r.status_code == 200 and r.json()["next_bill_date"] == "2026-10-31", r.text
    assert (await _generate(client, old, "custom_no_days")).status_code == 200
    assert (await _periods(client, old, "custom_no_days"))["next_bill_date"] == "2026-11-30"


async def test_a_template_paused_on_2_8_0_restarts_from_the_resume_day(client, session, monkeypatch):
    """2.8.0's Resume restarted the schedule from the resume day: the months it was paused
    are not billed. They are skipped with a reason, each can be un-skipped, and a pause made
    on this release keeps its missed periods due."""
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    h = old["headers"]
    assert "2026-06-01" in await _page(monkeypatch, old, "paused_now"), "no note by Resume naming the skip"
    r = await client.post(_url(old, "paused_now", "/resume"), headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    # June to November 2026: every period that started on or before the resume day.
    assert [p["service_start"] for p in body["skipped"]] == [
        "2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"], body
    assert body["next_bill_date"] == "2026-12-01", body
    r = await _generate(client, old, "paused_now")
    assert r.status_code in (200, 409) and not r.json().get("doc_id"), r.text
    rows = {row["service_start"]: row for row in (await _periods(client, old, "paused_now"))["rows"]}
    assert rows["2026-07-01"]["state"] == "skipped" and rows["2026-07-01"]["reason"], rows["2026-07-01"]
    k = rows["2026-11-01"]["period_index"]
    assert (await client.post(_url(old, "paused_now", f"/periods/{k}/unskip"), headers=h, json={})).status_code == 200
    assert (await _periods(client, old, "paused_now"))["next_bill_date"] == "2026-11-01"

    # Paused again on this release: what it misses stays due on resume.
    assert (await client.post(_url(old, "paused_now", "/pause"), headers=h, json={})).status_code == 200
    _on(monkeypatch, "2027-01-15")
    r = await client.post(_url(old, "paused_now", "/resume"), headers=h, json={})
    assert r.status_code == 200 and not r.json().get("skipped"), r.text
    assert r.json()["next_bill_date"] == "2026-11-01", r.text


async def test_the_upgrade_tells_once_which_templates_have_more_than_one_period_due(client, session, monkeypatch):
    """2.8.0 counted one overdue run; this release bills each missed period. The first start
    names every active template that comes out with more than one period due."""
    old = await _upgrade(session, monkeypatch, "2026-11-04")
    notices = [n for n in await _notices(session, old) if _OLD["weekly_late"]["ref_id"] in n]
    assert len(notices) == 1, await _notices(session, old)
    assert _OLD["month_end"]["ref_id"] in notices[0] and _OLD["clean_monthly"]["ref_id"] not in notices[0], notices
    await pre366.start()
    assert len([n for n in await _notices(session, old) if _OLD["weekly_late"]["ref_id"] in n]) == 1
