"""Accounting-only exclusions use the canonical document edit boundary."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from test_payments_stripe import _register, _payable_invoice, _h



@pytest.mark.asyncio
async def test_bulk_accounting_sync_flag_is_audited_and_reversible(client):
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    endpoint = "/docs/accounting-sync/bulk"
    op = str(uuid.uuid4())
    req = {"entity_ids": [eid], "excluded": True, "operation_id": op}
    applied = await client.post(endpoint, json=req, headers=_h(tok))
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"updated": 1, "selected": 1}
    snapshot = (await client.get(f"/docs/{eid}", headers=_h(tok))).json()
    assert snapshot["accounting_sync_excluded"] is True
    repeated = await client.post(endpoint, json=req, headers=_h(tok))
    assert repeated.status_code == 200 and repeated.json()["updated"] == 0

    absent = await client.post(endpoint, json={
        "entity_ids": [eid, "doc:unknown"], "excluded": False,
        "operation_id": str(uuid.uuid4()),
    }, headers=_h(tok))
    assert absent.status_code == 404
    assert (await client.get(f"/docs/{eid}", headers=_h(tok))).json()["accounting_sync_excluded"] is True

    restored = await client.post(endpoint, json={
        "entity_ids": [eid], "excluded": False,
        "operation_id": str(uuid.uuid4()),
    }, headers=_h(tok))
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"/docs/{eid}", headers=_h(tok))).json()["accounting_sync_excluded"] is False


@pytest.mark.asyncio
async def test_accounting_connectors_skip_excluded_invoices_without_affecting_shop_orders(monkeypatch):
    from celerp.db import SessionLocal
    from celerp_docs import doc_service

    state = {"doc_type": "invoice", "ref_id": "INV-77", "total": 85,
             "line_items": [], "accounting_sync_excluded": True}
    row = SimpleNamespace(entity_id="doc:INV-77", state=state)
    class Items:
        def scalars(self): return self
        def all(self): return [row]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def execute(self, *args): return Items()
        async def get(self, *args): return row
    monkeypatch.setattr("celerp.db.SessionLocal", lambda: Session())
    company = str(uuid.uuid4())
    for connector in ("xero", "quickbooks", "celerp"):
        assert await doc_service.list_unsynced_invoices(company, connector) == []
        assert await doc_service.invoice_for_push(company, row.entity_id, connector) is None
    assert len(await doc_service.list_unsynced_invoices(company, "shopify")) == 1
    assert (await doc_service.invoice_for_push(company, row.entity_id, "shopify"))["entity_id"] == row.entity_id
    state["accounting_sync_excluded"] = False
    assert len(await doc_service.list_unsynced_invoices(company, "xero")) == 1



def test_document_list_accounting_filter_and_bulk_control():
    from fasthtml.common import to_xml
    from ui.routes.documents import _doc_api_params, _doc_table, _doc_filters

    state = {"type": "bill", "q": "vendor", "accounting_sync": "excluded"}
    params = _doc_api_params(state, "", "", limit=50)
    assert params["doc_type"] == "bill" and params["accounting_sync"] == "excluded"
    # The Records select in the /docs filters panel: one GET form that keeps the type and search.
    controls = to_xml(_doc_filters(state, "", "", "all", "en"))
    assert 'name="accounting_sync"' in controls and '<option value="included">' in controls
    assert '<option value="excluded" selected>' in controls
    assert '<input type="hidden" name="type" value="bill">' in controls and 'value="vendor"' in controls

    html = to_xml(_doc_table([
        {"entity_id": "doc:bill-a", "doc_number": "BILL-001",
         "doc_type": "bill", "status": "final", "accounting_sync_excluded": True},
    ], doc_type="bill"))
    assert "doc-bulk-exclude-btn" in html and "doc-bulk-include-btn" in html
    assert "doc-bulk-operation" in html
    assert "Excluded" in html
    assert "/docs/bulk-accounting-sync" in html


def test_an_unknown_accounting_filter_says_every_record_is_shown():
    """A sync filter Celerp does not know is named as such, the way Client companies names a
    bad filter, instead of silently showing every record with no choice lit."""
    from fasthtml.common import to_xml
    from ui.routes.documents import _accounting_sync_warning

    warning = to_xml(_accounting_sync_warning({"accounting_sync": "bogus"}))
    assert "flash flash--warning" in warning
    assert "This sync filter is not one Celerp knows, so every record is shown." in warning
    assert _accounting_sync_warning({"accounting_sync": "included"}) == ""
    assert _accounting_sync_warning({}) == ""
