# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for the upsert=True toggle on batch import endpoints."""

from __future__ import annotations

import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
import ui.api_client as api_client

from test_helpers import make_authed_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup(session) -> tuple[uuid.UUID, uuid.UUID, str]:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session.add(Company(id=company_id, name="UpsertCo", slug=f"upsertco-{company_id.hex[:8]}"))
    session.add(User(
        id=user_id,
        email=f"admin-{user_id.hex[:8]}@test.co", name="Admin",
        auth_hash="x", is_active=True,
    ))
    # Flush parents before the membership row so Postgres' FK checks pass.
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role="admin", is_active=True))
    await session.commit()
    token = await make_authed_token(session, str(user_id), str(company_id), "admin")
    return company_id, user_id, token


# ---------------------------------------------------------------------------
# Items upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_items_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    payload = {"records": [record]}

    r1 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["created"] == 1
    assert r1.json()["skipped"] == 0
    assert r1.json()["updated"] == 0

    # Second call without upsert — should skip
    r2 = await client.post("/items/import/batch", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json()["created"] == 0
    assert r2.json()["skipped"] == 1
    assert r2.json()["updated"] == 0


@pytest.mark.asyncio
async def test_items_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:item:upsert-test-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "item.created",
        "data": {"sku": f"UPSK-{idem[-6:]}", "name": "Upsert Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    # First import creates
    r1 = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    # Second import with upsert=True — should update
    r2 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0

    # Third call with upsert=True — should skip (upsert key already exists)
    r3 = await client.post("/items/import/batch", headers=headers, json={"records": [record], "upsert": True})
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3["updated"] == 0
    assert body3["skipped"] == 1


@pytest.mark.asyncio
async def test_items_upsert_never_edits_another_item(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    first = {
        "entity_id": f"item:first-{uuid.uuid4().hex[:8]}",
        "event_type": "item.created",
        "data": {"sku": f"FIRST-{uuid.uuid4().hex[:6]}", "name": "First", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": f"csv:item:first-{uuid.uuid4().hex[:8]}",
    }
    assert (await client.post("/items/import/batch", headers=headers, json={"records": [first]})).json()["created"] == 1
    other = {**first, "entity_id": f"item:other-{uuid.uuid4().hex[:8]}", "data": {**first["data"], "name": "Other"}}
    r = await client.post("/items/import/batch", headers=headers, json={"records": [other], "upsert": True})
    assert r.status_code == 200 and r.json()["updated"] == 0 and r.json()["errors"], r.text
    assert (await client.get(f"/items/{first['entity_id']}", headers=headers)).json()["name"] == "First"


@pytest.mark.asyncio
async def test_items_import_rejects_comma_sku(client, session):
    """The comma-SKU invariant is enforced at the event/schema boundary, so bulk import cannot slip a
    comma-bearing SKU past the interactive 422: the row lands in `errors` and nothing is created. A
    comma is Celerp's OR operator, so a SKU that contained one could never be scanned back."""
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    record = {
        "entity_id": f"item:comma-{uuid.uuid4().hex[:8]}",
        "event_type": "item.created",
        "data": {"sku": "BAD,SKU", "name": "Comma Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": f"csv:item:comma-{uuid.uuid4().hex[:8]}",
    }
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 0
    assert body["errors"], "a comma SKU must be reported, not silently imported"
    assert any("comma" in str(e).lower() for e in body["errors"])


# ---------------------------------------------------------------------------
# Docs upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docs_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_docs_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"doc:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:doc:invoice:upd-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {"doc_type": "invoice", "doc_number": "UPD-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    changed = {**record, "data": {**record["data"], "notes": "updated through canonical patch"}}
    r2 = await client.post("/docs/import/batch", headers=headers, json={"records": [changed], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0

    # Exact replay is a no-op.
    r3 = await client.post("/docs/import/batch", headers=headers, json={"records": [changed], "upsert": True})
    assert r3.status_code == 200
    assert r3.json()["updated"] == 0
    assert r3.json()["skipped"] == 1


# ---------------------------------------------------------------------------
# Spreadsheet re-import through the import screens
# ---------------------------------------------------------------------------

class _Form:
    cookies: dict = {}

    def __init__(self, form: dict):
        self._form = form

    async def form(self):
        return self._form


class _Routes:
    def __init__(self):
        self.post_routes: dict = {}

    def get(self, path):
        return lambda fn: fn

    def post(self, path):
        def deco(fn):
            self.post_routes[path] = fn
            return fn
        return deco


def _numbered_via(client, headers):
    async def numbered_ids(_tok, resource, number, doc_type=None):
        params = {"number": number, **({"doc_type": doc_type} if doc_type else {})}
        r = await client.get(f"/{resource}/numbered", headers=headers, params=params)
        assert r.status_code == 200, r.text
        return r.json()["ids"]
    return numbered_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_reimporting_a_spreadsheet_updates_the_record_it_created(client, session, monkeypatch, resource):
    from ui.routes import docs_import, lists_import

    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    screen = docs_import if resource == "docs" else lists_import

    async def _batch(_tok, path, records, upsert=False):
        return (await client.post(path, headers=headers, json={"records": records, "upsert": upsert})).json()

    async def _search(_tok, params=None):
        return (await client.get(f"/{resource}", headers=headers, params=params or {})).json()

    monkeypatch.setattr(screen, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "batch_import", _batch)
    monkeypatch.setattr(api_client, "numbered_ids", _numbered_via(client, headers))
    routes = _Routes()
    screen.setup_routes(routes)
    confirm = routes.post_routes[f"/{resource}/import/confirm"]

    head, row = (("doc_type,doc_number,status,due_date", "invoice,RE-1,draft,{due}") if resource == "docs"
                 else ("ref_id,status,notes", "RE-1,draft,{due}"))
    await confirm(_Form({"csv_data": f"{head}\n{row.format(due='2026-01-31')}\n"}))
    await confirm(_Form({"csv_data": f"{head}\n{row.format(due='2026-02-28')}\n", "upsert": "1"}))

    rows = (await _search("tok", {"q": "RE-1"}))["items"]
    assert len(rows) == 1, rows
    record = (await client.get(f"/{resource}/{rows[0]['id']}", headers=headers)).json()
    assert record["due_date" if resource == "docs" else "notes"] == "2026-02-28"


async def _docs_import_screen(client, session, monkeypatch):
    from ui.routes import docs_import

    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    async def _batch(_tok, path, records, upsert=False):
        return (await client.post(path, headers=headers, json={"records": records, "upsert": upsert})).json()

    async def _search(_tok, params=None):
        return (await client.get("/docs", headers=headers, params=params or {})).json()

    monkeypatch.setattr(docs_import, "_token", lambda request: "tok")
    monkeypatch.setattr(docs_import.api, "batch_import", _batch)
    monkeypatch.setattr(docs_import.api, "numbered_ids", _numbered_via(client, headers))
    routes = _Routes()
    docs_import.setup_routes(routes)
    confirm = routes.post_routes["/docs/import/confirm"]

    async def numbered(number: str) -> list[dict]:
        rows = (await _search("tok", {"q": number}))["items"]
        return [(await client.get(f"/docs/{r['id']}", headers=headers)).json() for r in rows]

    return headers, confirm, numbered


@pytest.mark.asyncio
async def test_importing_a_document_made_in_the_app_updates_it(client, session, monkeypatch):
    headers, confirm, numbered = await _docs_import_screen(client, session, monkeypatch)
    r = await client.post("/docs", headers=headers, json={
        "doc_type": "quotation", "line_items": [{"name": "Service", "quantity": 1, "unit_price": 10.0}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    number = (await client.get(f"/docs/{doc_id}", headers=headers)).json()["ref_id"]

    await confirm(_Form({"csv_data": f"doc_type,doc_number,status,due_date\nquotation,{number},draft,2099-02-28\n",
                         "upsert": "1"}))
    docs = await numbered(number)
    assert [d["id"] for d in docs] == [doc_id]
    assert docs[0]["due_date"] == "2099-02-28"


@pytest.mark.asyncio
async def test_reimporting_with_the_type_written_differently_updates_the_same_document(client, session, monkeypatch):
    _, confirm, numbered = await _docs_import_screen(client, session, monkeypatch)
    await confirm(_Form({"csv_data": "doc_type,doc_number,status,due_date\ninvoice,RE-2,draft,2026-01-31\n"}))
    await confirm(_Form({"csv_data": "doc_type,doc_number,status,due_date\nInvoice,RE-2,draft,2026-02-28\n",
                         "upsert": "1"}))
    docs = await numbered("RE-2")
    assert [(d["doc_type"], d["due_date"]) for d in docs] == [("invoice", "2026-02-28")]


async def _seed_numbered(session, company_id, resource: str, number: str) -> str:
    """A record carrying ``number``, written straight to the ledger the way records made
    before numbers were checked on import could be."""
    from celerp.events.engine import emit_event

    entity_id = f"{'doc' if resource == 'docs' else 'list'}:{uuid.uuid4()}"
    data = ({"doc_type": "invoice", "doc_number": number, "status": "draft", "total": 0, "line_items": []}
            if resource == "docs" else {"ref_id": number, "status": "draft", "total": 0})
    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type=resource[:-1],
        event_type="doc.created" if resource == "docs" else "list.created", data=data,
        actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    return entity_id


async def _import_screen(client, session, monkeypatch, resource: str):
    from ui.routes import docs_import, lists_import

    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    screen = docs_import if resource == "docs" else lists_import
    sent: list[dict] = []

    async def _batch(_tok, path, records, upsert=False):
        sent.extend(records)
        return (await client.post(path, headers=headers, json={"records": records, "upsert": upsert})).json()

    monkeypatch.setattr(screen, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "batch_import", _batch)
    monkeypatch.setattr(api_client, "numbered_ids", _numbered_via(client, headers))
    routes = _Routes()
    screen.setup_routes(routes)
    return company_id, headers, routes.post_routes[f"/{resource}/import/confirm"], sent


def _csv(resource: str, number: str, value: str) -> str:
    if resource == "docs":
        return f"doc_type,doc_number,status,due_date\ninvoice,{number},draft,{value}\n"
    return f"ref_id,status,notes\n{number},draft,{value}\n"


async def _numbered_count(session, company_id, number: str) -> int:
    from sqlalchemy import func, or_, select

    from celerp.models.projections import Projection

    session.expire_all()
    return (await session.execute(select(func.count()).select_from(Projection).where(
        Projection.company_id == company_id,
        or_(Projection.state["doc_number"].as_string() == number,
            Projection.state["ref_id"].as_string() == number),
    ))).scalar()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_reimporting_a_number_two_records_share_is_an_error_row(client, session, monkeypatch, resource):
    from fasthtml.common import to_xml

    company_id, _, confirm, sent = await _import_screen(client, session, monkeypatch, resource)
    for _ in range(2):
        await _seed_numbered(session, company_id, resource, "AMB-1")

    panel = await confirm(_Form({"csv_data": _csv(resource, "AMB-1", "2099-02-28"), "upsert": "1"}))
    assert await _numbered_count(session, company_id, "AMB-1") == 2
    assert sent == []
    assert "AMB-1" in to_xml(panel)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_a_near_match_never_stands_in_for_the_number_imported(client, session, monkeypatch, resource):
    company_id, headers, confirm, _ = await _import_screen(client, session, monkeypatch, resource)
    near = await _seed_numbered(session, company_id, resource, "NM-10")

    await confirm(_Form({"csv_data": _csv(resource, "NM-1", "2099-02-28"), "upsert": "1"}))
    assert await _numbered_count(session, company_id, "NM-1") == 1
    record = (await client.get(f"/{resource}/{near}", headers=headers)).json()
    assert record.get("due_date" if resource == "docs" else "notes") in (None, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_importing_a_second_record_with_a_taken_number_is_refused(client, session, resource):
    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    kind = "doc" if resource == "docs" else "list"
    data = ({"doc_type": "invoice", "doc_number": "DUP-1", "status": "draft", "total": 0, "line_items": []}
            if resource == "docs" else {"ref_id": "DUP-1", "status": "draft", "total": 0})

    def record():
        return {"entity_id": f"{kind}:{uuid.uuid4()}", "event_type": f"{kind}.created", "data": data,
                "source": "csv_import", "idempotency_key": str(uuid.uuid4())}

    r = await client.post(f"/{resource}/import/batch", headers=headers, json={"records": [record(), record()]})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 1 and len(result["errors"]) == 1, result
    r = await client.post(f"/{resource}/import", headers=headers, json=record())
    assert r.status_code == 409, r.text
    assert await _numbered_count(session, company_id, "DUP-1") == 1


# ---------------------------------------------------------------------------
# Lists upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lists_upsert_false_skips_existing(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-001", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json() == {"created": 1, "skipped": 0, "updated": 0, "errors": []}

    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r2.json() == {"created": 0, "skipped": 1, "updated": 0, "errors": []}


@pytest.mark.asyncio
async def test_lists_upsert_true_emits_patch(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"list:upsert-{uuid.uuid4().hex[:8]}"
    idem = f"csv:list:upsert-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": "list.created",
        "data": {"ref_id": "UPL-002", "status": "draft", "total": 0, "line_items": []},
        "source": "csv_import",
        "idempotency_key": idem,
    }
    r1 = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r1.json()["created"] == 1

    changed = {**record, "data": {**record["data"], "notes": "updated through canonical patch"}}
    r2 = await client.post("/lists/import/batch", headers=headers, json={"records": [changed], "upsert": True})
    assert r2.status_code == 200
    body = r2.json()
    assert body["created"] == 0
    assert body["updated"] == 1
    assert body["skipped"] == 0

    r3 = await client.post("/lists/import/batch", headers=headers, json={"records": [changed], "upsert": True})
    assert r3.status_code == 200
    assert r3.json()["updated"] == 0
    assert r3.json()["skipped"] == 1


@pytest.mark.asyncio
async def test_doc_import_rejects_raw_lifecycle_event(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    record = {
        "entity_id": f"doc:unsafe-{uuid.uuid4().hex[:8]}",
        "event_type": "doc.payment.received",
        "data": {"amount": 10, "payment_date": "2026-09-20", "bank_account": "1111"},
        "source": "csv_import",
        "idempotency_key": str(uuid.uuid4()),
    }
    r = await client.post("/docs/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert r.json()["errors"]


@pytest.mark.asyncio
async def test_list_import_rejects_raw_lifecycle_event(client, session):
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    record = {
        "entity_id": f"list:unsafe-{uuid.uuid4().hex[:8]}",
        "event_type": "list.finalized",
        "data": {"status": "finalized"},
        "source": "csv_import",
        "idempotency_key": str(uuid.uuid4()),
    }
    r = await client.post("/lists/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert r.json()["errors"]


# ---------------------------------------------------------------------------
# Cross-company idempotency scoping (Bug 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_idempotency_key_allowed_for_different_companies(client, session):
    """Same idempotency_key from two different companies must both succeed (Bug 1 fix)."""
    company_a_id = uuid.uuid4()
    company_b_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()

    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    for cid, uid, name in [
        (company_a_id, user_a_id, "CompanyA"),
        (company_b_id, user_b_id, "CompanyB"),
    ]:
        session.add(Company(id=cid, name=name, slug=f"co-{cid.hex[:8]}"))
        session.add(User(id=uid, email=f"admin-{uid.hex[:8]}@xco.test", name="Admin", auth_hash="x", is_active=True))
        await session.flush()  # parents before membership for Postgres FK checks
        session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()

    token_a = await make_authed_token(session, str(user_a_id), str(company_a_id), "admin")
    token_b = await make_authed_token(session, str(user_b_id), str(company_b_id), "admin")

    shared_idem = f"csv:item:shared-key-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": f"item:{uuid.uuid4().hex}",
        "event_type": "item.created",
        "data": {"sku": f"XSK-{shared_idem[-6:]}", "name": "Cross Co Item", "quantity": 1, "sell_by": "piece"},
        "source": "csv_import",
        "idempotency_key": shared_idem,
    }

    # Company A import
    ra = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"records": [record]})
    assert ra.status_code == 200, ra.text
    assert ra.json()["created"] == 1

    # Company B import with same key and same entity_id - should ALSO create (different scope)
    record_b = {**record, "entity_id": f"item:{uuid.uuid4().hex}"}
    rb = await client.post("/items/import/batch",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"records": [record_b]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["created"] == 1, f"Expected created=1, got {rb.json()}"


# ---------------------------------------------------------------------------
# Server-side 500-record limit enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_import_501_records_returns_422(client, session):
    """POST /items/import/batch with 501 records must return 422.

    BatchImportRequest.records has max_length=500. Sending 501 records
    must be rejected by Pydantic validation before any DB work happens.
    """
    company_id, user_id, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    records = [
        {
            "entity_id": f"item:{uuid.uuid4().hex}",
            "event_type": "item.created",
            "data": {"sku": f"LIMIT-{i:04d}", "name": f"Item {i}", "quantity": 1, "sell_by": "piece"},
            "source": "csv_import",
            "idempotency_key": f"csv:item:limit-{i:04d}",
        }
        for i in range(501)
    ]
    r = await client.post(
        "/items/import/batch",
        headers=headers,
        json={"records": records},
    )
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Import field enforcement (Issues 1-4 fixes)
# ---------------------------------------------------------------------------

def _item_record(data: dict, sku_suffix: str | None = None) -> dict:
    """Build a minimal BatchImportRequest record dict."""
    suffix = sku_suffix or uuid.uuid4().hex[:8]
    return {
        "entity_id": f"item:{uuid.uuid4()}",
        "event_type": "item.created",
        "data": data,
        "source": "csv_import",
        "idempotency_key": f"csv:item:field-test-{suffix}",
    }


@pytest.mark.asyncio
async def test_import_missing_sell_by_counted_as_error(client, session):
    """Batch import with no sell_by must return 200 with created=0, errors non-empty.

    post_item() raises 422 when sell_by is absent. The per-record try/except
    must capture it and append to errors — not bubble a 422 from the endpoint.
    created + skipped must equal len(records).
    """
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({"name": "No Unit Item", "sku": f"NO-UNIT-{uuid.uuid4().hex[:6]}"})
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["skipped"] == 1
    assert len(body["errors"]) >= 1
    assert body["created"] + body["skipped"] == 1


@pytest.mark.asyncio
async def test_import_invalid_sell_by_counted_as_error(client, session):
    """Batch import with a sell_by value not in company units must surface in errors.

    Company has no custom units so DEFAULT_UNITS apply ("piece", "carat", etc.).
    "pcs" is not a valid unit name — post_item must raise 422.
    Per-record try/except must capture it; endpoint returns 200.
    """
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Bad Unit Item",
        "sku": f"BAD-UNIT-{uuid.uuid4().hex[:6]}",
        "sell_by": "pcs",  # invalid — "piece" is correct
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["skipped"] == 1
    assert len(body["errors"]) >= 1


@pytest.mark.asyncio
async def test_import_status_stripped_always_available(client, session):
    """Batch import with status=memo_out must create item with status=available.

    The backend strips status from rec.data before calling post_item, so the
    item always lands in the default available state regardless of CSV content.
    """
    from celerp.models.projections import Projection
    from sqlalchemy import select as _select

    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Status Test Item",
        "sku": f"STATUS-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "status": "memo_out",  # must be stripped
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    entity_id = record["entity_id"]
    proj = (await session.execute(
        _select(Projection).where(
            Projection.entity_id == entity_id,
            Projection.company_id == company_id,
        )
    )).scalars().first()
    assert proj is not None
    assert proj.state.get("status") == "available"


@pytest.mark.asyncio
async def test_import_timestamps_stripped_system_generated(client, session):
    """Batch import with user-supplied timestamps must be silently stripped.

    created_at is authoritative from Projection.created_at (set by ProjectionEngine
    on INSERT). updated_at is set by ProjectionEngine on every update.
    Neither field must ever be accepted from client-supplied event data.
    """
    from celerp.models.projections import Projection
    from sqlalchemy import select as _select
    from datetime import datetime

    company_id, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}

    record = _item_record({
        "name": "Timestamp Test Item",
        "sku": f"TS-{uuid.uuid4().hex[:6]}",
        "sell_by": "piece",
        "created_at": "BANGKOK",      # must be stripped — Projection column wins
        "updated_at": "not-a-date",   # must be stripped
    })
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    entity_id = record["entity_id"]
    proj = (await session.execute(
        _select(Projection).where(
            Projection.entity_id == entity_id,
            Projection.company_id == company_id,
        )
    )).scalars().first()
    assert proj is not None

    # State must not contain user-supplied garbage
    state = proj.state
    assert state.get("created_at") is None, "created_at must not be stored in state"
    assert state.get("updated_at") is None, "updated_at must not be stored in state"

    # Projection column must be set to a valid datetime by the engine
    assert proj.created_at is not None, "Projection.created_at must be set by engine"
    assert proj.updated_at is not None, "Projection.updated_at must be set by engine"
    # Both must be parseable datetimes (they're already datetime objects from SQLAlchemy)
    datetime.fromisoformat(proj.created_at.isoformat())
    datetime.fromisoformat(proj.updated_at.isoformat())


@pytest.mark.asyncio
async def test_import_spec_required_fields():
    """_IMPORT_SPEC.required must contain exactly {name, sell_by}.

    location_name and sku are not truly required (auto-resolved / auto-assigned).
    sell_by is required because post_item raises 422 without it.
    """
    from ui.routes.inventory import _IMPORT_SPEC

    assert "location_name" not in _IMPORT_SPEC.required
    assert "sku" not in _IMPORT_SPEC.required
    assert "name" in _IMPORT_SPEC.required
    assert "sell_by" in _IMPORT_SPEC.required


# ---------------------------------------------------------------------------
# Issue 1: ill-formed CSV handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_malformed_csv_shows_error(client):
    """CSV with extra columns beyond the header (causes None key in DictReader rows)
    must return a clean upload error, not a 500."""
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        # More columns than header → DictReader emits None key for overflow columns
        malformed = b"name,sell_by\nfoo,piece,extra_unexpected_col\n"
        r = await c.post(
            "/inventory/import/preview",
            cookies={"celerp_token": make_test_token(role="manager")},
            files={"csv_file": ("items.csv", malformed, "text/csv")},
        )
    assert r.status_code == 200
    body = r.text
    assert "unexpected error" not in body.lower()
    # Must show a user-friendly CSV error, not fall through to the column mapping step
    assert "more columns than" in body or "valid CSV" in body or "upload" in body.lower()


@pytest.mark.asyncio
async def test_import_none_fieldnames_shows_error(client):
    """CSV that causes DictReader to emit None fieldnames must return a clean error,
    not propagate to a 500."""
    from httpx import AsyncClient
    from httpx._transports.asgi import ASGITransport
    from ui.app import app as ui_app
    from test_helpers import make_test_token

    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        # A CSV where the header row is empty / blank triggers None fieldnames
        empty_header = b"\n\nname,sell_by\ntest,piece\n"
        r = await c.post(
            "/inventory/import/preview",
            cookies={"celerp_token": make_test_token(role="manager")},
            files={"csv_file": ("items.csv", empty_header, "text/csv")},
        )
    assert r.status_code == 200
    body = r.text
    assert "unexpected error" not in body.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["item.created", "item.snapshot"])
@pytest.mark.parametrize("field,value", [
    ("cost_base", 1.0),
    ("cost_landed", 500.0),
    ("landed_contributions", {"bill:x::freight": 50.0}),
    ("reserved_quantity", 5),
    ("fulfilled_for_docs", ["doc:x"]),
    ("status_doc_id", "doc:x"),
])
async def test_import_refuses_fields_the_app_manages(client, session, event_type, field, value):
    """Cost components, reservations and document links are kept by the app. An imported row that
    sets one is refused with a message naming it, and no item is created."""
    _, _, token = await _setup(session)
    headers = {"Authorization": f"Bearer {token}"}
    entity_id = f"item:managed-{uuid.uuid4().hex[:8]}"
    record = {
        "entity_id": entity_id,
        "event_type": event_type,
        "data": {"sku": f"MG-{uuid.uuid4().hex[:6]}", "name": "Managed", "quantity": 10,
                 "sell_by": "piece", field: value},
        "source": "csv_import",
        "idempotency_key": f"csv:item:{entity_id}",
    }
    r = await client.post("/items/import/batch", headers=headers, json={"records": [record]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert any(field in e for e in body["errors"]), body["errors"]
    assert (await client.get(f"/items/{entity_id}", headers=headers)).status_code == 404
