# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Received documents: imports are kept apart from our own books until booked."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import json
import pytest
from httpx import AsyncClient


async def _token(client: AsyncClient) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Buyer Co", "email": f"rcv-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _doc(doc_type: str = "invoice", number: str = "INV-77", price: float = 100.0) -> dict:
    return {
        "doc_type": doc_type,
        "ref_id": number,
        "company_name": "Sender Ltd",
        "company_email": "billing@sender.example.com",
        "contact_name": "Buyer Co",
        "contact_id": "contact:sender-side-id",
        "currency": "USD",
        "issue_date": "2026-09-01",
        "due_date": "2026-09-30",
        "line_items": [{"description": "Widget", "sku": "SENDER-SKU", "item_id": "item:sender",
                        "quantity": 2, "unit_price": price}],
    }


def _bundle(doc: dict, *, installation: str | None = "inst-a", document: str = "doc:S-1",
            revision: int | None = 1, company: str | None = None) -> dict:
    bundle: dict = {"version": 1, "doc": doc}
    if installation is not None:
        bundle["source"] = {"installation": installation, "document": document, "revision": revision}
        if company is not None:
            bundle["source"]["company"] = company
    return bundle


async def _import(client: AsyncClient, tok: str, bundle: dict) -> str:
    r = await client.post("/docs/import-bundle", json=bundle, headers=_h(tok), follow_redirects=False)
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith("/docs/received/rcv:")
    return location.rsplit("/", 1)[-1]


async def _received(client: AsyncClient, tok: str, rid: str) -> dict:
    r = await client.get(f"/docs/received/{rid}", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _book(client: AsyncClient, tok: str, rid: str) -> str:
    r = await client.post(f"/docs/received/{rid}/book", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _target(client: AsyncClient, tok: str, target: str) -> dict:
    path = "/lists" if target.startswith("list:") else "/docs"
    r = await client.get(f"{path}/{target}", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _update(client: AsyncClient, tok: str, rid: str):
    return await client.post(f"/docs/received/{rid}/update-draft", headers=_h(tok))


async def _all_docs(session) -> list:
    from sqlalchemy import select

    from celerp.models.projections import Projection

    return (await session.execute(
        select(Projection).where(Projection.entity_type == "doc")
    )).scalars().all()


# ---------------------------------------------------------------------------
# Received is its own entity, outside our books
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_is_kept_in_received_not_among_our_documents(client, session):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc()))

    docs = await client.get("/docs", headers=_h(tok))
    assert docs.status_code == 200
    assert "INV-77" not in docs.text
    assert not [p for p in await _all_docs(session) if (p.state or {}).get("ref_id") == "INV-77"]

    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry
    jes = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_type == "journal_entry",
                                  LedgerEntry.data["source_doc_id"].as_string() == rid)
    )).scalars().all()
    assert jes == []

    listed = (await client.get("/docs/received", headers=_h(tok))).json()["items"]
    row = next(i for i in listed if i["id"] == rid)
    assert row["sender_name"] == "Sender Ltd"
    assert row["doc_type"] == "invoice"
    assert row["sender_doc_number"] == "INV-77"
    assert row["issue_date"] == "2026-09-01"
    assert row["due_date"] == "2026-09-30"
    assert row["total"] == 200.0
    assert row["revision_state"] == "unbooked"
    assert row["book_target"] == {"kind": "doc", "type": "bill"}


@pytest.mark.asyncio
async def test_received_list_shows_the_original_share_link(client):
    tok = await _token(client)
    link = "https://shop.example.com/share/abc123"
    body = json.dumps(_bundle(_doc())).encode()
    with patch("celerp_docs.routes_share.validate_public_base_url", new=AsyncMock(return_value=link)), \
         patch("celerp.services.outbound_url.fetch_public_bytes",
               new=AsyncMock(return_value=MagicMock(status_code=200, content=body, headers={}))):
        r = await client.get("/docs/import", params={"link": link}, headers=_h(tok), follow_redirects=False)
    rid = r.headers["location"].rsplit("/", 1)[-1]
    assert (await _received(client, tok, rid))["source_link"] == link


