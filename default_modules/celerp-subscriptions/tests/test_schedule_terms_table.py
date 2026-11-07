# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every schedule term is declared once (periods.TERMS) and checked by the schedule builder,
so whichever door stores a term, a value of the wrong type or outside its range is refused
with the term's own reason or stored readable, and no stored value, however it got there,
answers 500 for the company. The terms, the request bounds, the write doors and the loop
readers come from the code (schedule_census), so a new one is covered here or fails."""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import agreement, company_id, freeze_day, generate_due, register, schedule, template, tick
from schedule_census import declared_terms, doc_doors, gate_catches, loop_readers, request_bounds, term_fields


TODAY = "2026-03-10"
# A value of every JSON shape a caller can send, and the numbers past any sane range.
HOSTILE = {"string": "x", "float": 2.5, "bool": True, "none": None, "list": ["monthly"], "dict": {"k": 1},
           "negative": -3, "huge": 10 ** 12}
TERMS = sorted(term_fields())
EVENT_TYPES = sorted({e for _, _, events in doc_doors() if not events.startswith("<") for e in events.split(",")})
DOORS = ["create", "import", "batch", "schedule", *(f"event:{e}" for e in EVENT_TYPES)]


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


def _values(term: str) -> dict:
    """The hostile values, plus the whole numbers just outside the term's declared range."""
    spec = declared_terms().get(term)
    edges = {}
    if spec is not None and spec.kind == "whole":
        edges = {**({"below": spec.low - 1} if spec.low is not None else {}),
                 **({"above": spec.high + 1} if spec.high is not None else {})}
    return {**HOSTILE, **edges}


def _cases() -> list[tuple[str, str, object]]:
    return [(term, label, value) for term in TERMS for label, value in _values(term).items()]


def _record(eid: str, terms: dict) -> dict:
    """A subscription document as a snapshot import brings it in: active, on a schedule."""
    data = {"doc_type": "subscription_invoice", "ref_id": eid.split(":")[1], "status": "active",
            "contact_id": "contact:test-001", "frequency": "monthly", "start_date": "2026-03-01",
            "anchor_date": "2026-03-01",
            "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
            **terms}
    return {"entity_id": eid, "event_type": "doc.created", "data": data, "source": "import", **_key()}


@pytest.mark.asyncio
async def _call(coro):
    try:
        return await coro
    except Exception as exc:  # the test client re-raises an unhandled server error (a 500 in production)
        return type("R", (), {"status_code": 500, "text": f"unhandled {type(exc).__name__}: {exc}",
                              "json": lambda self: {}})()


@pytest.mark.asyncio
async def _stored(session, eid: str) -> dict | None:
    from sqlalchemy import select

    from celerp.models.projections import Projection

    session.expire_all()
    return (await session.execute(select(Projection.state).where(Projection.entity_id == eid))).scalar_one_or_none()


def _unreadable(state: dict | None) -> str | None:
    """Why stored terms build no schedule (any error at all counts), or None."""
    from celerp_subscriptions import billing

    if state is None:
        return None
    try:
        exc = billing.unscheduled(state)
    except Exception as err:
        return f"unscheduled raised {type(err).__name__}: {err}"
    return exc.key if exc is not None else None


@pytest.mark.asyncio
async def _emit(session, cid, eid: str, event_type: str, changes: dict) -> None:
    from celerp.events.engine import emit_event

    before = await _stored(session, eid)
    data = {**before, **changes,
            "fields_changed": {f: {"old": before.get(f), "new": v} for f, v in changes.items()}}
    try:
        await emit_event(session, company_id=cid, entity_id=eid, entity_type="doc", event_type=event_type,
                         data=data, actor_id=None, location_id=None, source="test", **_key())
        await session.commit()
    except Exception:
        await session.rollback()


@pytest.mark.asyncio
async def _readers(client, hd) -> dict:
    from celerp_subscriptions import routes

    urls = sorted({"/subscriptions" + r.path for r in routes._build_router().routes
                   if "GET" in r.methods and "{" not in r.path})
    assert {"/subscriptions", "/subscriptions/due", "/subscriptions/catch-up"} <= set(urls)
    return {u: (await _call(client.get(u, headers=hd))).status_code for u in urls}


