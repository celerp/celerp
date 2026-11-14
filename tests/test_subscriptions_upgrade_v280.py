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
from celerp.models.ledger import LedgerEntry
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


async def _page(monkeypatch, old, name, path: str | None = None) -> str:
    """The template's page (or the UI page at ``path``) as the UI renders it from the real API."""
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
        r = await c.get(path or f"/subscriptions/{old['runs'][name]}")
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


async def _set(session, old, name, **fields) -> None:
    # A 2.8.0 template edited the way 2.8.0's generic field save stored it.
    await _set_doc(session, old, old["runs"][name], **fields)


async def _set_doc(session, old, eid, **fields) -> None:
    # A 2.8.0 document edited the way 2.8.0's generic field save stored it: its event and
    # projection as 2.8.0 wrote them, with none of this release's checks on the way in. The
    # event is what a projection rebuild on upgrade replays, so the edit survives it.
    cid = old["company_id"]
    proj = await session.get(Projection, {"company_id": cid, "entity_id": eid})
    proj.state, proj.version = {**proj.state, **fields}, proj.version + 1
    await session.flush()  # this release's lock order: the projection row, then the ledger
    session.add(LedgerEntry(company_id=cid, entity_id=eid, entity_type="doc", event_type="doc.updated",
                            data={"fields_changed": {f: {"new": v} for f, v in fields.items()}},
                            actor_id=None, location_id=None, source="import",
                            idempotency_key=str(uuid.uuid4())))
    await session.commit()


async def _unreadable_interval(session, old) -> None:
    # A template whose stored interval no release can read as days.
    await _set(session, old, "custom_no_days", custom_interval_days="two weeks")


async def test_a_template_that_cannot_move_is_refused_by_name_and_says_why(client, session, monkeypatch):
    """Any template the move cannot read is refused with a 409 naming it and the reason, on
    every schedule action and on its page, never a 500; setting its interval moves it."""
    old = await _upgrade(session, monkeypatch, "2026-11-04", _unreadable_interval)
    ref = _OLD["custom_no_days"]["ref_id"]
    h = old["headers"]
    for path, body in (("/generate-due", {"request_key": str(uuid.uuid4())}),
                       ("/bill-early", {"request_key": str(uuid.uuid4())}),
                       ("/periods/0/skip", {"reason": "x"}), ("/periods/0/unskip", {}),
                       ("/periods/-1/reopen", {})):
        r = await client.post(_url(old, "custom_no_days", path), headers=h, json=body)
        assert r.status_code == 409 and ref in r.text and "two weeks" in r.text, (path, r.status_code, r.text)
    # Pause and cancel need no schedule, so they stay open; anything else on the schedule waits.
    r = await client.patch(_url(old, "custom_no_days", "/schedule"), headers=h, json={"cancel_at_period_end": True})
    assert r.status_code == 409 and ref in r.text, r.text
    periods = await _periods(client, old, "custom_no_days")
    assert "two weeks" in periods["not_moved"], periods
    page = await _page(monkeypatch, old, "custom_no_days")
    assert "two weeks" in page and "/schedule/custom_interval_days" in page and "/generate-due" not in page
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
    from ui.i18n import t

    assert t("subscriptions.resume_restarts_note") in await _page(monkeypatch, old, "paused_now")
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




# Every way 2.8.0 templates come into this release, by the lifecycle slot that moves them.
_WAYS_IN = {"startup": "on_modules_ready", "restore": "on_company_restored",
            "module_enable": "on_company_module_enabled"}


@pytest.mark.parametrize("way", list(_WAYS_IN))
async def test_every_way_in_moves_what_it_can_and_names_what_it_cannot(client, session, monkeypatch, way):
    """Starting this release, restoring a 2.8.0 backup and turning Subscriptions on all move
    the company the same way: every template that can move does, and one that cannot is
    refused by name and why, named in a notice, and fixable from its page."""
    from celerp.modules.slots import fire_lifecycle

    _on(monkeypatch, "2026-11-04")
    old = await pre366.load(session, "demo", FIXTURE)
    await _unreadable_interval(session, old)
    if way == "startup":
        await pre366.last_started_on_older_release(session, "2.8.0", semantics=2)
        await pre366.start()
    else:
        assert not await fire_lifecycle(_WAYS_IN[way], session=session, company_id=old["company_id"])
        await session.commit()
    h, ref = old["headers"], _OLD["custom_no_days"]["ref_id"]
    for name, before in _OLD.items():
        if name != "custom_no_days" and before.get("next_run_date"):
            assert (await _state(session, old, name))["anchor_date"] == before["next_run_date"], (way, name)
    r = await _generate(client, old, "custom_no_days")
    assert r.status_code == 409 and ref in r.text and "two weeks" in r.text, (way, r.status_code, r.text)
    assert any(ref in n and "two weeks" in n for n in await _notices(session, old)), way
    r = await client.patch(_url(old, "custom_no_days", "/schedule"), headers=h, json={"custom_interval_days": 30})
    assert r.status_code == 200 and r.json()["next_bill_date"] == "2026-10-31", (way, r.text)