@pytest.mark.asyncio
async def test_same_document_through_a_refreshed_link_keeps_the_new_link(client):
    tok = await _token(client)
    body = json.dumps(_bundle(_doc())).encode()
    rids = []
    for link in ("https://shop.example.com/share/old1", "https://shop.example.com/share/new2"):
        with patch("celerp_docs.routes_share.validate_public_base_url", new=AsyncMock(return_value=link)), \
             patch("celerp.services.outbound_url.fetch_public_bytes",
                   new=AsyncMock(return_value=MagicMock(status_code=200, content=body, headers={}))):
            r = await client.get("/docs/import", params={"link": link}, headers=_h(tok), follow_redirects=False)
        rids.append(r.headers["location"].rsplit("/", 1)[-1])
    assert rids[0] == rids[1]
    received = await _received(client, tok, rids[0])
    assert received["source_link"] == "https://shop.example.com/share/new2"
    assert received["revision_count"] == 1


# ---------------------------------------------------------------------------
# Identity and revisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_identity_is_sender_installation_plus_document(client):
    tok = await _token(client)
    first = await _import(client, tok, _bundle(_doc(), installation="inst-a", document="doc:S-1"))
    same = await _import(client, tok, _bundle(_doc(), installation="inst-a", document="doc:S-1"))
    other_sender = await _import(client, tok, _bundle(_doc(), installation="inst-b", document="doc:S-1"))
    other_doc = await _import(client, tok, _bundle(_doc(), installation="inst-a", document="doc:S-2"))

    assert same == first
    assert len({first, other_sender, other_doc}) == 3
    assert (await _received(client, tok, first))["revision_count"] == 1


@pytest.mark.asyncio
async def test_two_companies_on_one_installation_are_two_senders(client):
    """Entity ids are per company, so two companies on one installation can
    share one; the sender company keeps their documents apart."""
    tok = await _token(client)
    a = await _import(client, tok, _bundle(_doc(), installation="inst-a", company="co-1", document="doc:INV-1"))
    b = await _import(client, tok, _bundle(_doc(), installation="inst-a", company="co-2", document="doc:INV-1"))
    assert a != b
    assert len([i for i in (await client.get("/docs/received", headers=_h(tok))).json()["items"]
                if i["id"] in (a, b)]) == 2


@pytest.mark.asyncio
async def test_same_number_from_different_senders_is_never_merged(client):
    """No fuzzy matching: equal document numbers and totals from two senders stay apart."""
    tok = await _token(client)
    a = await _import(client, tok, _bundle(_doc(number="INV-1"), installation="inst-a", document="doc:1"))
    b = await _import(client, tok, _bundle(_doc(number="INV-1"), installation="inst-b", document="doc:9"))
    assert a != b


@pytest.mark.asyncio
async def test_newer_revision_updates_the_record_and_keeps_history(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    again = await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    assert again == rid

    received = await _received(client, tok, rid)
    assert received["total"] == 300.0
    assert received["revision_count"] == 2
    assert [r["total"] for r in received["revisions"]] == [300.0, 200.0]


@pytest.mark.asyncio
async def test_older_revision_arriving_late_is_history_only(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    await _import(client, tok, _bundle(_doc(price=100.0), revision=1))

    received = await _received(client, tok, rid)
    assert received["total"] == 300.0
    assert received["revision_count"] == 2


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

MAPPING = [
    ("invoice", "doc", "bill"),
    ("proforma", "doc", "purchase_order"),
    ("quotation", "doc", "purchase_order"),
    ("purchase_order", "list", "quotation"),
    ("memo", "doc", "consignment_in"),
    ("consignment_in", "doc", "memo"),
]


def test_mapping_covers_every_shareable_type():
    from celerp_docs.received import BOOK_TARGETS

    assert {k: tuple(v) for k, v in BOOK_TARGETS.items()} == {s: (k, t) for s, k, t in MAPPING}


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_type,kind,local_type", MAPPING)
async def test_book_and_update_each_mapping(client, sender_type, kind, local_type):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(doc_type=sender_type, number="S-9", price=100.0), revision=1))
    assert (await _received(client, tok, rid))["book_target"] == {"kind": kind, "type": local_type}

    r = await client.post(f"/docs/received/{rid}/book", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == kind
    target = r.json()["id"]
    assert target.startswith("list:" if kind == "list" else "doc:")
    received = await _received(client, tok, rid)
    assert received["revision_state"] == "booked"
    assert received["booked_kind"] == kind

    made = await _target(client, tok, target)
    assert made.get("list_type" if kind == "list" else "doc_type") == local_type
    assert made["status"] == "draft"
    assert made["reference"] == "S-9"
    assert made["source_received_id"] == rid
    assert made["total"] == 200.0

    await _import(client, tok, _bundle(_doc(doc_type=sender_type, number="S-9", price=150.0), revision=2))
    assert (await _received(client, tok, rid))["revision_state"] == "update_available"
    r = await _update(client, tok, rid)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": target, "kind": kind}
    assert (await _target(client, tok, target))["total"] == 300.0
    assert (await _received(client, tok, rid))["revision_state"] == "booked"


@pytest.mark.asyncio
@pytest.mark.parametrize("doc_type", ["bill", "credit_note"])
async def test_unsupported_types_cannot_be_booked(client, doc_type):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(doc_type=doc_type)))
    received = await _received(client, tok, rid)
    assert received["revision_state"] == "not_bookable"
    assert received["book_target"] is None
    r = await client.post(f"/docs/received/{rid}/book", headers=_h(tok))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_book_creates_our_own_draft_linked_back(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(number="INV-77")))
    target = await _book(client, tok, rid)

    doc = (await client.get(f"/docs/{target}", headers=_h(tok))).json()
    assert doc["doc_type"] == "bill"
    assert doc["status"] == "draft"
    assert doc["reference"] == "INV-77"
    assert doc["ref_id"] != "INV-77"
    assert doc["source_received_id"] == rid
    assert doc["contact_name"] == "Sender Ltd"
    # Nothing that names an entity in the sender's system comes across.
    assert doc.get("contact_id") != "contact:sender-side-id"
    assert "sender" not in json.dumps(doc.get("line_items")).lower()

    received = await _received(client, tok, rid)
    assert received["booked_id"] == target
    assert received["revision_state"] == "booked"