def test_every_term_is_declared_bounded_once_and_read_behind_a_gate():
    """The census's terms are each declared in periods.TERMS; no request model bounds one ahead
    of the builder; the gate the readers ask turns any error into the row's reason; and no
    reader that loops agreements builds a schedule or reads a term before asking it."""
    assert {"catchup_periods", "cancel_at_period_end", "generation_mode", "auto_send"} <= set(TERMS)
    assert set(TERMS) - set(declared_terms()) == set()
    assert request_bounds() == []
    assert gate_catches() == {"unscheduled": True}
    assert {name: paths for name, (_, paths) in loop_readers().items() if paths} == {}


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.asyncio
async def test_a_hostile_term_through_any_door_is_refused_or_stored_readable(client, session, monkeypatch, door):
    """Every term x every hostile value through one writer door: refused (never a 500), or
    stored as terms that build a schedule; the company's list and previews still read."""
    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    broken = []
    cases = _cases()
    if door == "batch":
        eids = [f"doc:SUBI-H-{uuid.uuid4().hex[:8]}" for _ in cases]
        r = await _call(client.post("/docs/import/batch", headers=hd,
                                    json={"records": [_record(e, {t: v}) for e, (t, _, v) in zip(eids, cases)]}))
        assert r.status_code < 500, r.text
        for eid, (term, label, _) in zip(eids, cases):
            if why := _unreadable(await _stored(session, eid)):
                broken.append(f"{term}={label}: stored {why}")
    else:
        target = None
        if door == "schedule" or door.startswith("event:"):
            target = await agreement(client, hd, start="2026-03-01")
        for term, label, value in cases:
            if door == "create":
                r = await _call(client.post("/docs", headers=hd, json={
                    "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "start_date": "2026-03-01",
                    "line_items": [{"description": "S", "quantity": 1, "unit_price": 10.0}], term: value}))
                eid = (r.json() or {}).get("id") if r.status_code < 300 else None
                if eid:  # a stored draft's terms are what Activate builds from
                    activated = await _call(client.post(f"/subscriptions/{eid}/activate", headers=hd))
                    if activated.status_code >= 500:
                        broken.append(f"{term}={label}: activate {activated.status_code} {activated.text[:120]}")
            elif door == "import":
                eid = f"doc:SUBI-H-{uuid.uuid4().hex[:8]}"
                r = await _call(client.post("/docs/import", headers=hd, json=_record(eid, {term: value})))
            elif door == "schedule":
                eid, r = target, await _call(schedule(client, hd, target, **{term: value}))
            else:
                eid = target
                await _emit(session, cid, target, door.split(":", 1)[1], {term: value})
                r = None
            if r is not None and r.status_code >= 500:
                broken.append(f"{term}={label}: {r.status_code} {r.text[:120]}")
            elif eid and (why := _unreadable(await _stored(session, eid))):
                broken.append(f"{term}={label}: stored {why}")
    answers = await _readers(client, hd)
    assert all(code < 500 for code in answers.values()), answers
    assert not broken, broken


@pytest.mark.asyncio
async def test_hostile_stored_terms_never_break_the_company(client, session, monkeypatch):
    """Every term x every hostile value stored as an earlier release could (the writer check
    off): every collection read answers, each list row names its own reason when its terms
    build no schedule, and the dispatcher still bills the company's healthy agreement."""
    from celerp_subscriptions import billing, migration

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    good = await agreement(client, hd, start=TODAY)
    assert (await schedule(client, hd, good, generation_mode="auto_invoice")).status_code == 200
    monkeypatch.setattr(billing, "refuse_unbuildable_terms", lambda before, after: None)
    stored = {}
    for term, label, value in _cases():
        eid = f"doc:SUBI-S-{uuid.uuid4().hex[:8]}"
        assert (await client.post("/docs/import", headers=hd, json=_record(eid, {}))).status_code == 200
        await _emit(session, cid, eid, "doc.updated", {term: value, **(
            {"generation_mode": "auto_invoice"} if term == "catchup_periods" else {})})
        stored[eid] = f"{term}={label}"
    answers = await _readers(client, hd)
    assert all(code < 500 for code in answers.values()), answers
    rows = {i.get("id") or i["entity_id"]: i for i in
            (await client.get("/subscriptions", headers=hd, params={"limit": 500})).json()["items"]}
    wrong = {label: rows[eid]["schedule_error"] for eid, label in stored.items()
             if bool(rows[eid]["schedule_error"]) != bool(_unreadable(await _stored(session, eid)))}
    assert not wrong, wrong
    assert any(rows[eid]["schedule_error"] for eid in stored)
    await tick(session, str(cid), TODAY)
    assert (await template(client, hd, good)).get("periods", {}).get("0", {}).get("state") == "billed"
    await migration.check_template_schedule_integrity(session, cid)
    await migration.reconcile_restored_company_schedules(session=session, company_id=cid)


