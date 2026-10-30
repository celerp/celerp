"""Accountant stock counts use the canonical, journal-posting audit lifecycle."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.company_peer import CompanyPeerIdentity
from celerp.models.projections import Projection
from celerp.services.peer_identity import _digest
from celerp.services.peer_inventory import receive_stock_audit
from test_payments_stripe import _register, _h, _company_id
from peer_marks_support import KEY, memory_marks  # noqa: F401



@pytest.mark.asyncio
async def test_completed_peer_count_posts_canonical_adjustment_and_replays_once(client, session):
    token = await _register(client)
    company_id = uuid.UUID(_company_id(token))
    location = (await client.post("/companies/me/locations", headers=_h(token), json={
        "name": "Main", "type": "warehouse",
    }))
    assert location.status_code == 200, location.text
    location_id = location.json()["id"]
    item = await client.post("/items", headers=_h(token), json={
        "status": "available", "sku": "AUD-1", "name": "Audit Lot",
        "quantity": 10, "cost_total": 100, "sell_by": "piece",
        "location_id": location_id, "inventory_type": "stocked",
    })
    assert item.status_code == 200, item.text
    item_id = item.json()["id"]
    source_company, source_location = uuid.uuid4(), uuid.uuid4()
    source_item = "item:from-client"
    session.add(CompanyPeerIdentity(
        company_id=company_id, source_company_id=source_company,
        entity_type="item", source_entity_id=source_item,
        source_key_hash=_digest(source_item), target_entity_id=item_id,
        last_revision=1,
    ))
    await session.commit()
    grant = SimpleNamespace(company_id=company_id, source_company_id=source_company)
    data = {
        "location_id": str(source_location), "location_name": "Main",
        "location_type": "warehouse",
        "source_closed_event_id": 21,
        "counts": [{"item_id": source_item, "counted_qty": 7}],
    }
    source_audit = "list:client-audit-1"
    applied = await receive_stock_audit(
        session, grant=grant, source_entity_id=source_audit,
        source_revision=1, data=data)
    assert applied == "created"
    await session.commit()
    inventory = await session.get(Projection, (company_id, item_id), populate_existing=True)
    assert float(inventory.state["quantity"]) == 7
    audit_map = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == company_id,
        CompanyPeerIdentity.source_company_id == source_company,
        CompanyPeerIdentity.entity_type == "list",
    ))
    assert audit_map.last_revision == 1
    audit = await session.get(Projection, (company_id, audit_map.target_entity_id))
    assert audit.state["status"] == "closed"
    assert audit.state["result"] == "stock_adjusted"
    assert await receive_stock_audit(
        session, grant=grant, source_entity_id=source_audit,
        source_revision=1, data=data) == "skipped"
    assert await receive_stock_audit(
        session, grant=grant, source_entity_id=source_audit,
        source_revision=2, data=data) == "skipped"
    await session.commit()
    unchanged = await session.get(Projection, (company_id, item_id), populate_existing=True)
    assert float(unchanged.state["quantity"]) == 7
    with pytest.raises(HTTPException) as conflict:
        await receive_stock_audit(
            session, grant=grant, source_entity_id=source_audit,
            source_revision=2,
            data={**data, "counts": [{"item_id": source_item, "counted_qty": 9}]})
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_peer_count_never_overwrites_missing_or_wrong_location(client, session):
    token = await _register(client)
    company = uuid.UUID(_company_id(token))
    grant = SimpleNamespace(company_id=company, source_company_id=uuid.uuid4())
    data = {
        "location_id": str(uuid.uuid4()), "location_name": "No matching site",
        "location_type": "warehouse",
        "source_closed_event_id": 21,
        "counts": [{"item_id": "item:not-paired", "counted_qty": 0}],
    }
    with pytest.raises(HTTPException) as refusal:
        await receive_stock_audit(session, grant=grant,
                                  source_entity_id="list:audit-unknown",
                                  source_revision=1, data=data)
    assert refusal.value.status_code == 409
    await session.rollback()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company, Projection.entity_type == "list"))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_count_outbound_preserves_source_identity_and_retries(monkeypatch, memory_marks):
    from celerp.connectors import celerp as connector
    from celerp.connectors import sync_runner
    from celerp.connectors.base import ConnectorContext

    company_id = str(uuid.uuid4())
    deliveries = []
    source_state = {
        "location_id": str(uuid.uuid4()), "location_name": "Main",
        "location_type": "warehouse",
        "source_closed_event_id": 21,
        "counts": [{"item_id": "item:s", "counted_qty": 4}],
    }
    async def watermark(*args):
        assert args[2] == "inventory_out"
        return None
    async def read_audits(cid, after, since):
        assert str(cid) == company_id and not after and since is None
        return [("list:audit", 6, source_state)]
    class Client:
        def __init__(self, **kw): assert kw == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kw):
            assert url == "https://firm.example/lists/import/batch"
            deliveries.append(kw["json"]["records"][0])
            return SimpleNamespace(status_code=200, json=lambda: {"created": 1, "errors": []})
    async def no_excluded_document(cid):
        return None
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(connector, "_excluded_stock_document", no_excluded_document)
    monkeypatch.setattr(connector, "_read_audits", read_audits)
    monkeypatch.setattr(connector, "RateLimitedClient", Client)
    response = await connector.CelerpConnector().sync_inventory_out(ConnectorContext(
        company_id=company_id, store_handle="https://firm.example", access_token=KEY))
    assert response.ok and response.created == 1
    assert deliveries[0]["source_revision"] == 6
    assert deliveries[0]["data"] == source_state



@pytest.mark.asyncio
async def test_company_copy_seeds_original_location_identity(client, session):
    from celerp.services.peer_identity import seed_restored_identities, peer_identity

    token = await _register(client)
    target_company = uuid.UUID(_company_id(token))
    response = await client.post("/companies/me/locations", headers=_h(token), json={
        "name": "Secondary", "type": "warehouse",
    })
    assert response.status_code == 200, response.text
    restored_id = response.json()["id"]
    original_id = uuid.uuid4()
    source_company = uuid.uuid4()
    await seed_restored_identities(
        session, target_company=target_company, source_company=source_company,
        id_map={str(original_id): restored_id},
    )
    await session.commit()
    assert await peer_identity(
        session, target_company=target_company, source_company=source_company,
        entity_type="location", source_entity_id=str(original_id),
    ) == restored_id



@pytest.mark.asyncio
async def test_late_older_stock_count_cannot_reverse_newer_canonical_audit(client, session):
    token = await _register(client)
    company_id = uuid.UUID(_company_id(token))
    location = await client.post("/companies/me/locations", headers=_h(token), json={
        "name": "Main Audit Order", "type": "warehouse",
    })
    assert location.status_code == 200
    location_id = location.json()["id"]
    item = await client.post("/items", headers=_h(token), json={
        "status": "available", "sku": "AUD-ORDER", "name": "Chronology Stock",
        "quantity": 10, "cost_total": 100, "sell_by": "piece",
        "location_id": location_id, "inventory_type": "stocked",
    })
    assert item.status_code == 200, item.text
    item_id = item.json()["id"]
    source_company, source_location = uuid.uuid4(), uuid.uuid4()
    source_item = "item:chrono"
    session.add(CompanyPeerIdentity(
        company_id=company_id, source_company_id=source_company,
        entity_type="item", source_entity_id=source_item,
        source_key_hash=_digest(source_item), target_entity_id=item_id,
        last_revision=1,
    ))
    await session.commit()
    grant = SimpleNamespace(company_id=company_id, source_company_id=source_company)
    def count(qty, position):
        return {
            "location_id": str(source_location), "location_name": "Main Audit Order",
            "location_type": "warehouse", "source_closed_event_id": position,
            "counts": [{"item_id": source_item, "counted_qty": qty}],
        }
    assert await receive_stock_audit(
        session, grant=grant, source_entity_id="list:first",
        source_revision=1, data=count(8, 31)) == "created"
    await session.commit()
    assert await receive_stock_audit(
        session, grant=grant, source_entity_id="list:newest",
        source_revision=1, data=count(6, 33)) == "created"
    await session.commit()
    with pytest.raises(HTTPException) as stale:
        await receive_stock_audit(
            session, grant=grant, source_entity_id="list:delayed",
            source_revision=1, data=count(12, 32))
    assert stale.value.status_code == 409
    await session.rollback()
    actual = await session.get(Projection, (company_id, item_id), populate_existing=True)
    assert float(actual.state["quantity"]) == 6
    cursor = await session.scalar(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == company_id,
        CompanyPeerIdentity.source_company_id == source_company,
        CompanyPeerIdentity.entity_type == "audit_cursor",
    ))
    assert cursor.last_revision == 33
    assert cursor.target_entity_id == location_id



@pytest.mark.asyncio
async def test_completed_stock_audits_page_in_posted_order_not_document_id(client, session, monkeypatch):
    from datetime import datetime, timezone
    from celerp.models.ledger import LedgerEntry
    from celerp.connectors.celerp import _read_audits
    from contextlib import asynccontextmanager
    import celerp.db as db

    @asynccontextmanager
    async def shared_session():
        yield session

    monkeypatch.setattr(db, "get_session_ctx", shared_session)

    token = await _register(client)
    company_id = uuid.UUID(_company_id(token))
    created = await client.post("/companies/me/locations", headers=_h(token), json={
        "name": "Chronological Location", "type": "warehouse",
    })
    assert created.status_code == 200
    location_id = created.json()["id"]
    position = []
    for source_id, qty in (("list:z-older", 8), ("list:a-newer", 6)):
        session.add(Projection(
            company_id=company_id, entity_id=source_id, entity_type="list",
            version=1, updated_at=datetime.now(timezone.utc),
            state={"list_type": "audit", "status": "closed",
                   "result": "stock_adjusted", "location_id": location_id,
                   "line_items": [{"item_id": "item:lot", "counted_qty": qty}]},
        ))
        entry = LedgerEntry(
            company_id=company_id, entity_id=source_id, entity_type="list",
            event_type="list.closed", data={"result": "stock_adjusted"},
            source="audit", idempotency_key=f"audit:{source_id}",
        )
        session.add(entry)
        await session.flush()
        position.append(entry.id)
    await session.commit()
    first = await _read_audits(company_id, "", None)
    assert [x[0] for x in first] == ["list:z-older", "list:a-newer"]
    assert [x[2]["source_closed_event_id"] for x in first] == position
    second = await _read_audits(company_id, str(position[0]), None)
    assert [x[0] for x in second] == ["list:a-newer"]



def test_stock_route_accepts_ordered_audit_contract_and_rejects_missing_position():
    from pydantic import ValidationError
    from celerp.services.peer_import import StockCountData

    data = {
        "location_id": str(uuid.uuid4()), "location_name": "Warehouse",
        "location_type": "warehouse", "source_closed_event_id": 44,
        "counts": [{"item_id": "item:src", "counted_qty": 5}],
    }
    assert StockCountData.model_validate(data).model_dump(mode="json")["source_closed_event_id"] == 44
    for invalid in ({k: v for k, v in data.items() if k != "source_closed_event_id"},
                    {**data, "source_closed_event_id": True},
                    {**data, "source_closed_event_id": -1}):
        with pytest.raises(ValidationError):
            StockCountData.model_validate(invalid)



@pytest.mark.asyncio
async def test_stock_sync_refuses_missing_issued_stock_document(monkeypatch):
    from celerp.connectors import celerp as peer
    from celerp.connectors.base import ConnectorContext

    async def excluded(cid):
        assert isinstance(cid, uuid.UUID)
        return "doc:excluded-sale", "INV-0042"

    async def must_not_send(*args):
        pytest.fail("Stock counts must not bypass an excluded stock transaction")

    monkeypatch.setattr(peer, "_excluded_stock_document", excluded)
    monkeypatch.setattr(peer, "_read_audits", must_not_send)
    connector = peer.CelerpConnector()
    result = await connector.sync_inventory_out(ConnectorContext(
        company_id=str(uuid.uuid4()), store_handle="https://peer.example",
        access_token="cp1.secret",
    ))
    assert not result.ok
    assert result.attention[0]["id"] == "doc:excluded-sale"
    assert result.attention[0]["label"] == "INV-0042"
    assert "reconciliation" in result.errors[0].lower()


@pytest.mark.asyncio
async def test_stock_exclusion_guard_checks_full_document_page(monkeypatch):
    from contextlib import asynccontextmanager
    from celerp.connectors import celerp as peer
    from celerp.db import get_session_ctx
    from celerp.models.projections import Projection

    cid = uuid.uuid4()
    # The guard scans beyond a first page of excluded nonstock documents.
    pages = 0
    class Records:
        def __init__(self, values):
            self.values = values
        def all(self):
            return self.values
    class Session:
        async def execute(self, stmt):
            nonlocal pages
            pages += 1
            if pages == 1:
                return Records([
                    (f"doc:{n:04}", {"status": "final", "accounting_sync_excluded": True,
                                    "line_items": [{"name": "Consulting"}]})
                    for n in range(peer._PAGE)
                ])
            return Records([
                ("doc:1000", {"status": "final", "accounting_sync_excluded": True,
                              "ref_id": "INV-1000",
                              "line_items": [{"sku": "ITEM", "quantity": 1}]})
            ])
    @asynccontextmanager
    async def session_factory():
        yield Session()

    monkeypatch.setattr("celerp.db.get_session_ctx", session_factory)
    assert await peer._excluded_stock_document(cid) == ("doc:1000", "INV-1000")
    assert pages == 2


@pytest.mark.asyncio
async def test_distinct_source_locations_cannot_share_one_receiving_location(client, session):
    from celerp.services.peer_identity import resolve_peer_location

    token = await _register(client)
    company = uuid.UUID(_company_id(token))
    created = await client.post("/companies/me/locations", headers=_h(token), json={
        "name": "Paired Site", "type": "warehouse"})
    assert created.status_code == 200, created.text
    grant = SimpleNamespace(company_id=company, source_company_id=uuid.uuid4())
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    first_target = await resolve_peer_location(
        session, grant=grant, source_id=first_id,
        name="Paired Site", location_type="warehouse")
    assert first_target == created.json()["id"]
    await session.commit()

    assert await resolve_peer_location(
        session, grant=grant, source_id=first_id,
        name="Paired Site", location_type="warehouse") == first_target
    with pytest.raises(HTTPException) as refused:
        await resolve_peer_location(
            session, grant=grant, source_id=second_id,
            name="Paired Site", location_type="warehouse")
    assert refused.value.status_code == 409
    await session.rollback()
    mappings = (await session.execute(select(CompanyPeerIdentity).where(
        CompanyPeerIdentity.company_id == company,
        CompanyPeerIdentity.entity_type == "location"))).scalars().all()
    assert len(mappings) == 1 and mappings[0].source_entity_id == first_id