@pytest.mark.asyncio
async def test_book_is_idempotent(client, session):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc()))
    first = await _book(client, tok, rid)
    second = await _book(client, tok, rid)
    assert first == second
    booked = [p for p in await _all_docs(session) if (p.state or {}).get("source_received_id") == rid]
    assert len(booked) == 1


@pytest.mark.asyncio
async def test_book_retry_after_the_draft_was_made_reuses_it(client, session, monkeypatch):
    """The draft committed but recording the booking failed: the retry finds
    the same draft through its create key instead of making a second one."""
    from fastapi import HTTPException

    from celerp_docs import received as rcv

    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc()))
    real = rcv._emit

    async def _fail_once(*args, **kwargs):
        monkeypatch.setattr(rcv, "_emit", real)
        raise HTTPException(status_code=503, detail="unavailable")

    monkeypatch.setattr(rcv, "_emit", _fail_once)
    r = await client.post(f"/docs/received/{rid}/book", headers=_h(tok))
    assert r.status_code == 503
    await session.rollback()
    target = await _book(client, tok, rid)
    booked = [p for p in await _all_docs(session) if (p.state or {}).get("source_received_id") == rid]
    assert [p.entity_id for p in booked] == [target]
    assert (await _received(client, tok, rid))["revision_state"] == "booked"


@pytest.mark.asyncio
async def test_list_create_replay_does_not_use_a_number(client):
    tok = await _token(client)
    body = {"list_type": "quotation", "idempotency_key": f"k-{uuid.uuid4().hex}"}
    first = await client.post("/lists", headers=_h(tok), json=body)
    again = await client.post("/lists", headers=_h(tok), json=body)
    assert first.status_code == again.status_code == 200, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["event_id"] == first.json()["event_id"]
    nxt = (await client.post("/lists", headers=_h(tok), json={"list_type": "quotation"})).json()["id"]

    def _n(entity_id: str) -> int:
        return int("".join(ch for ch in entity_id.rsplit("-", 1)[-1] if ch.isdigit()))

    assert _n(nxt) == _n(first.json()["id"]) + 1


@pytest.mark.asyncio
async def test_update_clears_fields_the_sender_removed(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(), revision=1))
    target = await _book(client, tok, rid)
    made = await _target(client, tok, target)
    assert made["due_date"] == "2026-09-30"
    assert made["contact_email"] == "billing@sender.example.com"

    revised = _doc(price=150.0)
    del revised["due_date"], revised["company_email"]
    await _import(client, tok, _bundle(revised, revision=2))
    r = await _update(client, tok, rid)
    assert r.status_code == 200, r.text
    doc = await _target(client, tok, target)
    assert doc.get("due_date") is None
    assert doc.get("contact_email") is None
    assert doc["total"] == 300.0


