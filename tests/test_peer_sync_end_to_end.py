# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A client company's books reach its accountant's copy through the accountant's own
import, document and file routes, stock-moving documents included, and the two
companies' trial balance, receivables, payables and stock agree afterwards."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from company_backup_support import confirm, download, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_copy_round_trip import _books
from test_company_handoff_identity_restore import _link, _link_verified  # noqa: F401

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_peer_sync", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

FIRM = "https://firm.example"
# The firm routes a client company writes through, by kind.
_EXISTING_ROUTES = ("/crm/contacts/import/batch", "/items/import/batch", "/docs/import/batch",
                    "/lists/import/batch")


def _iid(item: dict) -> str:
    return item.get("entity_id") or item["id"]


def _company_of(tok: str) -> str:
    from celerp.services.auth import decode_access_token
    return str(decode_access_token(tok)["company_id"])


async def _location(engine, company_id: str) -> str:
    from celerp.models.company import Location
    async with maker(engine)() as s:
        return str(await s.scalar(select(Location.id).where(
            Location.company_id == uuid.UUID(company_id)).order_by(Location.name).limit(1)))


async def _stock(client, tok: str) -> dict:
    r = await client.get("/items", params={"limit": 500}, headers=auth(tok))
    assert r.status_code == 200, r.text
    out: dict[str, float] = {}
    for row in r.json()["items"]:
        out[row["sku"]] = round(out.get(row["sku"], 0) + float(row.get("quantity") or 0), 4)
    return out


async def _paired(real_client, real_engine, before_copy=None):
    """The demo company, its accountant's copy of it, and a key the copy issued to it;
    ``before_copy`` is what the company does before the copy is taken."""
    registered = await real_client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    tok = registered.json()["access_token"]
    await seed_demo.seed_items(real_client, tok)
    contacts = await seed_demo.seed_contacts(real_client, tok)
    await seed_demo.seed_docs(real_client, tok, contacts, "invoice", 4)
    await seed_demo.seed_docs(real_client, tok, contacts, "purchase_order", 2)
    if before_copy is not None:
        await before_copy(tok, contacts)

    # The accountant opens the company copy from its link as a company of their own.
    accountant = await owner(real_engine, "firm@example.test", "Firm Owner")
    from company_backup_support import company
    own = await company(real_engine, accountant, "Firm Practice", "firm-marker")
    firm_tok = await token(real_engine, accountant, own)
    preview = await real_client.post("/company-backups/read", files={
        "file": ("demo.celerp-company", await download(real_client, tok))},
        data={"mode": "new_company", **_link()}, headers=auth(firm_tok))
    assert preview.status_code == 200, preview.text
    opened = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"),
                                    headers=auth(firm_tok))
    assert opened.status_code == 201, opened.text
    firm_company = opened.json()["company_id"]
    firm_tok = await token(real_engine, accountant, firm_company)
    client_company = _company_of(tok)
    granted = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Demo Trading", "firm": "Firm Practice",
        "scopes": ["contacts", "products", "documents", "inventory"]}, headers=auth(firm_tok))
    assert granted.status_code == 201, granted.text
    return tok, firm_tok, client_company, granted.json()["credential"], contacts


def _route_to_app(monkeypatch, sent: list):
    """The connector's HTTP client, reaching the accountant's installation in-process,
    in every module that names it (each binds the class when first imported)."""
    from celerp.connectors import celerp as peer
    from celerp.connectors import celerp_key
    from celerp.main import app

    class FirmClient(httpx.AsyncClient):
        def __init__(self, *, public_only: bool = False, timeout: float | None = None):
            assert public_only
            super().__init__(transport=httpx.ASGITransport(app=app), timeout=timeout or 30.0)

        async def request(self, method, url, **kwargs):
            parsed = httpx.URL(url)
            assert f"{parsed.scheme}://{parsed.host}" == FIRM
            sent.append((method, parsed.path))
            return await super().request(method, url, **kwargs)

    monkeypatch.setattr(peer, "RateLimitedClient", FirmClient)
    monkeypatch.setattr(celerp_key, "RateLimitedClient", FirmClient)
    monkeypatch.setattr("celerp.connectors.http.RateLimitedClient", FirmClient)


