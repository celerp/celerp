# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Deferred revenue: schedules made at finalize, monthly recognition runs, their posting by
hand or by the dispatcher, reversal, amendments, credit notes and the roll-forward.

Initial conditions per test: a fresh company with the seeded chart (4100 Sales Revenue,
1120 the receivable, 2160 Deferred Revenue). The business day is frozen per test, so the
invoice entry and the runs it plans fall on known dates. A service from 1 January to
31 March 2026 is 90 days: 900.00 recognizes 310.00, 280.00 and 310.00. An item recognizes
over its service period only when its own flag says so; nothing that existed before does.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection
from test_credit_note_settlement_owned import _post
from test_income_accounts import _category, _draft, _item, _line, ui_client  # noqa: F401 (fixture)
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio

LAPSED = "Automatic recognition stopped: authorization lapsed"


def _freeze(monkeypatch, day: str) -> None:
    """Every business-day decision sees noon UTC on ``day``."""
    from celerp.services import business_time

    instant = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(business_time, "datetime", _Frozen)


def _key(r) -> str:
    return r.json()["detail"]["message_key"]


def _service(item, amount, start="2026-01-01", end="2026-03-31", name="Annual plan", **extra) -> dict:
    return _line(item, name, amount, service_start=start, service_end=end, **extra)


async def _deferred_item(client, auth, name="Annual plan", **fields) -> str:
    return await _item(client, auth, name, recognize_over_service_period=True, **fields)


async def _invoice(client, auth, lines: list[dict], **extra) -> str:
    doc = await _draft(client, auth, "invoice", lines, **extra)
    r = await _post(client, auth, f"/docs/{doc}/finalize")
    assert r.status_code == 200, r.text
    return doc


async def _schedules(session, auth, doc: str | None = None) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "rev_schedule"))).scalars().all()
    return [{"id": r.entity_id, **r.state} for r in rows if doc is None or r.state.get("doc_id") == doc]


