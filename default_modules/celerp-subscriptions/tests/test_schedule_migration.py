# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Existing templates move to agreement schedules without moving the date anyone sees.

Each old document is placed in the period that contains its date, counting back from the
old next date; a clean history gets its claims, anything else waits for a person in
history review with nothing guessed."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

from celerp_subscriptions.periods import shown
from agreement_kit import LINE, billed_docs, freeze_instant, generate_due, next_bill_date

pytestmark = pytest.mark.asyncio


def _today(monkeypatch, day: str) -> None:
    # The test company's timezone is UTC+14: midnight UTC is the afternoon of ``day`` there.
    freeze_instant(monkeypatch, datetime.fromisoformat(day).replace(tzinfo=timezone.utc))


async def _emit(session, cid, uid, entity_id, event_type, data):
    await emit_event(session, company_id=cid, entity_id=entity_id, entity_type="doc", event_type=event_type,
                     data=data, actor_id=uid, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))


async def _legacy(session, auth, *, next_run: str, doc_dates: list[str], frequency: str = "monthly",
                  start: str = "2025-01-01", missing: int = 0, status: str = "active") -> tuple[str, list[str]]:
    """A template as the previous release kept it: next_run_date and generated_doc_ids."""
    cid, uid = auth["company_id"], auth["user_id"]
    tid = f"doc:SUBT-{uuid.uuid4().hex[:8]}"
    docs = []
    for d in doc_dates:
        did = f"doc:INV-{uuid.uuid4().hex[:8]}"
        await _emit(session, cid, uid, did, "doc.created", {
            "doc_type": "invoice", "status": "final", "ref_id": did[4:], "issue_date": d, "subscription_id": tid,
            "contact_id": "contact:test-001", "line_items": [dict(LINE)], "total": 100.0, "currency": "USD"})
        docs.append(did)
    docs += [f"doc:GONE-{uuid.uuid4().hex[:8]}" for _ in range(missing)]
    await _emit(session, cid, uid, tid, "doc.created", {
        "doc_type": "subscription_invoice", "status": status, "ref_id": tid[4:], "frequency": frequency,
        "start_date": start, "next_run_date": next_run, "generated_doc_ids": docs,
        "contact_id": "contact:test-001", "line_items": [dict(LINE)], "currency": "USD"})
    await session.commit()
    return tid, docs


async def _state(session, cid, tid) -> dict:
    session.expire_all()
    return (await session.get(Projection, {"company_id": cid, "entity_id": tid})).state


async def _migrate(session, cid):
    from celerp_subscriptions.migration import migrate_company

    result = await migrate_company(session, cid)
    await session.commit()
    return result