async def _sync(company_id: str, credential: str) -> list:
    """One outbound run in the connector's own order; returns each entity's result."""
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.celerp import CelerpConnector
    from celerp.connectors.sync_runner import sync_plan

    connector = CelerpConnector()
    ctx = ConnectorContext(company_id=company_id, store_handle=FIRM, access_token=credential)
    results = []
    for entity in sync_plan(connector, SyncDirection.OUTBOUND):
        results.append(await getattr(connector, f"sync_{entity}")(ctx))
    return results


async def _client_activity(real_client, real_engine, tok: str, contacts: list) -> None:
    """What the client company does after the copy was taken: a new customer, a new
    stock invoice finalized, sent and paid, one fulfilled from stock, a purchase order
    received into stock, and a draft."""
    customer = await real_client.post("/crm/contacts", json={
        "name": "Harbour Cafe", "contact_type": "customer", "email": "orders@harbour.example"},
        headers=auth(tok))
    assert customer.status_code in (200, 201), customer.text
    items = (await real_client.get("/items", params={"limit": 500}, headers=auth(tok))).json()["items"]
    stocked = sorted((i for i in items if float(i.get("quantity") or 0) >= 5), key=lambda i: i["sku"])
    sold, fulfilled, bought = stocked[0], stocked[1], stocked[2]

    async def new_doc(doc_type: str, contact_id: str, item: dict, qty: int, price: float,
                      key: str = "") -> str:
        r = await real_client.post("/docs", json={
            "doc_type": doc_type, "contact_id": contact_id,
            "line_items": [{"item_id": _iid(item), "name": item["name"], "quantity": qty,
                            "unit_price": price, "line_total": qty * price}],
            "total": qty * price, "idempotency_key": f"after-copy:{doc_type}:{item['sku']}{key}",
        }, headers=auth(tok))
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    paid = await new_doc("invoice", customer.json()["id"], sold, 2, 40.0)
    await seed_demo._advance(real_client, auth(tok), paid, ("finalize", "send", "pay"), "after-copy:paid")
    shipped = await new_doc("invoice", customer.json()["id"], fulfilled, 3, 25.0)
    await seed_demo._advance(real_client, auth(tok), shipped, ("finalize",), "after-copy:shipped")
    r = await real_client.post(f"/docs/{shipped}/fulfill-lines", json={
        "line_entity_ids": [_iid(fulfilled)], "idempotency_key": "after-copy:fulfil"},
        headers=auth(tok))
    assert r.status_code == 200, r.text
    supplier = await real_client.post("/crm/contacts", json={
        "name": "Northern Supply", "contact_type": "vendor"}, headers=auth(tok))
    assert supplier.status_code in (200, 201), supplier.text
    po = await new_doc("purchase_order", supplier.json()["id"], bought, 4, 10.0)
    await _receive(real_client, real_engine, tok, po, bought, "after-copy")
    await new_doc("invoice", customer.json()["id"], sold, 1, 40.0, ":draft")
    return {"shipped": shipped, "fulfilled": fulfilled, "received": po}


async def _receive(real_client, real_engine, tok: str, po: str, item: dict, key: str) -> None:
    await seed_demo._advance(real_client, auth(tok), po, ("finalize",), f"{key}:po")
    r = await real_client.post(f"/docs/{po}/receive", json={
        "location_id": await _location(real_engine, _company_of(tok)),
        "received_items": [{"item_id": _iid(item), "quantity_received": 4, "po_line_index": 0}],
        "idempotency_key": f"{key}:receive"}, headers=auth(tok))
    assert r.status_code == 200, r.text


async def test_peer_connector_uses_existing_apis(real_client, real_engine, monkeypatch):
    """Every write reaches the accountant's copy through its existing routes, the copy's
    books tie to the client's, and a second run changes nothing."""
    tok, firm_tok, client_company, credential, contacts = await _paired(real_client, real_engine)
    await _client_activity(real_client, real_engine, tok, contacts)
    sent: list = []
    _route_to_app(monkeypatch, sent)

    results = await _sync(client_company, credential)

    assert [(r.entity.value, r.errors, r.attention) for r in results if r.errors or r.attention] == []
    assert sum(r.created + r.updated for r in results) >= 5
    writes = {path for method, path in sent if method == "POST"}
    assert writes and all(path in _EXISTING_ROUTES or path.endswith("/files") for path in writes), writes
    assert not any(path.startswith("/company-peers/") for path in writes)
    assert await _books(real_client, firm_tok) == await _books(real_client, tok)

    again = await _sync(client_company, credential)
    assert [(r.entity.value, r.created, r.updated, r.errors) for r in again
            if r.created or r.updated or r.errors] == []
    assert await _books(real_client, firm_tok) == await _books(real_client, tok)


