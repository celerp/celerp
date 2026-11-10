# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An agreement's stored schedule terms always build a schedule, whichever writer stores them,
and one agreement whose terms build none (stored by an earlier release) never breaks the
company's lists, previews or dispatcher. The writers and readers are read from the code
(schedule_census), so a new door or reader is covered or fails here."""
from __future__ import annotations

import uuid

import pytest

from agreement_kit import (agreement, company_id, freeze_day, generate_due, next_bill_date, register, schedule,
                           template, tick)
from schedule_census import direct_writers, doc_doors, loop_readers, term_fields

pytestmark = pytest.mark.asyncio

TODAY = "2026-03-10"
# Terms that describe no schedule, each with the reason the schedule gives.
BAD = {
    "custom_no_interval": ({"frequency": "custom"}, "subscriptions.err_custom_interval"),
    "timing": ({"billing_timing": "foo"}, "subscriptions.err_schedule_timing"),
    "frequency": ({"frequency": "fortnightly_x"}, "subscriptions.err_schedule_frequency"),
    "anchor": ({"anchor_date": "not-a-date"}, "subscriptions.err_schedule_date"),
    "term_end": ({"term_end": "2025-08-01"}, "subscriptions.err_term_before_first_period"),
}


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


def _record(eid: str, bad: dict) -> dict:
    """A subscription document as a snapshot import brings it in: active, on a schedule."""
    data = {"doc_type": "subscription_invoice", "ref_id": eid.split(":")[1], "status": "active",
            "contact_id": "contact:test-001", "frequency": "monthly", "start_date": "2025-09-01",
            "anchor_date": "2025-09-01",
            "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
            **bad}
    return {"entity_id": eid, "event_type": "doc.created", "data": data, "source": "import", **_key()}


async def _get(client, url, hd):
    try:
        return await client.get(url, headers=hd)
    except Exception as exc:  # the test client re-raises an unhandled server error (a 500 in production)
        return type("R", (), {"status_code": 500, "text": f"unhandled {type(exc).__name__}: {exc}"})()


def _builds(state: dict) -> bool:
    from celerp_subscriptions import billing

    return billing.unscheduled(state) is None if hasattr(billing, "unscheduled") else _schedule_ok(state)


def _schedule_ok(state: dict) -> bool:
    from celerp_subscriptions.periods import Schedule

    if not state.get("anchor_date"):
        return True
    try:
        Schedule.from_state(state)
    except ValueError:
        return False
    return True


def _check_off(monkeypatch) -> None:
    """Store terms as an earlier release could: the writer check switched off."""
    from celerp_subscriptions import billing

    monkeypatch.setattr(billing, "refuse_unbuildable_terms", lambda before, after: None, raising=False)


async def _stored(session, eid: str) -> dict | None:
    from sqlalchemy import select

    from celerp.models.projections import Projection

    session.expire_all()
    return (await session.execute(select(Projection.state).where(Projection.entity_id == eid))).scalar_one_or_none()


@pytest.mark.parametrize("case", sorted(BAD))
async def test_an_import_never_stores_terms_that_build_no_schedule(client, session, monkeypatch, case):
    bad, key = BAD[case]
    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    one = f"doc:SUBI-BAD-{uuid.uuid4().hex[:6]}"
    r = await client.post("/docs/import", headers=hd, json=_record(one, bad))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == key
    batch = f"doc:SUBI-BAD-{uuid.uuid4().hex[:6]}"
    rb = await client.post("/docs/import/batch", headers=hd, json={"records": [_record(batch, bad)]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["created"] == 0, rb.text
    assert await _stored(session, one) is None and await _stored(session, batch) is None
    assert (await _get(client, "/subscriptions", hd)).status_code == 200


async def test_every_document_event_the_code_writes_keeps_an_agreement_on_a_schedule(client, session, monkeypatch):
    """Each document event type a door in the code writes, sent with a term that builds no
    schedule onto a working agreement, leaves its stored terms building one: refused, or not
    written. The event types are the census's, so a new door's type is tried here."""
    from celerp.events.engine import emit_event

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    types = sorted({e for _, _, events in doc_doors() if not events.startswith("<") for e in events.split(",")})
    assert {"doc.created", "doc.updated"} <= set(types)
    broken = []
    for event_type in types:
        eid = await agreement(client, hd, start="2026-03-01")
        before = await _stored(session, eid)
        bad = {"frequency": "custom", "custom_interval_days": None}
        data = {**before, **bad, "fields_changed": {"frequency": {"old": before.get("frequency"), "new": "custom"}}}
        try:
            await emit_event(session, company_id=cid, entity_id=eid, entity_type="doc", event_type=event_type,
                             data=data, actor_id=None, location_id=None, source="test", **_key())
        except Exception:
            pass
        after = await _stored(session, eid)
        if not _builds(after):
            broken.append(event_type)
    assert not broken, broken


@pytest.mark.parametrize("cleared", [None, ""], ids=["null", "empty"])
async def test_a_raw_event_never_takes_an_agreement_off_its_schedule(client, session, monkeypatch, cleared):
    """A document event clearing a scheduled agreement's first bill date, written past every
    route, is refused naming why (the agreement keeps its first bill date: pause or cancel it to
    stop billing), and its next bill date stays."""
    from fastapi import HTTPException

    from celerp.events.engine import emit_event
    from celerp_subscriptions.billing import refuse_unbuildable_terms

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    cid = uuid.UUID(await company_id(client, hd))
    eid = await agreement(client, hd, start="2026-03-01")
    before = await _stored(session, eid)
    nxt = await next_bill_date(client, hd, eid)
    data = {**before, "anchor_date": cleared,
            "fields_changed": {"anchor_date": {"old": before["anchor_date"], "new": cleared}}}
    with pytest.raises(HTTPException) as refused:
        await emit_event(session, company_id=cid, entity_id=eid, entity_type="doc", event_type="doc.updated",
                         data=data, actor_id=None, location_id=None, source="test", **_key())
    await session.rollback()
    assert (refused.value.status_code, refused.value.detail["message_key"]) == (422, "subscriptions.err_schedule_removed")
    assert ((await template(client, hd, eid))["anchor_date"], await next_bill_date(client, hd, eid)) == (
        before["anchor_date"], nxt)
    # The same rule read directly: only a write clearing a stored first bill date is refused by it.
    with pytest.raises(HTTPException):
        refuse_unbuildable_terms(before, {**before, "anchor_date": cleared})
    refuse_unbuildable_terms({**before, "anchor_date": None}, {**before, "anchor_date": None})


async def test_every_reader_that_loops_agreements_asks_first():
    """Every function that loops over many agreements and builds a schedule asks whether the
    row's terms build one first; every projection writer outside emit_event names no term."""
    readers = loop_readers()
    assert {"billing.py::due_preview", "routes.py::list_subscription_templates"} <= set(readers)
    assert {name: paths for name, (_, paths) in readers.items() if paths} == {}
    terms = term_fields()
    assert "frequency" in terms
    assert {name: named for name, named in direct_writers().items() if named} == {}