@pytest.mark.asyncio
async def test_update_never_touches_our_own_fields(client, session):
    """Fields we set on the draft that the sender does not manage survive an update."""
    from sqlalchemy import select

    from celerp.models.projections import Projection

    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(), revision=1))
    target = await _book(client, tok, rid)
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    row = (await session.execute(select(Projection).where(Projection.entity_id == target))).scalar_one()
    before = dict(row.state)
    assert (await _update(client, tok, rid)).status_code == 200
    after = await _target(client, tok, target)
    for key in ("ref_id", "status", "source_received_id", "doc_type"):
        assert after[key] == before[key]


@pytest.mark.asyncio
async def test_stale_expected_version_is_rejected(client):
    tok = await _token(client)
    created = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "bill", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    doc_id = created.json()["id"]
    version = created.json()["event_id"]
    first = await client.patch(f"/docs/{doc_id}", headers=_h(tok), json={
        "fields_changed": {"notes": {"old": None, "new": "one"}}, "expected_version": version,
    })
    assert first.status_code == 200, first.text
    stale = await client.patch(f"/docs/{doc_id}", headers=_h(tok), json={
        "fields_changed": {"notes": {"old": "one", "new": "two"}}, "expected_version": version,
    })
    assert stale.status_code == 409
    assert (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()["notes"] == "one"


@pytest.mark.asyncio
async def test_local_edit_racing_update_draft_wins(client, monkeypatch):
    """A local edit that lands after Update draft read the draft, but before it
    wrote, is kept: the update is rejected and the draft needs reconciling."""
    from celerp_docs import received as rcv

    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    real = rcv._fresh

    async def _read_then_edit(session, company_id, entity_id):
        row = await real(session, company_id, entity_id)
        edit = await client.patch(f"/docs/{target}", headers=_h(tok), json={
            "fields_changed": {"notes": {"old": None, "new": "edited here"}},
        })
        assert edit.status_code == 200, edit.text
        return row

    monkeypatch.setattr(rcv, "_fresh", _read_then_edit)
    r = await _update(client, tok, rid)
    assert r.status_code == 409
    monkeypatch.setattr(rcv, "_fresh", real)
    doc = await _target(client, tok, target)
    assert doc["notes"] == "edited here"
    assert doc["total"] == 200.0
    assert (await _received(client, tok, rid))["revision_state"] == "needs_reconciliation"


@pytest.mark.asyncio
async def test_update_retry_after_patch_committed_recovers_its_version(client, session, monkeypatch):
    """The draft was patched but recording the update failed. The retry finds
    the patch by its key, records that version, and the draft stays updatable."""
    from fastapi import HTTPException

    from celerp_docs import received as rcv

    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    real = rcv._emit

    async def _fail_once(*args, **kwargs):
        monkeypatch.setattr(rcv, "_emit", real)
        raise HTTPException(status_code=503, detail="unavailable")

    monkeypatch.setattr(rcv, "_emit", _fail_once)
    assert (await _update(client, tok, rid)).status_code == 503
    await session.rollback()
    assert (await _target(client, tok, target))["total"] == 300.0
    assert (await _received(client, tok, rid))["revision_state"] == "needs_reconciliation"

    r = await _update(client, tok, rid)
    assert r.status_code == 200, r.text
    assert (await _received(client, tok, rid))["revision_state"] == "booked"
    await _import(client, tok, _bundle(_doc(price=175.0), revision=3))
    assert (await _received(client, tok, rid))["revision_state"] == "update_available"
    assert (await _update(client, tok, rid)).status_code == 200
    assert (await _target(client, tok, target))["total"] == 350.0


@pytest.mark.asyncio
async def test_book_never_matches_an_existing_local_document(client):
    tok = await _token(client)
    local = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "bill", "contact_name": "Sender Ltd", "reference": "INV-77",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 100.0}],
    })
    assert local.status_code == 200
    rid = await _import(client, tok, _bundle(_doc(number="INV-77")))
    assert await _book(client, tok, rid) != local.json()["id"]


# ---------------------------------------------------------------------------
# Sender revisions after booking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revision_on_untouched_draft_offers_update(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))

    assert (await _received(client, tok, rid))["revision_state"] == "update_available"
    r = await client.post(f"/docs/received/{rid}/update-draft", headers=_h(tok))
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{target}", headers=_h(tok))).json()
    assert doc["total"] == 300.0
    assert (await _received(client, tok, rid))["revision_state"] == "booked"


