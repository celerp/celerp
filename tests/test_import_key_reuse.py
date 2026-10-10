"""An import key names one record: sent again with other contents, it is refused.

Initial conditions per test: a fresh company with the seeded chart. A document or list is
imported with key K through /docs/import or /lists/import (single) or their batch routes.
Sending K again with the same record answers as the first call (single) or skips it
(batch); sending K with different contents, or for another record, is refused with
doc_import.key_reused (409 on the single routes, a refused row on the batch routes) and
changes nothing. With updating existing records on, the batch still updates (its own
toggle). An older import that recorded no request digest replays as before.
"""
from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from test_cost_restatement import _state

pytestmark = pytest.mark.asyncio

_KEY = "doc_import.key_reused"


def _doc(total=80.0, **extra) -> dict:
    return {"doc_type": "invoice", "status": "draft", "doc_number": f"K-{uuid.uuid4().hex[:6]}", "total": total,
            "line_items": [{"name": "Service", "quantity": 1, "unit_price": total, "line_total": total}], **extra}


def _list(total=0.0) -> dict:
    return {"ref_id": f"L-{uuid.uuid4().hex[:6]}", "status": "draft", "total": total, "line_items": []}


def _rec(kind: str, data: dict, eid: str | None = None, key: str | None = None) -> dict:
    return {"entity_id": eid or f"{kind}:{uuid.uuid4().hex[:10]}", "event_type": f"{kind}.created",
            "source": "csv", "idempotency_key": key or uuid.uuid4().hex, "data": data}


def _path(kind: str) -> str:
    return "/docs/import" if kind == "doc" else "/lists/import"


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_a_single_import_resent_with_other_contents_is_refused(client, session, auth, kind):
    data = _doc() if kind == "doc" else _list()
    rec = _rec(kind, data)
    assert (await client.post(_path(kind), headers=auth["headers"], json=rec)).status_code == 200
    before = await _state(session, auth, rec["entity_id"])
    changed = {**rec, "data": {**data, "total": 95.0, "notes": "changed"}}
    r = await client.post(_path(kind), headers=auth["headers"], json=changed)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == _KEY, r.text
    assert await _state(session, auth, rec["entity_id"]) == before


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_a_single_import_key_used_for_another_record_is_refused_with_the_key(client, session, auth, kind):
    data = _doc() if kind == "doc" else _list()
    rec = _rec(kind, data)
    assert (await client.post(_path(kind), headers=auth["headers"], json=rec)).status_code == 200
    other = _rec(kind, data, key=rec["idempotency_key"])
    r = await client.post(_path(kind), headers=auth["headers"], json=other)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == _KEY, r.text
    assert await _state(session, auth, other["entity_id"]) == {}


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_the_batch_refuses_a_row_resent_with_other_contents(client, session, auth, kind):
    data = _doc() if kind == "doc" else _list()
    rec = _rec(kind, data)
    path = _path(kind) + "/batch"
    assert (await client.post(path, headers=auth["headers"], json={"records": [rec]})).json()["created"] == 1
    before = await _state(session, auth, rec["entity_id"])
    changed = {**rec, "data": {**data, "notes": "changed"}}
    r = await client.post(path, headers=auth["headers"], json={"records": [changed]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0 and body["updated"] == 0 and len(body["errors"]) == 1, body
    assert "updating existing records" in body["errors"][0], body
    assert await _state(session, auth, rec["entity_id"]) == before


# Neighbouring rules: the same record resent replays (single) or skips (batch), the batch's
# update toggle still updates, and an older import with no recorded digest still replays.


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_the_same_record_resent_still_replays(client, session, auth, kind):
    rec = _rec(kind, _doc() if kind == "doc" else _list())
    first = await client.post(_path(kind), headers=auth["headers"], json=rec)
    again = await client.post(_path(kind), headers=auth["headers"], json=rec)
    assert again.status_code == 200 and again.json()["idempotency_hit"] is True, again.text
    assert again.json()["event_id"] == first.json()["event_id"]
    b = await client.post(_path(kind) + "/batch", headers=auth["headers"], json={"records": [rec]})
    assert (b.json()["skipped"], b.json()["errors"]) == (1, []), b.text


@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_the_batch_update_toggle_still_updates(client, session, auth, kind):
    data = _doc() if kind == "doc" else _list()
    rec = _rec(kind, data)
    path = _path(kind) + "/batch"
    assert (await client.post(path, headers=auth["headers"], json={"records": [rec]})).json()["created"] == 1
    changed = {**rec, "data": {**data, "notes": "changed"}}
    r = await client.post(path, headers=auth["headers"], json={"records": [changed], "upsert": True})
    assert r.json()["updated"] == 1, r.text
    assert (await _state(session, auth, rec["entity_id"])).get("notes") == "changed"


async def test_an_older_import_with_no_recorded_digest_still_replays(client, session, auth):
    rec = _rec("doc", _doc())
    await emit_event(session, company_id=auth["company_id"], entity_id=rec["entity_id"], entity_type="doc",
                     event_type="doc.created", data=rec["data"], actor_id=auth["user_id"], location_id=None,
                     source="csv", idempotency_key=rec["idempotency_key"])
    await session.commit()
    r = await client.post("/docs/import", headers=auth["headers"], json={**rec, "data": {**rec["data"], "total": 1.0}})
    assert r.status_code == 200 and r.json()["idempotency_hit"] is True, r.text