@pytest.mark.parametrize("case", sorted(BAD))
async def test_one_agreement_without_a_schedule_never_breaks_the_company(client, session, monkeypatch, case):
    from celerp_subscriptions import migration, routes

    bad, key = BAD[case]
    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    good = await agreement(client, hd, start=TODAY)
    assert (await schedule(client, hd, good, generation_mode="auto_invoice")).status_code == 200
    eid = f"doc:SUBI-BAD-{uuid.uuid4().hex[:6]}"
    _check_off(monkeypatch)
    assert (await client.post("/docs/import", headers=hd, json=_record(eid, bad))).status_code == 200
    urls = sorted({"/subscriptions" + r.path for r in routes._build_router().routes
                   if "GET" in r.methods and "{" not in r.path})
    assert {"/subscriptions", "/subscriptions/due"} <= set(urls)
    answers = {u: (await _get(client, u, hd)).status_code for u in urls}
    assert all(code < 500 for code in answers.values()), answers
    rows = {i.get("id") or i["entity_id"]: i for i in (await client.get("/subscriptions", headers=hd)).json()["items"]}
    assert rows[eid]["next_bill_date"] is None and rows[eid]["schedule_error"]
    assert rows[good]["next_bill_date"] == TODAY
    assert good in {i["entity_id"] for i in (await client.get("/subscriptions/due", headers=hd)).json()["items"]}
    periods = await _get(client, f"/subscriptions/{eid}/periods", hd)
    assert periods.status_code == 409 and periods.json()["detail"]["message_key"] == key
    cid = await company_id(client, hd)
    await tick(session, cid, TODAY)
    assert (await template(client, hd, good)).get("periods", {}).get("0", {}).get("state") == "billed"
    problems = await migration.check_template_schedule_integrity(session, uuid.UUID(cid))
    assert any(eid in line for line in problems), problems
    await migration.reconcile_restored_company_schedules(session=session, company_id=uuid.UUID(cid))