@pytest.mark.asyncio
async def test_revision_on_locally_edited_draft_needs_reconciliation(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    edit = await client.patch(f"/docs/{target}", headers=_h(tok), json={
        "fields_changed": {"notes": {"old": None, "new": "checked against delivery"}},
    })
    assert edit.status_code == 200, edit.text
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))

    assert (await _received(client, tok, rid))["revision_state"] == "needs_reconciliation"
    r = await client.post(f"/docs/received/{rid}/update-draft", headers=_h(tok))
    assert r.status_code == 409
    doc = (await client.get(f"/docs/{target}", headers=_h(tok))).json()
    assert doc["total"] == 200.0


@pytest.mark.asyncio
async def test_revision_after_target_left_draft_is_review_only(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    assert (await client.post(f"/docs/{target}/void", headers=_h(tok), json={})).status_code == 200
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))

    received = await _received(client, tok, rid)
    assert received["revision_state"] == "review_only"
    assert received["total"] == 300.0
    r = await client.post(f"/docs/received/{rid}/update-draft", headers=_h(tok))
    assert r.status_code == 409
    doc = (await client.get(f"/docs/{target}", headers=_h(tok))).json()
    assert doc["status"] == "void"
    assert doc["total"] == 200.0


# ---------------------------------------------------------------------------
# /docs/import: one share link, two input forms
# ---------------------------------------------------------------------------

def _fetch_ok():
    body = json.dumps(_bundle(_doc())).encode()
    return AsyncMock(return_value=MagicMock(status_code=200, content=body, headers={}))


@pytest.mark.asyncio
async def test_import_accepts_link_or_legacy_src_and_token_as_one_path(client):
    tok = await _token(client)
    page = "https://shop.example.com/share/abc123"
    fetch = _fetch_ok()
    with patch("celerp_docs.routes_share.validate_public_base_url", new=AsyncMock(return_value=page)), \
         patch("celerp.services.outbound_url.fetch_public_bytes", new=fetch):
        by_link = await client.get("/docs/import", params={"link": page}, headers=_h(tok), follow_redirects=False)
        by_pair = await client.get("/docs/import", params={"src": "https://shop.example.com/", "token": "abc123"},
                                   headers=_h(tok), follow_redirects=False)
    assert by_link.status_code == 302 and by_pair.status_code == 302
    assert [c.args[0] for c in fetch.await_args_list] == [f"{page}/bundle", f"{page}/bundle"]
    assert by_pair.headers["location"] == by_link.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"link": "https://shop.example.com/share/abc123", "token": "abc123"},
    {"link": "https://shop.example.com/share/abc123", "src": "https://shop.example.com"},
    {"src": "https://shop.example.com"},
    {"token": "abc123"},
    {},
    {"src": "https://shop.example.com", "token": "../../admin"},
])
async def test_import_rejects_mixed_or_incomplete_input(client, params):
    tok = await _token(client)
    fetch = _fetch_ok()
    with patch("celerp.services.outbound_url.fetch_public_bytes", new=fetch):
        r = await client.get("/docs/import", params=params, headers=_h(tok), follow_redirects=False)
    assert r.status_code in (400, 422)
    fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sender side: stable identity travels in the bundle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bundle_carries_stable_source_identity(client, monkeypatch):
    from celerp.config import settings

    monkeypatch.setattr(settings, "gateway_instance_id", "sender-instance-1")
    tok = await _token(client)
    created = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    entity_id = created.json()["id"]
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    source = (await client.get(f"/share/{token}/bundle")).json()["source"]

    assert source["installation"] == hashlib.sha256(b"sender-instance-1").hexdigest()
    assert source["document"] == entity_id
    assert isinstance(source["revision"], int)
    # Identity does not depend on the link: revoking and re-sharing keeps it.
    await client.delete(f"/docs/{entity_id}/share", headers=_h(tok))
    token2 = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    again = (await client.get(f"/share/{token2}/bundle")).json()["source"]
    assert (again["installation"], again["document"]) == (source["installation"], source["document"])


@pytest.mark.asyncio
async def test_two_sender_companies_on_one_installation_stay_apart(client, monkeypatch):
    from celerp.config import settings

    monkeypatch.setattr(settings, "gateway_instance_id", "sender-instance-1")
    first = await _token(client)

    async def _company(name: str) -> str:
        r = await client.post("/companies", json={"name": name}, headers=_h(first))
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    sources = []
    for tok in (first, await _company("Second Co")):
        created = await client.post("/docs", headers=_h(tok), json={
            "doc_type": "invoice", "contact_name": "ACME",
            "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
        })
        entity_id = created.json()["id"]
        token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
        sources.append((await client.get(f"/share/{token}/bundle")).json())
    a, b = (s["source"] for s in sources)
    assert a["installation"] == b["installation"]
    assert a["document"] == b["document"]
    assert a["company"] and b["company"] and a["company"] != b["company"]

    buyer = await _company("Receiving Co")
    assert await _import(client, buyer, sources[0]) != await _import(client, buyer, sources[1])