async def test_only_the_one_mover_moves_templates_and_only_the_one_reader_reads_2_8_0_fields():
    """Each primitive's callers, by name: a new path that moves templates, reconciles them or
    reads a template not yet on a schedule without the primitive fails here first."""
    import ast
    from importlib import import_module

    pkg = Path(import_module("celerp_subscriptions").__file__).parent
    callers: dict[str, set[str]] = {}

    def visit(node, fn=None):
        # Each call is credited to the innermost function it is written in.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call):
            f = node.func
            callers.setdefault(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None), set()).add(fn)
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    for path in pkg.glob("*.py"):
        visit(ast.parse(path.read_text()))
    assert callers["_migrate_template"] == {"migrate_company", "move_template"}
    assert callers["migrate_company"] == {"migrate_schedules_hook", "reconcile_company_schedules"}
    assert callers["move_template"] == {"_move_with"}
    assert callers["legacy_schedule"] == {"_migrate_template", "not_moved", "_first_schedule"}
    assert callers["legacy_terms"] == {"legacy_schedule", "unscheduled"}
    assert callers["_first_schedule"] == {"activate_subscription", "change_schedule"}
    # Every lifecycle slot that reaches the mover is one test_every_way_in_moves_... runs.
    from celerp.modules import loader

    slots = loader.read_manifest(loader.resolve_runtime_module_path(
        "celerp-subscriptions", loader.module_search_path()))["slots"]
    movers = {s for s, entries in slots.items() if isinstance(entries, list)
              for e in entries if str(e.get("handler")).startswith("celerp_subscriptions.migration:")}
    assert movers == set(_WAYS_IN.values()), movers


async def test_a_2_8_0_draft_activates_with_the_interval_it_holds(client, session, monkeypatch):
    """2.8.0's page stored a typed interval as text: the draft holding "14" activates every 14
    days; with the interval emptied, activation names the interval, not the frequency."""
    from ui.i18n import t

    async def interval_emptied(session, old):
        await _set(session, old, "draft", frequency="custom", custom_interval_days="")

    old = await _upgrade(session, monkeypatch, "2026-11-04", before_start=interval_emptied)
    r = await client.post(_url(old, "custom_typed", "/activate"), headers=old["headers"], json={})
    assert r.status_code == 200, r.text
    state = await _state(session, old, "custom_typed")
    assert (state["anchor_date"], state["custom_interval_days"]) == ("2026-10-01", 14), state
    starts = [row["service_start"] for row in (await _periods(client, old, "custom_typed"))["rows"]]
    assert "2026-10-15" in starts and "2026-10-29" in starts, starts

    r = await client.post(_url(old, "draft", "/activate"), headers=old["headers"], json={})
    assert r.status_code == 422 and r.json()["detail"]["message"] == t("subscriptions.err_custom_interval"), r.text


async def test_fixing_a_template_from_its_page_sends_a_shared_document_to_history_review(
        client, session, monkeypatch):
    """Two 2.8.0 templates that both name one document: when one is fixed from its page, that
    document waits for history review instead of being placed as billed in both."""
    async def two_unreadable_sharing_a_document(session, old):
        await _unreadable_interval(session, old)
        shared = _OLD["custom_no_days"]["generated_doc_ids"][0]
        await _set(session, old, "custom_10", custom_interval_days="ten days",
                   generated_doc_ids=[*_OLD["custom_10"]["generated_doc_ids"], shared])

    old = await _upgrade(session, monkeypatch, "2026-11-04", two_unreadable_sharing_a_document)
    shared = _OLD["custom_no_days"]["generated_doc_ids"][0]
    r = await client.patch(_url(old, "custom_no_days", "/schedule"), headers=old["headers"],
                           json={"custom_interval_days": 30})
    assert r.status_code == 200, r.text
    state = await _state(session, old, "custom_no_days")
    assert [e["doc_id"] for e in state.get("history_review") or []] == [shared], state
    assert not [e for e in state["periods"].values() if e.get("doc_id") == shared], state["periods"]