@pytest.mark.asyncio
async def test_every_term_the_schedule_route_takes_is_refused_by_its_own_reason(client, session, monkeypatch):
    """Each term the schedule route takes, sent outside its declared range in the type the
    route reads, is refused with the term's keyed reason (the TERMS entry), never a generic
    invalid-input answer that names the field."""
    from celerp_subscriptions.routes import ScheduleBody

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    eid = await agreement(client, hd, start=TODAY)
    declared = declared_terms()
    tried, wrong = [], {}
    for term in sorted(set(ScheduleBody.model_fields) & set(declared)):
        spec = declared[term]
        outside = ([v for v in (spec.low - 1 if spec.low is not None else None,
                                spec.high + 1 if spec.high is not None else None) if v is not None]
                   if spec.kind == "whole" else ["bogus"] if spec.kind == "choice" else [])
        for value in outside:
            r = await schedule(client, hd, eid, **{term: value})
            tried.append(term)
            key = (r.json().get("detail") or {}).get("message_key") if r.status_code == 422 else None
            if key != spec.key:
                wrong[f"{term}={value}"] = (r.status_code, r.text[:160])
    assert {"custom_interval_days", "catchup_periods", "frequency", "billing_timing", "generation_mode"} <= set(tried)
    assert not wrong, wrong


@pytest.mark.parametrize("periods", [{"x": {}}, {"0": "billed"}, ["0"]], ids=["key", "entry", "shape"])
@pytest.mark.asyncio
async def test_an_unreadable_stored_schedule_reads_as_that_rows_reason(client, session, monkeypatch, periods):
    """Stored bookkeeping no writer gives (a period key that is not a number, an entry that is
    not a record, periods that are not a mapping) reads as the row's own unreadable-schedule
    reason: the list names it, its page refuses by that key, the rest of the company reads."""
    from ui.i18n import t

    from celerp_subscriptions import billing

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    good = await agreement(client, hd, start=TODAY)
    eid = await agreement(client, hd, start=TODAY)
    monkeypatch.setattr(billing, "refuse_unbuildable_terms", lambda before, after: None)
    await _emit(session, cid, eid, "doc.updated", {"periods": periods})
    assert (await _stored(session, eid))["periods"] == periods
    assert billing.unscheduled(await _stored(session, eid)).key == "subscriptions.err_schedule_unreadable"
    answers = await _readers(client, hd)
    assert all(code < 500 for code in answers.values()), answers
    rows = {i.get("id") or i["entity_id"]: i for i in (await client.get("/subscriptions", headers=hd)).json()["items"]}
    assert rows[eid]["schedule_error"] == t("subscriptions.err_schedule_unreadable")
    assert rows[good]["schedule_error"] is None and rows[good]["next_bill_date"] == TODAY
    page = await _call(client.get(f"/subscriptions/{eid}/periods", headers=hd))
    assert page.status_code == 409 and page.json()["detail"]["message_key"] == "subscriptions.err_schedule_unreadable"
    billing_door = await _call(generate_due(client, hd, eid))
    assert billing_door.status_code == 409, billing_door.text
    assert billing_door.json()["detail"]["message_key"] == "subscriptions.err_schedule_unreadable"
    # A write that names a period of it (the dispatcher recording a failed run) still records,
    # without the period's dates History cannot read off it.
    await billing.write_agreement(session, cid, eid, {"billing_failure": {"period_index": 0, "at": "x"}}, None)
    await session.commit()
    assert (await _stored(session, eid))["billing_failure"] == {"period_index": 0, "at": "x"}
