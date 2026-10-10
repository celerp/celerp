"""A document is voided once, and unvoid reverses exactly that one void.

Initial conditions per test: a fresh company with the seeded chart; documents are
made through the API (a finalized service invoice, a finalized service bill, or a
credit note issued on a finalized service invoice). A void of a document that is
already void is refused with a message key and writes nothing; unvoid then brings
the document and the books back to where they stood before the first void.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.events.engine import emit_event
from celerp.models.ledger import LedgerEntry
from test_cost_restatement import _state
from test_credit_note_settlement_owned import _cn, _post, _svc_invoice
from test_set_aside_older_paths import _net

pytestmark = pytest.mark.asyncio

_CODES = ("1120", "4100", "2110", "5000", "6000")


async def _svc_bill(client, auth, total) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "bill", "contact_id": "supplier:1", "ref_id": f"B-{uuid.uuid4().hex[:6]}",
        "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}],
        "total": total})
    assert r.status_code == 200, r.text
    f = await _post(client, auth, f"/docs/{r.json()['id']}/finalize")
    assert f.status_code == 200, f.text
    return r.json()["id"]


async def _target(client, auth, kind) -> str:
    if kind == "invoice":
        return await _svc_invoice(client, auth, 100.0)
    if kind == "bill":
        return await _svc_bill(client, auth, 77.0)
    inv = await _svc_invoice(client, auth, 100.0)
    return await _cn(client, auth, inv, 30.0)


async def _books(session, auth) -> dict:
    return {c: await _net(session, auth, c, prefix="je:") for c in _CODES}


async def _events(session, auth) -> int:
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()


@pytest.mark.parametrize("kind", ["invoice", "bill", "credit_note"])
async def test_a_second_void_is_refused_and_writes_nothing(client, session, auth, kind):
    target = await _target(client, auth, kind)
    assert (await _post(client, auth, f"/docs/{target}/void")).status_code == 200
    n = await _events(session, auth)
    second = await _post(client, auth, f"/docs/{target}/void")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["message_key"] == "docs.void_already_void", second.text
    assert await _events(session, auth) == n


@pytest.mark.parametrize("kind", ["invoice", "bill", "credit_note"])
async def test_unvoid_after_a_refused_second_void_restores_the_document_and_books(client, session, auth, kind):
    target = await _target(client, auth, kind)
    before = await _books(session, auth)
    status_before = (await _state(session, auth, target)).get("status")
    assert (await _post(client, auth, f"/docs/{target}/void")).status_code == 200
    await _post(client, auth, f"/docs/{target}/void")
    u = await _post(client, auth, f"/docs/{target}/unvoid")
    assert u.status_code == 200, u.text
    assert (await _state(session, auth, target)).get("status") == status_before
    assert await _books(session, auth) == before


async def test_an_older_double_void_unvoids_to_the_status_before_the_first_void(client, session, auth):
    """An older release accepted a second void, recording the document's void status as
    the status to restore. Unvoid reverses one void: the document stands again as issued."""
    inv = await _svc_invoice(client, auth, 100.0)
    before = await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    await emit_event(session, company_id=auth["company_id"], entity_id=inv, entity_type="doc",
                     event_type="doc.voided", data={"pre_void_status": "void"}, actor_id=auth["user_id"],
                     location_id=None, source="api", idempotency_key=f"older-void-{uuid.uuid4().hex}")
    await session.commit()
    u = await _post(client, auth, f"/docs/{inv}/unvoid")
    assert u.status_code == 200, u.text
    assert (await _state(session, auth, inv)).get("status") == "final"
    assert await _books(session, auth) == before


# Neighbouring rules: a single void and unvoid still round-trip, a replay of the same
# void request still answers as the first call (E01), and unvoid of a live document is
# still refused.


async def test_one_void_and_unvoid_round_trip(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    before = await _books(session, auth)
    assert (await _post(client, auth, f"/docs/{inv}/void")).status_code == 200
    assert (await _state(session, auth, inv)).get("status") == "void"
    assert (await _books(session, auth))["1120"] == 0.0
    assert (await _post(client, auth, f"/docs/{inv}/unvoid")).status_code == 200
    assert (await _state(session, auth, inv)).get("status") == "final"
    assert await _books(session, auth) == before


async def test_a_replayed_void_answers_as_the_first_call(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    body = {"idempotency_key": f"void-{uuid.uuid4().hex}"}
    first = await _post(client, auth, f"/docs/{inv}/void", body)
    assert first.status_code == 200, first.text
    n = await _events(session, auth)
    again = await _post(client, auth, f"/docs/{inv}/void", body)
    assert again.status_code == 200 and again.json() == first.json()
    assert await _events(session, auth) == n


async def test_unvoid_of_a_live_document_is_refused(client, session, auth):
    inv = await _svc_invoice(client, auth, 100.0)
    r = await _post(client, auth, f"/docs/{inv}/unvoid")
    assert r.status_code == 409, r.text