async def test_a_let_go_bill_of_an_agreement_without_a_schedule_claims_its_period_back(client, session, monkeypatch):
    """Unvoid of a bill a period let go, once its agreement's stored terms build no schedule: the
    claim needs no schedule, so Unvoid still undoes the Reopen, never a refusal naming terms the
    agreement's recorded periods keep from changing, and never a server error."""
    from celerp_subscriptions import billing

    freeze_day(monkeypatch, "2026-03-01")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-03-01")
    first = (await generate_due(client, hd, eid)).json()
    assert (await client.post(f"/docs/{first['doc_id']}/void", headers=hd, json={"reason": "x", **_key()})).status_code == 200
    assert (await client.post(f"/subscriptions/{eid}/periods/0/reopen", headers=hd)).status_code == 200
    _check_off(monkeypatch)
    cid = uuid.UUID(await company_id(client, hd))
    await billing.locked_company(session, cid)
    await billing.write_agreement(session, cid, eid, {"frequency": "custom"}, None)
    await session.commit()
    try:
        r = await client.post(f"/docs/{first['doc_id']}/unvoid", headers=hd, json=_key())
    except Exception as exc:
        pytest.fail(f"unhandled {type(exc).__name__}: {exc}")
    assert r.status_code == 200, r.text
    stored = await _stored(session, eid)
    assert (stored["periods"]["0"]["doc_id"], stored["released_bills"]) == (first["doc_id"], {})


async def test_no_documents_route_moves_an_agreement_out_of_its_statuses(client, session, monkeypatch):
    """Every Documents route that writes a status (the census's), pressed on an active manual
    agreement, leaves it a status an agreement holds."""
    from celerp_subscriptions.billing import AGREEMENT_STATUSES
    from consent_census import status_routes

    freeze_day(monkeypatch, TODAY)
    hd = await register(client)
    labels = [label.split()[:2] for label in status_routes()]
    labels = [(method, path) for method, path in labels if path.startswith("/{entity_id}") and path.count("{") == 1]
    assert ("POST", "/{entity_id}/finalize") in labels
    body = {"status": "final", "amount": 10, "method": "cash", "date": TODAY, "reason": "x", "sent_via": "manual"}
    left = {}
    for method, path in labels:
        eid = await agreement(client, hd, start=TODAY)
        url = "/docs" + path.replace("{entity_id}", eid)
        try:
            await client.request(method, url, headers=hd, json={**body, **_key()})
        except Exception:
            pass
        status = (await template(client, hd, eid)).get("status")
        if status not in AGREEMENT_STATUSES:
            left[f"{method} {path}"] = status
    assert not left, left


async def test_catch_up_names_why_a_period_was_refused_and_which(client, session, monkeypatch):
    """Two agreements held back by automatic billing; one is paused after the list was read.
    Its periods are refused saying it is paused, each naming its dates, and the other bills."""
    from bs4 import BeautifulSoup
    from fasthtml.common import to_xml
    from ui.i18n import sentence, t
    from ui.routes.subscriptions import _outcome_table

    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    held = []
    for _ in range(2):
        eid = await agreement(client, hd, start="2026-01-01")
        assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=1)).status_code == 200
        held.append(eid)
    d, paused = held
    listed = {i["entity_id"]: i["periods"] for i in (await client.get("/subscriptions/catch-up", headers=hd)).json()["items"]}
    assert set(held) <= set(listed)
    assert (await client.post(f"/subscriptions/{paused}/pause", headers=hd)).status_code == 200
    items = [{"entity_id": d, "period_index": listed[d][0]["period_index"], "request_key": str(uuid.uuid4())},
             *({"entity_id": paused, "period_index": p["period_index"], "request_key": str(uuid.uuid4())} for p in listed[paused])]
    results = (await client.post("/subscriptions/catch-up/run", headers=hd, json={"items": items})).json()["results"]
    assert results[0]["ok"], results[0]
    refused = results[1:]
    assert [r["error"] for r in refused] == [t("subscriptions.err_bill_paused")] * len(listed[paused])
    assert [(r["service_start"], r["service_end"]) for r in refused] == [
        (p["service_start"], p["service_end"]) for p in listed[paused]]
    text = BeautifulSoup(to_xml(_outcome_table(results, {})), "html.parser").get_text()
    for r in refused:
        assert sentence("subscriptions.done_refused", start=r["service_start"], end=r["service_end"],
                        reason=t("subscriptions.err_bill_paused")) in text