def test_print_import_link_fills_in_the_page_address():
    """The Import link on a share page carries the address the page is read
    from, so a relay-hosted page links back to the relay address."""
    from celerp.output.doc_print import import_accept_url, render_doc_print_html

    assert import_accept_url("https://share.celerp.com/x/y") == \
        "https://www.celerp.com/accept?link=https%3A%2F%2Fshare.celerp.com%2Fx%2Fy"
    html = render_doc_print_html({"doc_type": "invoice", "line_items": []},
                                 import_url="https://www.celerp.com/accept", import_from_page=True)
    assert 'id="dp-import"' in html
    assert "encodeURIComponent(location.origin+location.pathname)" in html
    plain = render_doc_print_html({"doc_type": "invoice", "line_items": []},
                                  import_url="https://www.celerp.com/accept?link=x")
    assert "location.origin" not in plain


# ---------------------------------------------------------------------------
# Legacy imports
# ---------------------------------------------------------------------------

async def _legacy_import(session, company_id, entity_id: str, data: dict):
    from celerp.events.engine import emit_event

    await emit_event(
        session, company_id=company_id, entity_id=entity_id, entity_type="doc",
        event_type="doc.shared_import", data=data, actor_id=None, location_id=None,
        source="share_import", idempotency_key=f"share:{uuid.uuid4().hex}:{company_id}", metadata_={},
    )


@pytest.mark.asyncio
async def test_legacy_untouched_import_moves_to_received_and_survives_rebuild(client, session):
    from sqlalchemy import select

    from celerp.migrations._data_reconcile import set_meta
    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp_docs.received import received_id
    from celerp_docs.received_legacy import LEGACY_RECEIVED_KEY, move_legacy_imports

    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])
    untouched, worked = "doc:rcv:legacy00001", "doc:rcv:legacy00002"
    await _legacy_import(session, company_id, untouched, {
        "doc_type": "invoice", "ref_id": "OLD-1", "company_name": "Old Sender", "total": 50.0,
        "line_items": [], "source_share_token": "tok1", "source_origin": "https://old.example.com",
    })
    await _legacy_import(session, company_id, worked, {
        "doc_type": "invoice", "ref_id": "OLD-2", "company_name": "Old Sender", "total": 60.0, "line_items": [],
    })
    await session.commit()
    note = await client.post(f"/docs/{worked}/notes", headers=_h(tok), json={"note": "checked"})
    assert note.status_code == 200, note.text

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, ""))
    result = await move_legacy_imports(session)
    await session.commit()
    assert result["moved"] == 1
    assert (await move_legacy_imports(session))["changed"] is False

    rid = received_id("legacy", "", untouched)
    await ProjectionEngine.rebuild(session, company_id)
    await session.commit()
    rows = {p.entity_id: p for p in (await session.execute(
        select(Projection).where(Projection.company_id == company_id)
    )).scalars().all()}
    assert untouched not in rows
    moved = rows[rid]
    assert moved.entity_type == "received_document"
    assert moved.state["sender_doc_number"] == "OLD-1"
    assert moved.state["source_link"] == "https://old.example.com/share/tok1"
    assert "source_share_token" not in moved.state["document"]
    assert rows[worked].entity_type == "doc"
    assert rows[worked].state["status"] == "received"

    listed = (await client.get("/docs/received", headers=_h(tok))).json()["items"]
    assert [i["id"] for i in listed if i["sender_doc_number"] == "OLD-1"] == [rid]


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doctor_does_not_invent_an_entry_for_a_sent_draft(client):
    """A draft invoice that was only sent was never finalized, so no sales
    entry is missing and Doctor must not post one."""
    tok = await _token(client)
    created = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    inv = created.json()["id"]
    sent = await client.post(f"/docs/{inv}/send", headers=_h(tok), json={})
    assert sent.status_code == 200, sent.text
    assert (await client.get(f"/docs/{inv}", headers=_h(tok))).json()["status"] == "sent"

    r = await client.post("/admin/doctor?checks=missing_jes", headers=_h(tok))
    missing = next(c for c in r.json()["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 0