async def test_migration_keeps_visible_next_date(client, session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    clean, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-01-01", "2026-02-01", "2026-03-01"])
    empty, _ = await _legacy(session, auth, next_run="2026-05-10", doc_dates=[])
    weekly, _ = await _legacy(session, auth, next_run="2026-03-16", frequency="weekly",
                              doc_dates=["2026-03-02", "2026-03-09"])
    await _migrate(session, auth["company_id"])
    hd = auth["headers"]
    assert [await next_bill_date(client, hd, t) for t in (clean, empty, weekly)] == ["2026-04-01", "2026-05-10", "2026-03-16"]


async def test_migration_clean_history_gets_claims(session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    tid, docs = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-01-01", "2026-02-03", "2026-03-01"])
    await _migrate(session, auth["company_id"])
    state = await _state(session, auth["company_id"], tid)
    assert {k: (v["state"], v["doc_id"]) for k, v in state["periods"].items()} == {
        "-3": ("billed", docs[0]), "-2": ("billed", docs[1]), "-1": ("billed", docs[2])}
    assert state["anchor_date"] == "2026-04-01" and shown("billing_timing", state.get("billing_timing")) == "advance"
    assert "next_run_date" not in state and "generated_doc_ids" not in state
    assert not state.get("history_review")


async def test_migration_drifted_doubled_gapped_deleted_flagged(session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    cases = {
        "doubled": await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-02-01", "2026-02-15", "2026-03-01"]),
        "gapped": await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-01-01", "2026-03-01"]),
        "deleted": await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"], missing=1),
        "drifted": await _legacy(session, auth, next_run="2026-03-01", doc_dates=["2026-02-01", "2026-03-05"]),
        "before_term": await _legacy(session, auth, next_run="2026-04-01", start="2026-02-01",
                                     doc_dates=["2026-01-15", "2026-02-01", "2026-03-01"]),
    }
    await _migrate(session, auth["company_id"])
    for name, (tid, docs) in cases.items():
        state = await _state(session, auth["company_id"], tid)
        review = {r["doc_id"]: r for r in state.get("history_review") or []}
        assert set(review) == set(docs), name
        assert all("candidates" in r for r in review.values()), name
        assert not any(v["state"] == "billed" for v in (state.get("periods") or {}).values()), name
    doubled = {r["doc_id"]: r["candidates"] for r in (await _state(session, auth["company_id"], cases["doubled"][0]))["history_review"]}
    assert doubled[cases["doubled"][1][0]] == doubled[cases["doubled"][1][1]] == [-2]


async def test_migration_all_templates_manual_due(session, auth, monkeypatch):
    from celerp_subscriptions.billing import run_due

    _today(monkeypatch, "2026-06-15")
    a, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    b, _ = await _legacy(session, auth, next_run="2026-02-01", doc_dates=[])
    await _migrate(session, auth["company_id"])
    for tid in (a, b):
        assert shown("generation_mode", (await _state(session, auth["company_id"], tid)).get("generation_mode")) == "manual_due"
    report = await run_due(session=session, company_id=auth["company_id"], today=date(2026, 6, 15), budget=10)
    await session.commit()
    assert report.done == 0
    assert await billed_docs(session, a) == [] and await billed_docs(session, b) == []


async def _events(session, cid) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()


async def test_migration_idempotent_marker_and_staged_company_skipped(session, auth, monkeypatch):
    from celerp_subscriptions import migration

    _today(monkeypatch, "2026-03-15")
    cid = auth["company_id"]
    good, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    bad, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    real = migration._migrate_template

    async def _flaky(session, company_id, proj, *a, **kw):
        if proj.entity_id == bad:
            raise RuntimeError("broken on purpose")
        return await real(session, company_id, proj, *a, **kw)

    monkeypatch.setattr(migration, "_migrate_template", _flaky)
    first = await _migrate(session, cid)
    assert first["failed"] == 1 and not first["marked"]
    assert "anchor_date" in await _state(session, cid, good)
    assert "next_run_date" in await _state(session, cid, bad)
    monkeypatch.setattr(migration, "_migrate_template", real)
    second = await _migrate(session, cid)
    assert second["marked"] and "anchor_date" in await _state(session, cid, bad)
    before = await _events(session, cid)
    third = await _migrate(session, cid)
    assert third["marked"] and third["migrated"] == 0 and await _events(session, cid) == before

    other = uuid.uuid4()
    from test_helpers import company_auth
    staged_auth = await company_auth(session, other, uuid.uuid4())
    staged, _ = await _legacy(session, staged_auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    (await session.get(Company, other)).is_migration_staged = True
    await session.commit()
    assert await _migrate(session, other) is None
    assert "next_run_date" in await _state(session, other, staged)


async def test_check_template_schedule_integrity(session, auth, monkeypatch):
    from celerp_subscriptions.migration import check_template_schedule_integrity

    _today(monkeypatch, "2026-03-15")
    cid = auth["company_id"]
    first, docs = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-02-01", "2026-03-01"])
    second, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=[])
    await _migrate(session, cid)
    assert await check_template_schedule_integrity(session, cid) == []
    await _emit(session, cid, auth["user_id"], second, "doc.updated", {"fields_changed": {"periods": {"new": {
        "-1": {"state": "billed", "doc_id": docs[1], "release_count": 0}}}}})
    await session.commit()
    problems = await check_template_schedule_integrity(session, cid)
    assert problems and any(docs[1] in p for p in problems)


async def test_migration_period_universe_lower_bound_keeps_next_date(client, session, auth, monkeypatch):
    from celerp_subscriptions.periods import Schedule

    _today(monkeypatch, "2026-01-20")
    fixtures = {
        "never_generated": await _legacy(session, auth, next_run="2026-02-01", start="2025-06-01", doc_dates=[]),
        "generated_late": await _legacy(session, auth, next_run="2026-01-01", doc_dates=["2025-11-05", "2025-12-09"]),
        "generated_early": await _legacy(session, auth, next_run="2026-01-01", doc_dates=["2025-11-28", "2025-12-28"]),
        "paused_resumed": await _legacy(session, auth, next_run="2026-01-01", doc_dates=["2025-09-01", "2025-12-01"]),
    }
    old = {"never_generated": "2026-02-01", "generated_late": "2026-01-01", "generated_early": "2026-01-01"}
    await _migrate(session, auth["company_id"])
    for name, (tid, _) in fixtures.items():
        state = await _state(session, auth["company_id"], tid)
        if name == "paused_resumed":
            assert state.get("history_review"), name
            continue
        s = Schedule.from_state(state)
        keys = [int(k) for k in state.get("periods") or {}]
        assert s.lowest == (min(keys) if keys else 0), name
        assert not any(s.exists(k) for k in range(s.lowest - 24, s.lowest)), name
        assert s.next_bill_date() == date.fromisoformat(old[name]), name
        assert await next_bill_date(client, auth["headers"], tid) == old[name], name
    hd = auth["headers"]
    assert (await generate_due(client, hd, fixtures["never_generated"][0])).status_code == 409  # nothing due that was not due
    assert (await generate_due(client, hd, fixtures["generated_late"][0])).json()["period_index"] == 0


async def test_history_confirm_and_withdraw(client, session, auth, monkeypatch):
    _today(monkeypatch, "2026-04-15")
    hd = auth["headers"]
    tid, (jan, mar) = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-01-01", "2026-03-01"])
    await _migrate(session, auth["company_id"])

    def confirm(doc, k):
        return client.post(f"/subscriptions/{tid}/history/{doc}/confirm", headers=hd, json={"period_index": k})

    assert (await confirm(mar, -2)).status_code == 409  # outside the document's candidates
    assert (await confirm("doc:UNLISTED", -2)).status_code == 409
    assert (await confirm(jan, -3)).status_code == 200
    assert (await confirm(jan, -3)).status_code == 200  # a repeat returns the state it is in
    assert (await confirm(mar, -3)).status_code == 409  # that period is claimed
    assert (await confirm(mar, -1)).status_code == 200
    state = await _state(session, auth["company_id"], tid)
    assert not state.get("history_review")
    assert state["periods"]["-3"]["doc_id"] == jan and state["periods"]["-1"]["doc_id"] == mar
    w = await client.post(f"/subscriptions/{tid}/history/{mar}/withdraw", headers=hd)
    assert w.status_code == 200
    state = await _state(session, auth["company_id"], tid)
    assert state["periods"]["-1"]["state"] == "unresolved" and mar in [r["doc_id"] for r in state["history_review"]]
    assert (await confirm(mar, -1)).status_code == 200
    assert (await client.post(f"/subscriptions/{tid}/periods/-2/skip", headers=hd, json={"reason": "Not billed"})).status_code == 200
    assert (await generate_due(client, hd, tid)).json()["period_index"] == 0
    late = await client.post(f"/subscriptions/{tid}/history/{mar}/withdraw", headers=hd)
    assert late.status_code == 409


async def test_history_set_aside_clears_documents_no_period_can_take(client, session, auth, monkeypatch):
    """A deleted document and the second of two in one period cannot be placed; setting each
    aside lets review finish, and withdrawing puts it back."""
    _today(monkeypatch, "2026-04-15")
    hd = auth["headers"]
    cid = auth["company_id"]
    tid, (mar, gone) = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"], missing=1)
    dtid, (feb1, feb15, dmar) = await _legacy(session, auth, next_run="2026-04-01",
                                              doc_dates=["2026-02-01", "2026-02-15", "2026-03-01"])
    await _migrate(session, cid)

    def act(t_id, doc, action, **body):
        return client.post(f"/subscriptions/{t_id}/history/{doc}/{action}", headers=hd, json=body or None)

    assert (await act(tid, "doc:UNLISTED", "set-aside")).status_code == 409
    assert (await act(tid, gone, "set-aside")).status_code == 200
    assert (await act(tid, gone, "set-aside")).status_code == 200  # a repeat returns the state it is in
    assert (await act(tid, mar, "confirm", period_index=-1)).status_code == 200
    state = await _state(session, cid, tid)
    assert not state.get("history_review")
    assert [r["doc_id"] for r in state["history_set_aside"]] == [gone]
    shown = (await client.get(f"/subscriptions/{tid}/periods", headers=hd)).json()
    assert [r["doc_id"] for r in shown["set_aside"]] == [gone]

    assert (await act(tid, gone, "withdraw")).status_code == 200
    state = await _state(session, cid, tid)
    assert [r["doc_id"] for r in state["history_review"]] == [gone] and not state.get("history_set_aside")
    assert (await act(tid, gone, "set-aside")).status_code == 200

    assert (await act(dtid, feb1, "confirm", period_index=-2)).status_code == 200
    assert (await act(dtid, feb15, "confirm", period_index=-2)).status_code == 409  # that period is claimed
    assert (await act(dtid, feb15, "set-aside")).status_code == 200
    assert (await act(dtid, dmar, "confirm", period_index=-1)).status_code == 200
    assert not (await _state(session, cid, dtid)).get("history_review")
    assert (await generate_due(client, hd, dtid)).json()["period_index"] == 0
    assert (await act(dtid, feb15, "withdraw")).status_code == 409  # a later period is billed

async def test_migration_reviews_shared_historical_document(session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    a, docs = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    b, other = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    await _emit(session, auth["company_id"], auth["user_id"], b, "doc.updated",
                {"fields_changed": {"generated_doc_ids": {"new": docs}}})
    await session.commit()
    await _migrate(session, auth["company_id"])
    for tid in (a, b):
        state = await _state(session, auth["company_id"], tid)
        assert state.get("history_review")
        assert not any(e.get("state") == "billed" for e in (state.get("periods") or {}).values())


async def test_migration_missing_anchor_keeps_legacy_state_for_repair(session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    tid, _ = await _legacy(session, auth, next_run="", start="", doc_dates=[])
    result = await _migrate(session, auth["company_id"])
    assert result["failed"] == 1 and not result["marked"]
    state = await _state(session, auth["company_id"], tid)
    assert "anchor_date" not in state and "next_run_date" in state

    from celerp.models.notification import Notification
    from ui.i18n import t
    title = t("subscriptions.notice_schedule_not_moved_title", "en")

    async def notices():
        return (await session.scalars(select(Notification.body).where(
            Notification.company_id == auth["company_id"], Notification.title == title,
        ))).all()

    assert len(bodies := await notices()) == 1 and "no next run date or start date" in bodies[0], bodies
    await _migrate(session, auth["company_id"])
    assert len(await notices()) == 1

async def test_history_confirmation_refuses_another_agreements_document(client, session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    first, docs = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    second, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    await _emit(session, auth["company_id"], auth["user_id"], second, "doc.updated",
                {"fields_changed": {"generated_doc_ids": {"new": docs}}})
    await session.commit()
    await _migrate(session, auth["company_id"])
    hd = auth["headers"]
    url = lambda eid: f"/subscriptions/{eid}/history/{docs[0]}/confirm"
    assert (await client.post(url(first), headers=hd, json={"period_index": -1})).status_code == 200
    assert (await client.post(url(second), headers=hd, json={"period_index": -1})).status_code == 409

async def test_restored_automation_requires_new_consent(session, auth, monkeypatch):
    from celerp.modules.slots import fire_lifecycle
    from celerp_subscriptions import billing

    _today(monkeypatch, "2026-03-15")
    tid = f"doc:SUBT-{uuid.uuid4().hex[:8]}"
    await _emit(session, auth["company_id"], auth["user_id"], tid, "doc.created", {
        "doc_type": "subscription_invoice", "status": "draft", "ref_id": tid[4:],
        "frequency": "monthly", "start_date": "2026-03-01", "anchor_date": "2026-03-01",
        "contact_id": "contact:test-001", "line_items": [dict(LINE)], "currency": "USD"})
    # Consent is recorded by the write that chooses the automatic terms; Activate keeps it.
    await billing.write_agreement(session, auth["company_id"], tid,
                                  {"generation_mode": "auto_invoice", "auto_send": True}, auth["user_id"])
    await billing.write_agreement(session, auth["company_id"], tid, {"status": "active"}, auth["user_id"])
    await session.commit()
    assert set((await _state(session, auth["company_id"], tid))["consents"]) == {"generation", "sending"}
    failures = await fire_lifecycle("on_company_restored", session=session, company_id=auth["company_id"])
    assert not failures, failures
    await session.commit()
    state = await _state(session, auth["company_id"], tid)
    assert state["generation_mode"] == "manual_due" and state["auto_send"] is False
    assert "consents" not in state

    # Re-enabling an already-running module reconciles its data, without resetting
    # the freshly authorized schedule of a company that was never restored.
    from celerp_subscriptions.migration import reconcile_company_schedules
    await reconcile_company_schedules(session=session, company_id=auth["company_id"])
    await session.commit()
    assert (await _state(session, auth["company_id"], tid)) == state


async def test_restored_company_hook_reconciles_legacy_schedule_before_commit(session, auth, monkeypatch):
    from celerp.modules.slots import fire_lifecycle

    _today(monkeypatch, "2026-03-15")
    tid, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    assert (await _state(session, auth["company_id"], tid)).get("anchor_date") is None
    failures = await fire_lifecycle("on_company_restored", session=session, company_id=auth["company_id"])
    assert not failures, failures
    await session.commit()
    state = await _state(session, auth["company_id"], tid)
    assert state["anchor_date"] == "2026-04-01"
    assert "next_run_date" not in state

    from celerp.migrations._data_reconcile import get_meta
    from celerp_subscriptions.migration import _MARKER, migrate_company

    marker = _MARKER.format(auth["company_id"])
    conn = await session.connection()
    assert await conn.run_sync(lambda c: get_meta(c, marker)) is None
    before = await _events(session, auth["company_id"])
    result = await migrate_company(session, auth["company_id"])
    await session.commit()
    assert result["marked"] and result["migrated"] == 0
    assert await _events(session, auth["company_id"]) == before
    conn = await session.connection()
    assert await conn.run_sync(lambda c: get_meta(c, marker)) == "done"


@pytest.mark.parametrize("days", ["14", "", "x", 0, 40000])
async def test_an_interval_a_schedule_that_is_not_custom_never_read_does_not_stop_the_move(days):
    """An earlier release kept whatever the interval box held when the frequency changed from
    custom; a monthly template's interval is never read, so the move keeps it only when it holds
    and the moved schedule builds."""
    from celerp_subscriptions.migration import legacy_schedule
    from celerp_subscriptions.periods import Schedule

    for status in ("active", "draft"):
        state = {"status": status, "frequency": "monthly", "custom_interval_days": days, "start_date": "2026-01-01",
                 "next_run_date": "2026-02-01"}
        moved = {**state, **legacy_schedule(state), "periods": {}}
        assert Schedule.from_state(moved).frequency == "monthly"
        assert moved["custom_interval_days"] in (14, None, "")


@pytest.mark.parametrize("change", [
    {"doc_type": "quotation"}, "void", {"subscription_id": "doc:SUBT-another"},
], ids=["not-an-invoice", "void", "another-agreement"])
async def test_history_confirmation_refuses_a_document_that_stopped_being_a_candidate(
        client, session, auth, monkeypatch, change):
    """Review listed the document when the template moved; by the time a person confirms it,
    it became another kind of document, was voided or was taken by another agreement."""
    _today(monkeypatch, "2026-04-15")
    tid, (_, mar) = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-01-01", "2026-03-01"])
    await _migrate(session, auth["company_id"])
    assert mar in [r["doc_id"] for r in (await _state(session, auth["company_id"], tid))["history_review"]]
    if change == "void":
        voided = await client.post(f"/docs/{mar}/void", headers=auth["headers"], json={"reason": "Wrong customer"})
        assert voided.status_code == 200, voided.text
    else:
        await _emit(session, auth["company_id"], auth["user_id"], mar, "doc.updated",
                    {"fields_changed": {f: {"new": v} for f, v in change.items()}})
        await session.commit()
    r = await client.post(f"/subscriptions/{tid}/history/{mar}/confirm", headers=auth["headers"],
                          json={"period_index": -1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_history_not_candidate"


async def test_a_shared_document_with_no_owner_is_confirmed_on_one_agreement_only(client, session, auth, monkeypatch):
    """Two templates listed one document and it names neither: the first to confirm it claims
    it, and the other is refused because a period of the first now bills it."""
    _today(monkeypatch, "2026-03-15")
    first, docs = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    second, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    await _emit(session, auth["company_id"], auth["user_id"], second, "doc.updated",
                {"fields_changed": {"generated_doc_ids": {"new": docs}}})
    await _emit(session, auth["company_id"], auth["user_id"], docs[0], "doc.updated",
                {"fields_changed": {"subscription_id": {"new": None}}})
    await session.commit()
    await _migrate(session, auth["company_id"])
    url = lambda eid: f"/subscriptions/{eid}/history/{docs[0]}/confirm"
    assert (await client.post(url(first), headers=auth["headers"], json={"period_index": -1})).status_code == 200
    r = await client.post(url(second), headers=auth["headers"], json={"period_index": -1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "subscriptions.err_history_not_candidate"


async def test_a_document_with_an_unreadable_date_waits_for_review_and_the_template_moves(session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    tid, (doc,) = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["not a date"])
    result = await _migrate(session, auth["company_id"])
    assert result["failed"] == 0 and result["review"] == 1, result
    state = await _state(session, auth["company_id"], tid)
    assert state["anchor_date"]
    assert [r["doc_id"] for r in state["history_review"]] == [doc]


async def test_a_company_whose_move_fails_does_not_stop_the_others(session, auth, monkeypatch):
    from celerp_subscriptions import migration

    _today(monkeypatch, "2026-03-15")
    tid, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    broken = uuid.uuid4()
    session.add(Company(id=broken, name="Another company", slug=f"another-{broken.hex[:8]}"))
    await session.commit()
    real, tried = migration.migrate_company, []

    async def one_company_breaks(session, cid):
        # Other tests' companies share the database: only these two are moved here.
        if cid == broken:
            tried.append(cid)
            raise RuntimeError("synthetic failure moving one company")
        return await real(session, cid) if cid == auth["company_id"] else None

    monkeypatch.setattr(migration, "migrate_company", one_company_breaks)
    await migration.migrate_schedules_hook(session=session)
    await session.commit()
    assert tried == [broken]
    assert "anchor_date" in await _state(session, auth["company_id"], tid)


async def test_a_template_whose_terms_build_no_schedule_says_why_when_its_page_moves_it(client, session, auth, monkeypatch):
    """Choosing a frequency on a template that cannot move yet (no next date, no start date)
    is refused naming the template and why, and leaves it as it was."""
    _today(monkeypatch, "2026-03-15")
    tid, _ = await _legacy(session, auth, next_run="", start="", doc_dates=[])
    r = await client.patch(f"/subscriptions/{tid}/schedule", headers=auth["headers"], json={"frequency": "weekly"})
    assert r.status_code == 409, r.text
    state = await _state(session, auth["company_id"], tid)
    assert state["frequency"] == "monthly" and "anchor_date" not in state


async def test_a_template_moves_onto_a_schedule_when_its_page_sets_the_frequency(client, session, auth, monkeypatch):
    _today(monkeypatch, "2026-03-15")
    tid, _ = await _legacy(session, auth, next_run="2026-04-01", doc_dates=["2026-03-01"])
    r = await client.patch(f"/subscriptions/{tid}/schedule", headers=auth["headers"], json={"frequency": "monthly"})
    assert r.status_code == 200, r.text
    assert r.json()["next_bill_date"] == "2026-04-01"
    state = await _state(session, auth["company_id"], tid)
    assert state["anchor_date"] and "next_run_date" not in state