async def test_the_key_check_reaches_the_accountants_installation(real_client, real_engine, monkeypatch):
    """Before sending, the run checks its key with the accountant's installation, whichever
    peer module was imported first."""
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.celerp import CelerpConnector

    _tok, _firm_tok, client_company, credential, _contacts = await _paired(real_client, real_engine)
    sent: list = []
    _route_to_app(monkeypatch, sent)

    await CelerpConnector().before_outbound(
        ConnectorContext(company_id=client_company, store_handle=FIRM, access_token=credential))

    assert ("GET", "/company-peers/verify") in sent


async def test_peer_pushes_stock_moving_documents(real_client, real_engine, monkeypatch):
    """Issued stock invoices, a fulfilled invoice and a received purchase order move
    stock in the accountant's copy the way they did in the client's company."""
    tok, firm_tok, client_company, credential, contacts = await _paired(real_client, real_engine)
    await _client_activity(real_client, real_engine, tok, contacts)
    _route_to_app(monkeypatch, [])

    results = await _sync(client_company, credential)

    documents = next(r for r in results if r.entity.value == "documents")
    assert documents.attention == [] and documents.errors == []
    assert await _stock(real_client, firm_tok) == await _stock(real_client, tok)
    assert await _books(real_client, firm_tok) == await _books(real_client, tok)


async def test_old_peer_receivers_are_gone(real_client, real_engine):
    """The separate receive routes no longer exist; only the key handshake and the
    owner's grant routes stay under /company-peers."""
    for path in ("contacts", "products", "documents", "attachments", "inventory"):
        r = await real_client.post(f"/company-peers/{path}", json={})
        assert r.status_code in (404, 405), (path, r.status_code)


async def test_peer_key_refused_outside_its_routes(real_client, real_engine):
    """A peer key opens only the import and file routes its scopes name; any other route
    answers 403, and a scope the key lacks is refused on its route."""
    tok, firm_tok, client_company, credential, _ = await _paired(real_client, real_engine)
    headers = {"Authorization": f"Peer {credential}", "X-Celerp-Source-Company": client_company}
    for method, path in (("GET", "/docs"), ("POST", "/docs"), ("GET", "/accounting/trial-balance"),
                         ("POST", "/company-peers/grants"), ("GET", "/crm/contacts")):
        r = await real_client.request(method, path, headers=headers, json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)
    narrow = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Contacts only", "firm": "Firm Practice", "scopes": ["contacts"]},
        headers=auth(firm_tok))
    assert narrow.status_code == 201, narrow.text
    r = await real_client.post("/docs/import/batch", json={"records": []}, headers={
        "Authorization": f"Peer {narrow.json()['credential']}", "X-Celerp-Source-Company": client_company})
    assert r.status_code == 403, r.text
    wrong_source = {**headers, "X-Celerp-Source-Company": str(uuid.uuid4())}
    r = await real_client.post("/crm/contacts/import/batch", json={"records": []}, headers=wrong_source)
    assert r.status_code == 401, r.text


async def test_source_revision_is_for_connected_companies_only(real_client, real_engine):
    """A signed-in user's import naming a source revision is refused with the reason."""
    registered = await real_client.post("/auth/register", json={
        "company_name": "Solo Co", "email": "solo@example.test", "name": "Solo", "password": "solopw1234"})
    tok = registered.json()["access_token"]
    r = await real_client.post("/crm/contacts/import/batch", json={"records": [{
        "entity_id": "contact:x", "event_type": "crm.contact.created", "data": {"name": "X"},
        "source": "csv", "idempotency_key": "x", "source_revision": 2}]}, headers=auth(tok))
    assert r.status_code == 422, r.text
    assert "connected company" in r.text