async def _runs(client, auth) -> list[dict]:
    r = await client.get("/accounting/recognition", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def _by_month(runs: list[dict]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for run in runs:
        out.setdefault(run["month"], []).append((run["segment"], run["amount"], run["status"]))
    return {m: sorted(v) for m, v in out.items()}


async def _post_month(client, auth, month: str, **body):
    return await _post(client, auth, f"/accounting/recognition/{month}/post", body)


async def _reverse_month(client, auth, month: str, **body):
    return await _post(client, auth, f"/accounting/recognition/{month}/reverse", body)


async def _run_entries(session, auth) -> list[dict]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:auto:rev:%")))).scalars().all()
    return [{"id": r.entity_id, **r.state} for r in rows]


async def _authorize(client, headers, enabled=True):
    return await client.put("/accounting/recognition/setting", headers=headers, json={"enabled": enabled})


async def _tick(session, auth, day: str):
    """One dispatcher pass for the company on ``day``, unattended, in the test's transaction."""
    from contextlib import asynccontextmanager

    from celerp.events.engine import unattended
    from celerp_docs import periodic

    @asynccontextmanager
    async def factory():
        yield session

    await unattended(periodic.run_company(factory, uuid.UUID(str(auth["company_id"])),
                                          today=date.fromisoformat(day)))


async def _member(client, session, auth, role: str) -> tuple[dict, str]:
    from test_helpers import invite_user

    email = f"{role}-{uuid.uuid4().hex[:8]}@test.test"
    headers = {"Authorization": f"Bearer {await invite_user(client, session, auth['headers'], email, role)}"}
    users = (await client.get("/companies/me/users", headers=auth["headers"])).json()["items"]
    return headers, next(str(u.get("id") or u.get("user_id")) for u in users if u["email"] == email)


async def _setting(session, auth) -> dict:
    from celerp.models.company import Company

    session.expire_all()
    return (await session.get(Company, auth["company_id"])).settings or {}


# ── schedules at finalize ────────────────────────────────────────────────────────────────

async def test_deferral_policy_creates_schedule_with_posting_off(client, session, auth, monkeypatch):
    """A line selling an item that recognizes over its service period credits Deferred revenue
    and makes a schedule holding the source line, account, period, amount, rate and basis;
    automatic posting is off and nothing is recognized until a run posts."""
    _freeze(monkeypatch, "2026-01-05")
    plan, other = await _deferred_item(client, auth), await _item(client, auth, "Setup fee")
    doc = await _invoice(client, auth, [_service(plan, 900), _line(other, "Setup fee", 100)])
    [schedule] = await _schedules(session, auth, doc)
    deferred = schedule["deferred_account"]
    line_id = (await client.get(f"/docs/{doc}", headers=auth["headers"])).json()["line_items"][0]["line_id"]
    assert schedule["id"] == f"rev:{doc}:{line_id}" and schedule["line_id"] == line_id
    assert schedule["account"] == "4100" and schedule["basis"] == "daily" and schedule["fund_id"] is None
    assert schedule["source_je"] == f"je:auto:{doc}:fin" and schedule["rate"] == 1
    assert schedule["segments"] == [{"start": "2026-01-01", "end": "2026-03-31", "amount": 900.0}]
    assert await _net(session, auth, deferred) == -900.0 and await _net(session, auth, "4100") == -100.0
    setting = (await client.get("/accounting/recognition/setting", headers=auth["headers"])).json()
    assert setting == {"enabled": False, "by": None, "at": None, "lapsed": None}
    assert await _run_entries(session, auth) == []


async def test_existing_items_never_deferred(client, session, auth, monkeypatch):
    """An item made without the flag, even on a line with a service period, posts its revenue
    as every sale did and makes no schedule."""
    _freeze(monkeypatch, "2026-01-05")
    item = await _item(client, auth, "Consulting")
    assert not (await client.get(f"/items/{item}", headers=auth["headers"])).json().get("recognize_over_service_period")
    doc = await _invoice(client, auth, [_service(item, 900)])
    assert await _schedules(session, auth, doc) == []
    assert await _net(session, auth, "4100") == -900.0 and await _net(session, auth, "1120") == 900.0


async def test_deposits_and_restricted_gifts_never_deferred(client, session, auth, monkeypatch):
    """A flagged item in a gift category makes no schedule, and a deposit (a payment taken on
    an invoice on deposit terms) is cash, never a schedule or revenue."""
    _freeze(monkeypatch, "2026-01-05")
    donation = await _category(client, auth, "Donation")
    r = await client.put(f"/companies/me/categories/{donation}/gift", headers=auth["headers"], json={"gift": True})
    assert r.status_code == 200, r.text
    gift = await _deferred_item(client, auth, "Annual gift", category=donation)
    doc = await _invoice(client, auth, [_service(gift, 300, name="Annual gift")])
    assert await _schedules(session, auth, doc) == [] and await _net(session, auth, "4100") == -300.0

    plain = await _item(client, auth, "Workshop")
    deposit = await _invoice(client, auth, [_line(plain, "Workshop", 200)], payment_terms="Deposit (50%)")
    r = await _post(client, auth, f"/docs/{deposit}/payment", {
        "amount": 100, "method": "cash", "payment_date": "2026-01-05", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    assert await _schedules(session, auth) == [] and await _net(session, auth, "4100") == -500.0


async def test_deferred_line_needs_service_period(client, session, auth, monkeypatch):
    """Finalize refuses a deferred line with no service period, or one ending before it starts,
    naming the line; nothing posts."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    for line in (_line(plan, "Annual plan", 900), _service(plan, 900, start="2026-03-01", end="2026-02-01")):
        doc = await _draft(client, auth, "invoice", [line])
        r = await _post(client, auth, f"/docs/{doc}/finalize")
        assert r.status_code == 422 and _key(r) == "recognition.err_service_period", r.text
        assert await _schedules(session, auth, doc) == []


# ── runs ─────────────────────────────────────────────────────────────────────────────────

async def test_partial_first_period_daily_share(client, session, auth, monkeypatch):
    """A service from 20 January for 90 days recognizes 12 days' share in January, and the last
    run is exactly what is left."""
    _freeze(monkeypatch, "2026-01-20")
    plan = await _deferred_item(client, auth)
    doc = await _invoice(client, auth, [_service(plan, 900, start="2026-01-20", end="2026-04-19")])
    [schedule] = await _schedules(session, auth, doc)
    _freeze(monkeypatch, "2026-05-02")
    assert _by_month(await _runs(client, auth)) == {
        "2026-01": [(0, 120.0, "outstanding")], "2026-02": [(0, 280.0, "outstanding")],
        "2026-03": [(0, 310.0, "outstanding")], "2026-04": [(0, 190.0, "outstanding")]}
    for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
        assert (await _post_month(client, auth, month)).status_code == 200
    assert await _net(session, auth, schedule["deferred_account"], prefix="je:") == 0.0
    assert await _net(session, auth, "4100", prefix="je:") == -900.0
    [jan] = [e for e in await _run_entries(session, auth) if ":2026-01:" in e["id"]]
    assert jan["ts"].startswith("2026-01-31")


async def test_recognition_run_idempotent_per_schedule_period(client, session, auth, monkeypatch):
    """A run posted by hand, posted again, and then reached by the dispatcher is one entry; the
    repeat answers with that entry."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900)])
    _freeze(monkeypatch, "2026-02-03")
    first = await _post_month(client, auth, "2026-01")
    again = await _post_month(client, auth, "2026-01")
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert again.json()["runs"] == first.json()["runs"] and first.json()["runs"][0]["je_id"]
    assert (await _authorize(client, auth["headers"])).status_code == 200
    await _tick(session, auth, "2026-02-03")
    assert len(await _run_entries(session, auth)) == 1


async def test_missed_run_stays_outstanding_and_visible(client, session, auth, monkeypatch):
    """Months that ended unposted stay outstanding in the list, the roll-forward and the summary;
    the dispatcher catches up only the latest ended month, never the older ones."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900)])
    _freeze(monkeypatch, "2026-04-05")
    runs = _by_month(await _runs(client, auth))
    assert runs == {"2026-01": [(0, 310.0, "outstanding")], "2026-02": [(0, 280.0, "outstanding")],
                    "2026-03": [(0, 310.0, "outstanding")]}
    summary = (await client.get("/accounting/recognition/summary", headers=auth["headers"])).json()
    assert summary["outstanding"] == 3 and summary["months"] == ["2026-01", "2026-02", "2026-03"]
    roll = (await client.get("/accounting/recognition/roll-forward", headers=auth["headers"],
                             params={"date_from": "2026-01-01", "date_to": "2026-03-31"})).json()
    assert sorted(r["month"] for r in roll["outstanding"]) == ["2026-01", "2026-02", "2026-03"]
    assert (await _authorize(client, auth["headers"])).status_code == 200
    await _tick(session, auth, "2026-04-05")
    runs = _by_month(await _runs(client, auth))
    assert runs["2026-03"] == [(0, 310.0, "posted")]
    assert runs["2026-01"] == [(0, 310.0, "outstanding")] and runs["2026-02"] == [(0, 280.0, "outstanding")]


async def test_closed_period_run_refused(client, session, auth, monkeypatch):
    """A run in a locked month is refused with the lock date and the first open day; posting it
    on that day is a recorded choice, any other day is refused, and a repeat returns the entry."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900)])
    _freeze(monkeypatch, "2026-02-10")
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": "2026-01-31"})
    assert r.status_code == 200, r.text
    r = await _post_month(client, auth, "2026-01")
    assert r.status_code == 409 and _key(r) == "recognition.err_month_locked", r.text
    assert r.json()["detail"]["params"]["lock_date"] == "2026-01-31"
    assert r.json()["detail"]["params"]["first_open"] == "2026-02-01"
    r = await _post_month(client, auth, "2026-01", post_on="2026-02-05")
    assert r.status_code == 422 and _key(r) == "recognition.err_post_on", r.text
    assert await _run_entries(session, auth) == []
    posted = await _post_month(client, auth, "2026-01", post_on="2026-02-01")
    assert posted.status_code == 200, posted.text
    [entry] = await _run_entries(session, auth)
    assert entry["ts"].startswith("2026-02-01")
    repeat = await _post_month(client, auth, "2026-01", post_on="2026-02-01")
    assert repeat.json()["runs"] == posted.json()["runs"] and len(await _run_entries(session, auth)) == 1


async def test_recognition_reverse_refuses_unposted_run(client, session, auth, monkeypatch):
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900)])
    _freeze(monkeypatch, "2026-02-03")
    r = await _reverse_month(client, auth, "2026-01")
    assert r.status_code == 409 and _key(r) == "recognition.err_run_not_posted", r.text
    assert await _run_entries(session, auth) == []


async def test_run_reversal_reopens_run(client, session, auth, monkeypatch):
    """Reverse posts the run's lines swapped and the run is outstanding again; a repeat finds it
    outstanding; posting it again is a new entry. A reversal in a locked month takes the first
    open day only as the recorded choice."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    doc = await _invoice(client, auth, [_service(plan, 900)])
    [schedule] = await _schedules(session, auth, doc)
    _freeze(monkeypatch, "2026-02-03")
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    r = await _reverse_month(client, auth, "2026-01")
    assert r.status_code == 200, r.text
    assert _by_month(await _runs(client, auth))["2026-01"] == [(0, 310.0, "outstanding")]
    assert await _net(session, auth, schedule["deferred_account"], prefix="je:") == -900.0
    again = await _reverse_month(client, auth, "2026-01")
    assert again.status_code == 200 and again.json() == r.json()
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    ids = sorted(e["id"] for e in await _run_entries(session, auth))
    assert len(ids) == 3 and any(i.endswith(":2") for i in ids) and any(i.endswith(":reversal") for i in ids)

    assert (await client.post("/accounting/period-lock", headers=auth["headers"],
                              json={"lock_date": "2026-01-31"})).status_code == 200
    r = await _reverse_month(client, auth, "2026-01")
    assert r.status_code == 409 and _key(r) == "recognition.err_month_locked", r.text
    r = await _reverse_month(client, auth, "2026-01", reverse_on="2026-02-01")
    assert r.status_code == 200, r.text
    assert _by_month(await _runs(client, auth))["2026-01"] == [(0, 310.0, "outstanding")]


# ── automatic posting ────────────────────────────────────────────────────────────────────

async def test_auto_post_recognition_requires_authorization(client, session, auth):
    """Automatic posting is off by default; only a member who manages accounting turns it on,
    which records who and when; turning it off clears the record."""
    viewer, _ = await _member(client, session, auth, "viewer")
    r = await _authorize(client, viewer)
    assert r.status_code == 403, r.text
    assert "recognition" not in ((await _setting(session, auth)).get("consents") or {})
    r = await _authorize(client, auth["headers"])
    assert r.status_code == 200, r.text
    consent = (await _setting(session, auth))["consents"]["recognition"]
    assert consent["by"] == str(auth["user_id"]) and consent["at"]
    assert (await _authorize(client, auth["headers"], enabled=False)).status_code == 200
    assert "recognition" not in ((await _setting(session, auth)).get("consents") or {})


@pytest.mark.parametrize("lapse", ["permission", "deactivated"])
async def test_recognition_autopost_stops_when_authorizer_loses_permission(client, session, auth, monkeypatch, lapse):
    """The dispatcher checks the authorizer on every pass: once they lose manage_accounting (moved to
    a role without it) or are deactivated it posts nothing and the summary says so; a manual post still works."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    await _invoice(client, auth, [_service(plan, 900)])
    manager, manager_id = await _member(client, session, auth, "manager")
    assert (await _authorize(client, manager)).status_code == 200
    await _tick(session, auth, "2026-02-02")
    assert _by_month(await _runs(client, auth))["2026-01"] == [(0, 310.0, "posted")]
    if lapse == "permission":
        r = await client.patch(f"/companies/me/users/{manager_id}", headers=auth["headers"], json={"role": "viewer"})
    else:
        r = await client.patch(f"/companies/me/users/{manager_id}", headers=auth["headers"], json={"is_active": False})
    assert r.status_code == 200, r.text
    _freeze(monkeypatch, "2026-03-02")
    await _tick(session, auth, "2026-03-02")
    assert _by_month(await _runs(client, auth))["2026-02"] == [(0, 280.0, "outstanding")]
    summary = (await client.get("/accounting/recognition/summary", headers=auth["headers"])).json()
    assert summary["lapsed"] and summary["message"] == LAPSED
    assert (await _post_month(client, auth, "2026-02")).status_code == 200
    assert len(await _run_entries(session, auth)) == 2


# ── amendments, credits and the source ───────────────────────────────────────────────────

async def test_mid_term_amendment_replans_remainder(client, session, auth, monkeypatch):
    """An amendment closes the segment the day before it takes effect and plans what is left
    over the new period; the posted January run is untouched. One effective inside a posted
    run is refused."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    doc = await _invoice(client, auth, [_service(plan, 900)])
    [schedule] = await _schedules(session, auth, doc)
    _freeze(monkeypatch, "2026-02-03")
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    [jan] = await _run_entries(session, auth)
    path = f"/accounting/recognition/schedules/{schedule['id']}/amend"
    r = await _post(client, auth, path, {"effective": "2026-01-10", "service_end": "2026-04-30", "reason": "Extended"})
    assert r.status_code == 409 and _key(r) == "recognition.err_amend_posted", r.text
    r = await _post(client, auth, path, {"effective": "2026-02-15", "service_end": "2026-04-30", "reason": "Extended"})
    assert r.status_code == 200, r.text
    [amended] = await _schedules(session, auth, doc)
    assert amended["segments"] == [{"start": "2026-01-01", "end": "2026-02-14", "amount": 450.0},
                                   {"start": "2026-02-15", "end": "2026-04-30", "amount": 450.0}]
    assert amended["amendments"][0]["kind"] == "amend" and amended["amendments"][0]["reason"] == "Extended"
    _freeze(monkeypatch, "2026-05-02")
    assert _by_month(await _runs(client, auth)) == {
        "2026-01": [(0, 310.0, "posted")], "2026-02": [(0, 140.0, "outstanding"), (1, 84.0, "outstanding")],
        "2026-03": [(1, 186.0, "outstanding")], "2026-04": [(1, 180.0, "outstanding")]}
    assert [e for e in await _run_entries(session, auth) if e["id"] == jan["id"]] == [jan]


async def test_cancellation_credit_note_reduces_unrecognized_first(client, session, auth, monkeypatch):
    """A credit note on a scheduled line takes Deferred revenue down first and the revenue
    account only for what was already recognized; the rest is planned again."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    doc = await _invoice(client, auth, [_service(plan, 900)])
    [schedule] = await _schedules(session, auth, doc)
    deferred = schedule["deferred_account"]
    line_id = schedule["line_id"]
    _freeze(monkeypatch, "2026-02-10")
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    note = await _draft(client, auth, "credit_note", [_line(plan, "Annual plan", 100, source_line_id=line_id)],
                        original_doc_id=doc)
    assert (await _post(client, auth, f"/docs/{note}/finalize")).status_code == 200
    assert await _net(session, auth, deferred, prefix="je:") == -490.0
    assert await _net(session, auth, "4100", prefix="je:") == -310.0
    [s] = await _schedules(session, auth, doc)
    assert s["segments"][-1] == {"start": "2026-02-01", "end": "2026-03-31", "amount": 490.0}
    assert s["amendments"][-1]["kind"] == "credit"
    _freeze(monkeypatch, "2026-03-10")
    assert _by_month(await _runs(client, auth))["2026-02"][-1] == (1, 232.54, "outstanding")
    assert (await _post_month(client, auth, "2026-02")).status_code == 200
    note = await _draft(client, auth, "credit_note", [_line(plan, "Annual plan", 400, source_line_id=line_id)],
                        original_doc_id=doc)
    assert (await _post(client, auth, f"/docs/{note}/finalize")).status_code == 200
    assert await _net(session, auth, deferred, prefix="je:") == 0.0
    assert await _net(session, auth, "4100", prefix="je:") == -400.0
    assert all(run["amount"] == 0 for run in await _runs(client, auth) if run["month"] == "2026-03")


async def test_recognition_stops_for_void_source(client, session, auth, monkeypatch):
    """Voiding the invoice plans no further run; the roll-forward lists the schedule under its
    void source with both posted runs, and reversing them takes Deferred revenue to zero."""
    _freeze(monkeypatch, "2026-01-05")
    plan = await _deferred_item(client, auth)
    doc = await _invoice(client, auth, [_service(plan, 900)])
    [schedule] = await _schedules(session, auth, doc)
    _freeze(monkeypatch, "2026-03-02")
    for month in ("2026-01", "2026-02"):
        assert (await _post_month(client, auth, month)).status_code == 200
    assert (await _post(client, auth, f"/docs/{doc}/void", {"reason": "Issued in error"})).status_code == 200
    _freeze(monkeypatch, "2026-04-02")
    assert (await _authorize(client, auth["headers"])).status_code == 200
    await _tick(session, auth, "2026-04-02")
    r = await _post_month(client, auth, "2026-03")
    assert r.status_code == 200 and r.json()["runs"] == [], r.text
    assert len(await _run_entries(session, auth)) == 2
    roll = (await client.get("/accounting/recognition/roll-forward", headers=auth["headers"],
                             params={"date_from": "2026-01-01", "date_to": "2026-04-30"})).json()
    [void] = roll["void_source"]
    assert void["schedule_id"] == schedule["id"] and sorted(r["month"] for r in void["posted"]) == ["2026-01", "2026-02"]
    for month in ("2026-01", "2026-02"):
        assert (await _reverse_month(client, auth, month)).status_code == 200
    assert await _net(session, auth, schedule["deferred_account"], prefix="je:") == 0.0


async def test_change_fund_moves_schedule_and_posted_runs(client, session, auth, monkeypatch):
    """Change fund on an imported invoice with a deferred line: the schedule takes the line's
    new fund, a run standing posted is voided and posted again on it at its own date, a run
    posted and reversed stays as it was, and Reverse afterwards reverses the copy."""
    from decimal import Decimal

    from test_change_fund import _change_doc, _standing
    from test_fund_movements import _setup
    from test_outside_lines import _issued_import

    _freeze(monkeypatch, "2026-01-05")
    f = await _setup(client, auth)
    plan = await _deferred_item(client, auth)
    doc = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={
        "records": [_issued_import(doc, [_service(plan, 900)])], "fund_choices": {"records": {doc: f["U1"]}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    [schedule] = await _schedules(session, auth, doc)
    assert schedule["fund_id"] == f["U1"]
    _freeze(monkeypatch, "2026-04-05")
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    assert (await _post_month(client, auth, "2026-02")).status_code == 200
    assert (await _reverse_month(client, auth, "2026-02")).status_code == 200

    r = await _change_doc(client, session, auth, doc, document=f["U2"])
    assert r.status_code == 200, r.text
    [schedule] = await _schedules(session, auth, doc)
    assert schedule["fund_id"] == f["U2"]
    assert await _standing(session, auth, "je:auto:rev:") == {
        (f["U2"], schedule["deferred_account"]): Decimal("310.00"), (f["U2"], "4100"): Decimal("-310.00")}
    copy = next(e for e in await _run_entries(session, auth) if ":2026-01:1:fund:" in e["id"])
    assert copy["status"] == "posted" and str(copy["ts"])[:10] == "2026-01-31"
    assert _by_month(await _runs(client, auth)) == {
        "2026-01": [(0, 310.0, "posted")], "2026-02": [(0, 280.0, "outstanding")], "2026-03": [(0, 310.0, "outstanding")]}
    roll = (await client.get("/accounting/recognition/roll-forward", headers=auth["headers"],
                             params={"date_from": "2026-01-01", "date_to": "2026-03-31"})).json()
    assert roll["deferred"] == 900.0 and roll["recognized"] == 310.0 and roll["closing"] == roll["liability"] == 590.0
    assert roll["by_fund"][f["U2"]]["closing"] == roll["by_fund"][f["U2"]]["liability"] == 590.0
    assert not any(roll["by_fund"].get(f["U1"], {}).values())

    assert (await _reverse_month(client, auth, "2026-01")).status_code == 200
    assert await _standing(session, auth, "je:auto:rev:") == {}
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    assert await _standing(session, auth, "je:auto:rev:") == {
        (f["U2"], schedule["deferred_account"]): Decimal("310.00"), (f["U2"], "4100"): Decimal("-310.00")}


# ── roll-forward and page ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("funds", [False, True])
async def test_schedule_reconciles_to_liability(client, session, auth, monkeypatch, funds):
    """The roll-forward's closing balance is the schedules' unrecognized total and the Deferred
    revenue account's balance; with funds on, each run posts on its schedule's fund."""
    from test_fund_movements import _setup

    _freeze(monkeypatch, "2026-01-05")
    fund = (await _setup(client, auth))["U1"] if funds else None
    plan = await _deferred_item(client, auth)
    first = await _invoice(client, auth, [_service(plan, 900, **({"fund_id": fund} if fund else {}))])
    await _invoice(client, auth, [_service(plan, 600, start="2026-02-01", end="2026-02-28")])
    [schedule] = await _schedules(session, auth, first)
    assert schedule["fund_id"] == fund
    _freeze(monkeypatch, "2026-02-03")
    assert (await _post_month(client, auth, "2026-01")).status_code == 200
    roll = (await client.get("/accounting/recognition/roll-forward", headers=auth["headers"],
                             params={"date_from": "2026-01-01", "date_to": "2026-01-31"})).json()
    assert roll["opening"] == 0 and roll["deferred"] == 1500.0 and roll["recognized"] == 310.0
    assert roll["adjusted"] == 0 and roll["closing"] == 1190.0 and roll["liability"] == 1190.0
    assert -await _net(session, auth, schedule["deferred_account"], prefix="je:") == 1190.0
    [run] = await _run_entries(session, auth)
    assert {e.get("fund_id") for e in run["entries"]} == {fund}
    if funds:
        assert roll["by_fund"][fund]["closing"] == 590.0


async def test_recognition_empty_state(ui_client):
    """Accounting > Revenue recognition with no schedule shows the empty state and how to start."""
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api

    empty = {"runs": [], "schedules": 0, "summary": {"outstanding": 0, "months": [], "lapsed": None, "message": None}}
    with _ui_api(get_recognition=AsyncMock(return_value=empty)):
        r = await ui_client.get("/accounting/recognition")
    assert r.status_code == 200, (r.status_code, r.headers.get("location"))
    assert "No revenue is deferred. Set an item or agreement to recognize over its service period to start." in r.text
