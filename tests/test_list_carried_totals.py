# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A list's totals are written with the event that sets them, so replaying its history gives the
totals the list was saved with. A quotation an earlier version saved by its rule (tax on each line
before the header discount) keeps them through a restore check and a rebuild, is never re-totalled
by a write that changes no money, and is converted only after the user recalculates it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from ui.i18n import t

_VAT10 = {"code": "VAT 10%", "rate": 10.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}
_TOTALS = ("subtotal", "discount_amount", "tax_amount", "total")


async def _quotation(client, session, *, finalize: bool = True):
    from test_tax_acceptance_matrix import _demo_company

    auth, contacts = await _demo_company(client, session, "USD")
    customer = next(c for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "contact_id": customer["id"],
        "discount": 10, "discount_type": "flat",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "tax_rate": 10,
                        "taxes": [_VAT10]}]})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    if finalize:
        r = await client.post(f"/lists/{lid}/finalize", headers=auth["headers"], json={})
        assert r.status_code == 200, r.text
    return auth, lid


async def _as_saved_by_earlier_version(session, company_id, lid) -> dict:
    """Make the list's history and record what an earlier version wrote: its events carry no
    totals, and its record holds the totals that version's rule gave (tax 10.00 on the line
    before the 10.00 discount, total 100.00)."""
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == lid))).scalars().all()
    for row in rows:
        if "totals" in (row.data or {}):
            row.data = {k: v for k, v in row.data.items() if k != "totals"}
    proj = await session.get(Projection, (company_id, lid))
    proj.state = {**proj.state, "subtotal": 100.0, "discount_amount": 10.0, "tax_amount": 10.0, "total": 100.0}
    await session.commit()
    return proj.state


async def _rebuild(session, company_id) -> None:
    """What Rebuild (POST /ledger/rebuild) runs: every record replayed from the ledger."""
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    await lock_company(session, company_id)
    await ProjectionEngine.rebuild(session, company_id=company_id)
    await session.commit()


async def _state(session, company_id, lid) -> dict:
    session.expire_all()
    return (await session.get(Projection, (company_id, lid))).state


async def test_new_list_events_carry_the_totals_they_give(client, session):
    auth, lid = await _quotation(client, session, finalize=False)
    state = await _state(session, auth["company_id"], lid)
    (created,) = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == lid, LedgerEntry.event_type == "list.created"))).scalars().all()
    assert created.data["totals"] == {k: state[k] for k in _TOTALS}
    assert state["total"] == 99.0 and "totals" not in state
    assert created.data["line_items"][0]["line_total"] == 100.0


@pytest.mark.parametrize("event_type, data", [
    ("list.created", {"list_type": "quotation", "line_items": []}),
    ("list.updated", {"fields_changed": {"notes": {"old": None, "new": "x"}}}),
])
def test_a_list_event_without_its_totals_is_refused_at_write(event_type, data):
    """emit_event stamps every list write with its totals; an event that reached the schema
    unstamped is refused."""
    with pytest.raises(ValidationError):
        EVENT_SCHEMA_MAP[event_type](**data)


async def test_a_list_write_from_outside_the_documents_module_carries_its_totals(client, session):
    """A writer that knows nothing of totals (a contact merge re-pointing the list, a line given
    its id) emits through emit_event, which stamps the write."""
    from celerp.events.engine import emit_event

    auth, lid = await _quotation(client, session, finalize=False)
    cid = auth["company_id"]
    held = {k: (await _state(session, cid, lid))[k] for k in _TOTALS}
    entry = await emit_event(
        session, company_id=cid, entity_id=lid, entity_type="list", event_type="list.updated",
        data={"fields_changed": {"contact_name": {"old": None, "new": "Renamed"}}},
        actor_id=None, location_id=None, source="test", idempotency_key=f"outside:{lid}", metadata_={})
    await session.commit()
    assert entry.data["totals"] == held


async def test_earlier_version_quotation_keeps_its_totals_through_restore_check_and_rebuild(client, session):
    from celerp.services.company_backup import _check_replay

    auth, lid = await _quotation(client, session)
    cid = auth["company_id"]
    earlier = await _as_saved_by_earlier_version(session, cid, lid)

    await _check_replay(session, cid)  # the restore's replay agrees with the record: no refusal
    await _rebuild(session, cid)
    after = await _state(session, cid, lid)
    assert {k: after[k] for k in _TOTALS} == {k: earlier[k] for k in _TOTALS}