async def test_a_2_8_0_draft_starts_on_its_start_date_whatever_next_run_date_it_holds(client, session, monkeypatch):
    """2.8.0's activate set a draft's first run from its start date and never read a next run
    date the draft held, so the move anchors the draft on its start date too."""
    async def stale_next_run(session, old):
        await _set(session, old, "draft", next_run_date="2027-03-01")

    old = await _upgrade(session, monkeypatch, "2026-11-04", before_start=stale_next_run)
    state = await _state(session, old, "draft")
    assert (state["anchor_date"], state["start_date"]) == (_OLD["draft"]["start_date"],) * 2, state
    assert "next_run_date" not in state


async def test_a_template_a_restore_could_not_move_moves_at_the_next_start(client, session, monkeypatch):
    """A template is refused as moving the next time Celerp starts, and it does, even when the
    pass that failed was the one a restore forces in a company that started this release before."""
    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules.slots import fire_lifecycle
    from celerp_subscriptions import billing, migration
    from ui.i18n import t

    _on(monkeypatch, "2026-11-04")
    old = await pre366.load(session, "demo", FIXTURE)
    cid, eid = old["company_id"], old["runs"]["clean_monthly"]
    await (await session.connection()).run_sync(lambda c: set_meta(c, migration._MARKER.format(cid), "done"))
    await session.commit()
    real = migration._migrate_template

    async def broken_for_one(session, company_id, proj, *a, **kw):
        if proj.entity_id == eid:
            raise RuntimeError("broken on purpose")
        return await real(session, company_id, proj, *a, **kw)

    monkeypatch.setattr(migration, "_migrate_template", broken_for_one)
    assert not await fire_lifecycle("on_company_restored", session=session, company_id=cid)
    await session.commit()
    assert billing.not_moved(await _state(session, old, "clean_monthly")) == t("subscriptions.not_moved_pending")

    monkeypatch.setattr(migration, "_migrate_template", real)
    await pre366.last_started_on_older_release(session, "2.8.0", semantics=2)
    await pre366.start()
    assert (await _state(session, old, "clean_monthly"))["anchor_date"] == _OLD["clean_monthly"]["next_run_date"]


async def test_a_moved_template_s_history_names_only_what_the_move_changed(client, session, monkeypatch):
    """The move onto a schedule records only fields whose shown value changes: billing by
    hand and billing in advance, which a 2.8.0 template already reads as, are not named."""
    import ui.routes.subscriptions  # noqa: F401  (registers the agreement's History describers)
    from celerp.models.ledger import LedgerEntry
    from ui.components.activity import _FIELD_DESCRIBERS

    old = await _upgrade(session, monkeypatch, "2026-11-04")
    said_nothing = []
    for name, eid in old["runs"].items():
        entries = (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == old["company_id"], LedgerEntry.entity_id == eid))).scalars()
        for entry in entries:
            for f, c in ((entry.data or {}).get("fields_changed") or {}).items():
                describe = _FIELD_DESCRIBERS.get(f)
                if describe is None or not isinstance(c, dict) or c.get("drop"):
                    continue
                line = describe(c)
                if line and line == describe({"old": c.get("new"), "new": c.get("new")}):
                    said_nothing.append((name, line))
    assert not said_nothing, said_nothing


async def _daily(session, old) -> None:
    # A template billed every day on 2.8.0 and paused there.
    await _set(session, old, "paused_now", frequency="custom", custom_interval_days=1, next_run_date="2026-04-03")


async def test_a_restart_past_the_due_limit_is_refused_with_nothing_changed(client, session, monkeypatch):
    """Resume on a template paused on 2.8.0 that would skip more open periods than one request
    walks is refused, counting them, and changes nothing; a retry is refused the same way."""
    from celerp_subscriptions.periods import DUE_LIMIT
    from ui.i18n import t

    old = await _upgrade(session, monkeypatch, "2027-12-01", _daily)
    before = await _state(session, old, "paused_now")
    answers = [await client.post(_url(old, "paused_now", "/resume"), headers=old["headers"], json={})
               for _ in range(2)]
    # 2026-04-03 to 2027-11-30, less the period held for history review.
    assert answers[0].status_code == 409, answers[0].text
    assert answers[0].json()["detail"]["message"] == t("subscriptions.err_too_many_started", count=607, limit=DUE_LIMIT)
    assert [(r.status_code, r.json()) for r in answers[1:]] == [(409, answers[0].json())]
    assert await _state(session, old, "paused_now") == before