async def test_undone_receipt_and_taken_back_shipment_follow_the_client(
        real_client, real_engine, monkeypatch):
    """A receipt the client undid and goods it took back from a customer are undone and
    taken back in the accountant's copy on the next run, and the two still agree."""
    tok, firm_tok, client_company, credential, contacts = await _paired(real_client, real_engine)
    done = await _client_activity(real_client, real_engine, tok, contacts)
    _route_to_app(monkeypatch, [])
    await _sync(client_company, credential)

    r = await real_client.delete(f"/docs/{done['received']}/receive", headers=auth(tok))
    assert r.status_code == 200, r.text
    shipped = (await real_client.get(f"/docs/{done['shipped']}", headers=auth(tok))).json()
    r = await real_client.post(f"/docs/{done['shipped']}/revert-lines", json={
        "line_ids": [shipped["line_items"][0]["line_id"]], "idempotency_key": "after-copy:take-back"},
        headers=auth(tok))
    assert r.status_code == 200, r.text
    results = await _sync(client_company, credential)

    assert [(r.entity.value, r.attention) for r in results if r.errors or r.attention] == []
    assert await _stock(real_client, firm_tok) == await _stock(real_client, tok)
    assert await _books(real_client, firm_tok) == await _books(real_client, tok)


async def test_an_undone_receipt_that_fails_later_in_its_record_leaves_the_copy_as_it_was(
        real_client, real_engine, monkeypatch):
    """The accountant's copy undoes the client's undone receipt inside the bill's own record:
    when the record then fails, the undo rolls back with it, and the next run applies it whole."""
    from celerp.services import peer_documents

    tok, firm_tok, client_company, credential, contacts = await _paired(real_client, real_engine)
    done = await _client_activity(real_client, real_engine, tok, contacts)
    _route_to_app(monkeypatch, [])
    await _sync(client_company, credential)
    before = await _stock(real_client, firm_tok)
    r = await real_client.delete(f"/docs/{done['received']}/receive", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert await _stock(real_client, tok) != before

    real_commit = peer_documents.commit_peer_revision

    async def fails_once(session, **kwargs):
        monkeypatch.setattr(peer_documents, "commit_peer_revision", real_commit)
        raise ValueError("The record failed after its receipt was undone.")

    monkeypatch.setattr(peer_documents, "commit_peer_revision", fails_once)
    results = await _sync(client_company, credential)

    assert any(r.errors for r in results)
    assert await _stock(real_client, firm_tok) == before
    results = await _sync(client_company, credential)
    assert [(r.entity.value, r.attention) for r in results if r.errors or r.attention] == []
    assert await _stock(real_client, firm_tok) == await _stock(real_client, tok)


async def test_receipt_before_the_copy_is_not_received_twice(real_client, real_engine, monkeypatch):
    """Goods received before the copy was taken are already in the copy; the bill's next
    change does not receive them again."""
    held: dict = {}

    async def receive_first(tok, contacts):
        items = (await real_client.get("/items", params={"limit": 500}, headers=auth(tok))).json()["items"]
        item = sorted(items, key=lambda i: i["sku"])[0]
        vendor = await real_client.post("/crm/contacts", json={
            "name": "Early Supply", "contact_type": "vendor"}, headers=auth(tok))
        r = await real_client.post("/docs", json={
            "doc_type": "purchase_order", "contact_id": vendor.json()["id"],
            "line_items": [{"item_id": _iid(item), "name": item["name"], "quantity": 4,
                            "unit_price": 10.0, "line_total": 40.0}],
            "total": 40.0, "idempotency_key": "before-copy:po"}, headers=auth(tok))
        assert r.status_code in (200, 201), r.text
        held["po"] = r.json()["id"]
        await _receive(real_client, real_engine, tok, held["po"], item, "before-copy")

    tok, firm_tok, client_company, credential, _ = await _paired(
        real_client, real_engine, before_copy=receive_first)
    r = await real_client.patch(f"/docs/{held['po']}", json={"notes": "Checked against the delivery note"},
                                headers=auth(tok))
    assert r.status_code == 200, r.text
    _route_to_app(monkeypatch, [])

    results = await _sync(client_company, credential)

    assert [(r.entity.value, r.attention) for r in results if r.errors or r.attention] == []
    assert await _stock(real_client, firm_tok) == await _stock(real_client, tok)
    assert await _books(real_client, firm_tok) == await _books(real_client, tok)
