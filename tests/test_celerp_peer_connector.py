"""Accountant outgoing sync uses the connector plan and safe delivery boundaries."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from celerp.connectors.base import ConnectorContext, SyncDirection
from celerp.connectors.registry import get
from celerp.connectors.sync_runner import supported_outbound, sync_plan
from celerp.connectors import celerp as peer
from peer_marks_support import KEY, memory_marks  # noqa: F401

pytestmark = pytest.mark.usefixtures("memory_marks")


def test_celerp_sends_records_and_takes_journals_and_the_lock_date():
    connector = get("celerp")
    outbound = ["products_out", "contacts_out", "journals_out", "documents_out", "inventory_out", "attachments_out"]
    assert supported_outbound(connector) == outbound
    assert sync_plan(connector, SyncDirection.OUTBOUND) == outbound
    assert sync_plan(connector, SyncDirection.INBOUND) == ["journals", "period_lock"]


@pytest.mark.parametrize("url", [
    "http://example.com", "https://example.com/other", "https://user@host.test",
    "https://host.test:8443", "https://host.test/?query=1", "file:///etc/passwd",
    "https://host.test#fragment", "https://host.test@127.0.0.1",
])
def test_invalid_peer_url_is_rejected(url):
    with pytest.raises(ValueError):
        peer._target_url(ConnectorContext(company_id=str(uuid.uuid4()),
                                          store_handle=url, access_token="cp1.valid"))


@pytest.mark.asyncio
async def test_versioned_contact_outbound_and_retry_reporting(monkeypatch):
    sent = []
    company = str(uuid.uuid4())
    first = [("contact:a", 3, {"name": "A", "contact_type": "vendor", "private_note": "not sent"}),
             ("contact:b", 2, {"name": "B"})]
    async def read_page(company_id, after, since):
        assert str(company_id) == company
        assert since is None
        return first if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url == "https://peer.example/crm/contacts/import/batch"
            sent.append(kwargs)
            return SimpleNamespace(status_code=200 if len(sent) == 1 else 409,
                                   json=lambda: {"updated": 1, "errors": []})
    from celerp.connectors import sync_runner
    async def no_prior_sync(*args):
        return None
    monkeypatch.setattr(sync_runner, "_last_success_watermark", no_prior_sync)
    monkeypatch.setattr(peer, "_read_page", read_page)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    connector = peer.CelerpConnector()
    result = await connector.sync_contacts_out(ConnectorContext(
        company_id=company, store_handle="https://peer.example", access_token=KEY))
    assert result.updated == 1
    assert len(result.errors) == 1
    assert result.attention == [{"id": "contact:b", "label": "B",
                                 "reason": "Receiving installation needs attention"}]
    record = sent[0]["json"]["records"][0]
    assert record["source_revision"] == 3 and record["entity_id"] == "contact:a"
    assert record["data"]["name"] == "A"
    assert record["data"]["contact_type"] == "vendor"
    assert record["data"]["email"] is None
    assert "private_note" not in record["data"]
    assert sent[0]["headers"]["Authorization"] == f"Peer {KEY}"
    assert sent[0]["headers"]["X-Celerp-Source-Company"] == company


def test_peer_credential_form_uses_key_and_https_origin():
    from ui.routes.settings_connectors import _local_connector_entry, _store_url_error
    assert _local_connector_entry("celerp")["auth_type"] == "api_key"
    assert _store_url_error("https://accountant.example.com", "celerp") is None
    for url in ("", "http://accountant.example.com", "https://accountant.example.com/path",
                "https://accountant.example.com?x=1", "https://user@accountant.example.com"):
        assert _store_url_error(url, "celerp") is not None


@pytest.mark.asyncio
async def test_contact_sync_resumes_from_last_successful_run(monkeypatch):
    from datetime import datetime, timezone
    from celerp.connectors import sync_runner

    watermark = datetime(2026, 10, 12, tzinfo=timezone.utc)
    seen = []
    async def last_success(company_id, connector, entity):
        assert connector == "celerp" and entity == "contacts_out"
        return watermark
    async def read_page(company_id, after, since):
        seen.append((str(company_id), after, since))
        return []
    monkeypatch.setattr(sync_runner, "_last_success_watermark", last_success)
    monkeypatch.setattr(peer, "_read_page", read_page)
    result = await peer.CelerpConnector().sync_contacts_out(ConnectorContext(
        company_id=str(uuid.uuid4()), store_handle="https://peer.example",
        access_token=KEY,
    ))
    assert result.ok
    assert len(seen) == 1 and seen[0][1:] == ("", watermark)



@pytest.mark.asyncio
async def test_product_sync_transmits_catalog_only_and_preserves_revision(monkeypatch):
    from celerp.connectors import sync_runner

    company = str(uuid.uuid4())
    sent = []
    async def watermark(company_id, connector, entity):
        assert (company_id, connector, entity) == (company, "celerp", "products_out")
        return None
    async def products(company_id, after, since):
        assert str(company_id) == company and since is None
        return [("item:src", 5, {"name": "Widget", "sell_by": "piece",
                                  "sku": "W1", "quantity": 400, "cost_base": 123})] if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url == "https://firm.example/items/import/batch"
            sent.append(kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"created": 1, "errors": []})
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(peer, "_read_products", products)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    result = await peer.CelerpConnector().sync_products_out(ConnectorContext(
        company_id=company, store_handle="https://firm.example",
        access_token=KEY,
    ))
    assert result.created == 1 and result.ok
    assert len(sent) == 1
    envelope = sent[0]["json"]["records"][0]
    assert envelope["source_revision"] == 5 and envelope["entity_id"] == "item:src"
    assert envelope["data"]["name"] == "Widget"
    assert "quantity" not in envelope["data"] and "cost_base" not in envelope["data"]


def test_peer_product_payload_rejects_financial_movements_and_nonfinite_prices():
    from pydantic import ValidationError
    from celerp.services.peer_import import ProductData
    for extra in ({"quantity": 8}, {"cost_total": 200}, {"retail_price": float("inf")}):
        with pytest.raises(ValidationError):
            ProductData.model_validate({"name": "Widget", **extra})


@pytest.mark.asyncio
async def test_shared_peer_delivery_marks_failed_product_without_losing_source_revision(monkeypatch):
    from celerp.connectors import sync_runner

    seen = []
    company = str(uuid.uuid4())
    async def last_success(company_id, connector, entity):
        assert (company_id, connector, entity) == (company, "celerp", "products_out")
        return None
    async def read_products(company_id, after, since):
        return [("item:a", 9, {"name": "Part", "sell_by": "piece"})] if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            seen.append(kwargs["json"])
            return SimpleNamespace(status_code=503)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", last_success)
    monkeypatch.setattr(peer, "_read_products", read_products)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    result = await peer.CelerpConnector().sync_products_out(ConnectorContext(
        company_id=company, store_handle="https://firm.example", access_token=KEY))
    assert not result.ok
    assert result.errors == ["Product delivery incomplete"]
    assert seen[0]["records"][0]["source_revision"] == 9
    assert seen[0]["records"][0]["entity_id"] == "item:a"



@pytest.mark.asyncio
async def test_draft_document_outbound_filters_private_and_excluded_fields(monkeypatch):
    from celerp.connectors import sync_runner

    company = str(uuid.uuid4())
    seen = []
    async def watermark(company_id, connector, entity):
        assert (company_id, connector, entity) == (company, "celerp", "documents_out")
        return None
    async def docs(company_id, after, since):
        if after:
            return []
        return [
            ("doc:new", 3, {"doc_type": "invoice", "status": "draft", "ref_id": "DRAFT-12",
                            "line_items": [{"name": "Service", "quantity": 1, "unit_price": 100}],
                            "internal_audit": "must-not-be-sent"}),
            ("doc:skip", 2, {"doc_type": "invoice", "status": "draft",
                             "accounting_sync_excluded": True}),
            ("doc:issued", 6, {"doc_type": "invoice", "status": "sent", "ref_id": "INV-7"}),
        ]
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            assert url == "https://peer.example/docs/import/batch"
            record = kwargs["json"]["records"][0]
            seen.append(record)
            status = "skipped" if record["entity_id"] == "doc:issued" else "created"
            return SimpleNamespace(status_code=200, json=lambda: {status: 1, "errors": []})
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(peer, "_read_documents", docs)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    result = await peer.CelerpConnector().sync_documents_out(ConnectorContext(
        company_id=company, store_handle="https://peer.example", access_token=KEY))
    assert result.ok and result.created == 1 and result.skipped == 2
    assert [item["entity_id"] for item in seen] == ["doc:new", "doc:issued"]
    assert "internal_audit" not in seen[0]["data"]
    assert seen[0]["data"]["status"] == "draft"
    assert seen[1]["data"]["status"] == "sent"



@pytest.mark.asyncio
async def test_receiving_company_refusal_is_the_attention_reason(monkeypatch):
    from celerp.connectors import sync_runner

    cid = str(uuid.uuid4())
    sent = []
    refusal = "Line 1 names a product the accountant's copy does not hold yet"
    async def watermark(*args):
        return None
    async def page(company_id, after, since):
        return [
            ("doc:stock", 9, {"doc_type": "invoice", "status": "final", "ref_id": "INV-9",
                              "line_items": [{"name": "Widget", "sku": "W1",
                                             "quantity": 1, "unit_price": 100}]}),
            ("doc:service", 4, {"doc_type": "invoice", "status": "final",
                                "line_items": [{"name": "Advice",
                                               "quantity": 1, "unit_price": 100}]}),
        ] if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            entity_id = kwargs["json"]["records"][0]["entity_id"]
            sent.append(entity_id)
            body = ({"created": 0, "updated": 0, "skipped": 0,
                     "errors": [f"{entity_id}: {refusal}"]}
                    if entity_id == "doc:stock" else {"created": 1, "errors": []})
            return SimpleNamespace(status_code=200, json=lambda: body)
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(peer, "_read_documents", page)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    result = await peer.CelerpConnector().sync_documents_out(ConnectorContext(
        company_id=cid, store_handle="https://firm.example", access_token=KEY))
    assert sent == ["doc:stock", "doc:service"]
    assert not result.ok and result.created == 1
    assert result.attention == [{"id": "doc:stock", "label": "INV-9", "reason": refusal}]
    assert result.errors == ["Document delivery incomplete"]



def test_physical_counts_depend_on_document_reconciliation():
    connector = peer.CelerpConnector()
    assert connector.outbound_dependencies["inventory_out"] == {"documents_out"}
    plan = sync_plan(connector, SyncDirection.OUTBOUND)
    assert plan.index("documents_out") < plan.index("inventory_out")
    assert plan.index("inventory_out") < plan.index("attachments_out")


@pytest.mark.asyncio
async def test_stock_audit_does_not_require_contact_or_product_name(monkeypatch):
    from celerp.connectors import sync_runner

    source = str(uuid.uuid4())
    posted = []
    async def watermark(*args):
        return None
    async def audits(company_id, after, since):
        assert str(company_id) == source
        return [("list:count-1", 2, {
            "location_id": "location:a", "location_name": "Warehouse",
            "location_type": "warehouse", "source_closed_event_id": 21,
            "counts": [{"item_id": "item:a", "counted_qty": 4}],
        })] if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            posted.append((url, kwargs["json"]["records"][0]))
            return SimpleNamespace(status_code=200, json=lambda: {"created": 1, "errors": []})
    async def no_excluded_document(company_id):
        return None
    monkeypatch.setattr(sync_runner, "_last_success_watermark", watermark)
    monkeypatch.setattr(peer, "_excluded_stock_document", no_excluded_document)
    monkeypatch.setattr(peer, "_read_audits", audits)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    result = await peer.CelerpConnector().sync_inventory_out(ConnectorContext(
        company_id=source, store_handle="https://peer.example", access_token=KEY,
    ))
    assert result.ok and result.created == 1 and not result.errors
    assert posted[0][0] == "https://peer.example/lists/import/batch"
    assert posted[0][1]["entity_id"] == "list:count-1"
    assert posted[0][1]["data"]["counts"][0]["counted_qty"] == 4



@pytest.mark.asyncio
async def test_accountant_downstream_stops_when_required_outbound_entity_failed(monkeypatch):
    from contextlib import asynccontextmanager
    from celerp import db
    from celerp.connectors import ownership, sync_runner
    from celerp.connectors.base import SyncEntity, SyncResult

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def session():
        yield Session()

    async def no_config(*args, **kwargs):
        return None

    seen = []
    async def fake_run_sync(connector, ctx, entity, **kwargs):
        seen.append(entity)
        return SyncResult(
            entity=SyncEntity.PRODUCTS if entity == "products_out" else SyncEntity.CONTACTS,
            direction=SyncDirection.OUTBOUND,
            errors=["Source product needs review"] if entity == "products_out" else [],
        )

    monkeypatch.setattr(db, "get_session_ctx", session)
    monkeypatch.setattr(ownership, "lock_connector_operation", no_config)
    monkeypatch.setattr(sync_runner, "run_sync", fake_run_sync)
    connector = peer.CelerpConnector()
    assert connector.outbound_dependencies["documents_out"] == frozenset(
        {"contacts_out", "products_out"})
    assert connector.outbound_dependencies["attachments_out"] == frozenset({"documents_out"})
    assert connector.outbound_dependencies["journals_out"] == frozenset({"contacts_out"})
    results = await sync_runner.run_connector_sync(
        connector,
        ConnectorContext(company_id=str(uuid.uuid4()), access_token="cp1.fake",
                         store_handle="https://firm.example"),
        SyncDirection.OUTBOUND,
    )
    assert seen == ["products_out", "contacts_out", "journals_out"]
    assert len(results) == 3 and not results[0].ok


async def test_a_key_the_accountant_closed_says_so_on_the_journals_pull(monkeypatch):
    from ui.routes.settings_connectors import sync_failure_reason

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            return SimpleNamespace(status_code=401, json=lambda: {"detail": "Invalid key"})
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    with pytest.raises(ValueError) as closed:
        await peer.CelerpConnector().sync_journals(ConnectorContext(
            company_id=str(uuid.uuid4()), store_handle="https://firm.example", access_token=KEY))
    reason = sync_failure_reason(str(closed.value), "Celerp Accountant")
    assert "revoked or has expired" in reason and "new pairing link" in reason
    assert "GitHub" not in reason and "Web Access" not in reason