async def test_a_write_that_changes_no_money_keeps_the_stored_totals(client, session):
    auth, lid = await _quotation(client, session, finalize=False)
    cid = auth["company_id"]
    await _as_saved_by_earlier_version(session, cid, lid)
    state = await _state(session, cid, lid)
    r = await client.patch(f"/lists/{lid}", headers=auth["headers"], json={
        "fields_changed": {"notes": {"old": None, "new": "Saved again"}}})
    assert r.status_code == 200, r.text
    assert (await _state(session, cid, lid))["total"] == 100.0

    lines = [{**state["line_items"][0], "quantity": 2, "line_total": 200.0}]
    r = await client.patch(f"/lists/{lid}", headers=auth["headers"], json={
        "fields_changed": {"line_items": {"old": state["line_items"], "new": lines}},
        "expected_version": r.json()["version"]})
    assert r.status_code == 200, r.text
    after = await _state(session, cid, lid)
    assert (after["subtotal"], after["tax_amount"], after["total"]) == (200.0, 19.0, 209.0)
    await _rebuild(session, cid)
    assert (await _state(session, cid, lid))["total"] == 209.0


async def test_convert_refuses_an_earlier_version_total_until_it_is_recalculated(client, session):
    auth, lid = await _quotation(client, session)
    cid, h = auth["company_id"], auth["headers"]
    await _as_saved_by_earlier_version(session, cid, lid)

    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "list_old_rule_total"
    assert detail["message_key"] == "documents.err_list_old_rule_total"
    assert detail["params"] == {"stored": "100.00", "current": "99.00"}

    r = await client.post(f"/lists/{lid}/recalculate-total", headers=h)
    assert r.status_code == 200, r.text
    state = await _state(session, cid, lid)
    assert state["total"] == 99.0 and state["totals_before_recalc"]["totals"]["total"] == 100.0
    r = await client.post(f"/lists/{lid}/restore-total", headers=h)
    assert r.status_code == 200, r.text
    state = await _state(session, cid, lid)
    assert state["total"] == 100.0 and "totals_before_recalc" not in state
    await _rebuild(session, cid)
    assert (await _state(session, cid, lid))["total"] == 100.0

    assert (await client.post(f"/lists/{lid}/recalculate-total", headers=h)).status_code == 200
    events = [e.event_type for e in (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == lid).order_by(LedgerEntry.id))).scalars().all()]
    assert events[-3:] == ["list.totals_recalculated", "list.totals_restored", "list.totals_recalculated"]
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("ending", ["convert", "void"])
async def test_a_completed_or_void_list_keeps_its_total(client, session, ending):
    """Recalculate and Restore are refused, saying why, once a list is completed or void: a
    converted quotation keeps the total of the invoice made from it."""
    auth, lid = await _quotation(client, session)
    cid, h = auth["company_id"], auth["headers"]
    await _as_saved_by_earlier_version(session, cid, lid)
    assert (await client.post(f"/lists/{lid}/recalculate-total", headers=h)).status_code == 200
    if ending == "convert":
        r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    else:
        r = await client.post(f"/lists/{lid}/void", headers=h)
    assert r.status_code == 200, r.text
    before = await _state(session, cid, lid)
    for action in ("recalculate-total", "restore-total"):
        r = await client.post(f"/lists/{lid}/{action}", headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == t("documents.err_list_total_status")
    assert {k: (await _state(session, cid, lid))[k] for k in _TOTALS} == {k: before[k] for k in _TOTALS}


async def test_recalculate_and_restore_leave_a_list_that_agrees_as_it_is(client, session):
    """Both are safe to repeat: nothing to recalculate or restore writes nothing."""
    auth, lid = await _quotation(client, session)
    for action in ("recalculate-total", "restore-total"):
        r = await client.post(f"/lists/{lid}/{action}", headers=auth["headers"])
        assert r.status_code == 200 and r.json() == {"event_id": None}, r.text


async def test_a_copy_of_a_recalculated_list_takes_no_restore_point(client, session):
    """The previous total a recalculated quotation can restore belongs to that quotation: its
    duplicate and the invoice made from it start without one, so Restore previous total never
    puts the original's lines and earlier total on another document."""
    auth, lid = await _quotation(client, session)
    cid, h = auth["company_id"], auth["headers"]
    await _as_saved_by_earlier_version(session, cid, lid)
    assert (await client.post(f"/lists/{lid}/recalculate-total", headers=h)).status_code == 200

    r = await client.post(f"/lists/{lid}/duplicate", headers=h)
    assert r.status_code == 200, r.text
    dup = r.json()["id"]
    assert "totals_before_recalc" not in await _state(session, cid, dup)
    r = await client.post(f"/lists/{dup}/restore-total", headers=h)
    assert r.status_code == 200 and r.json() == {"event_id": None}, r.text
    assert (await _state(session, cid, dup))["total"] == 99.0

    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    assert "totals_before_recalc" not in await _state(session, cid, r.json()["target_doc_id"])

    await _rebuild(session, cid)
    assert "totals_before_recalc" not in await _state(session, cid, dup)
    assert "totals_before_recalc" not in await _state(session, cid, r.json()["target_doc_id"])
