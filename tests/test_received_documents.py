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
from celerp.services.permissions import missing_permission_text
from ui.i18n import t


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


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_type,kind,local_type", MAPPING)
async def test_booking_needs_the_sales_price_permission_only_for_a_sales_target(client, session, sender_type, kind, local_type):
    """A role that may edit documents but not set sales prices can book what it
    buys (bill, purchase order, consignment in) at the sender's prices, and keep
    the draft up to date. A sales target (a quotation, a memo) still needs
    set_sales_doc_prices, since its prices become ours."""
    from test_helpers import grant_permission, perm_setup

    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")
    admin = ctx["admin_h"]["Authorization"].split()[1]
    operator = ctx["operator_h"]["Authorization"].split()[1]
    rid = await _import(client, admin, _bundle(_doc(doc_type=sender_type, number="S-9", price=100.0), revision=1))

    r = await client.post(f"/docs/received/{rid}/book", headers=_h(operator))
    if local_type in {"bill", "purchase_order", "consignment_in"}:
        assert r.status_code == 200, r.text
        await _import(client, admin, _bundle(_doc(doc_type=sender_type, number="S-9", price=150.0), revision=2))
        assert (await _update(client, operator, rid)).status_code == 200
        made = await _target(client, admin, r.json()["id"])
        assert made["line_items"][0]["unit_price"] == 150.0
    else:
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == missing_permission_text("set_sales_doc_prices")


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
async def test_booking_twice_makes_one_draft(client, session):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc()))
    first = await _book(client, tok, rid)
    second = await _book(client, tok, rid)
    assert first == second
    booked = [p for p in await _all_docs(session) if (p.state or {}).get("source_received_id") == rid]
    assert len(booked) == 1


@pytest.mark.asyncio
async def test_booking_again_after_a_failed_finish_reuses_the_draft(client, session, monkeypatch):
    """The draft committed but recording the booking failed: booking again
    returns the same draft instead of making a second one."""
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
async def test_list_create_sent_twice_uses_one_number(client):
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
async def test_updating_again_after_a_failed_finish_keeps_the_draft_updatable(client, session, monkeypatch):
    """The draft was patched but recording the update failed. Updating again
    records that version, and the draft stays updatable."""
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


async def _false_sales_entry(session, company_id, doc_id: str, total: float) -> str:
    """The entry an earlier Doctor posted for an import from its status alone."""
    from celerp.services import auto_je

    await auto_je.create_for_doc_finalized(
        session, company_id=company_id, user_id=None, doc_id=doc_id,
        doc={"doc_type": "invoice", "total": total, "currency": "USD", "issue_date": "2026-01-05", "line_items": []},
    )
    return f"je:auto:{doc_id}:fin"


@pytest.mark.asyncio
async def test_legacy_import_with_an_accounting_entry_stays_a_document(client, session):
    """An import an earlier Doctor posted a sales entry for is not moved: the
    entry names it only through its own id and metadata, and moving the import
    would leave the entry pointing at nothing."""
    from celerp.migrations._data_reconcile import set_meta
    from celerp.models.projections import Projection
    from celerp_docs.received_legacy import LEGACY_RECEIVED_KEY, move_legacy_imports

    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])
    posted = f"doc:rcv:legacy{uuid.uuid4().hex[:8]}"
    await _legacy_import(session, company_id, posted, {
        "doc_type": "invoice", "ref_id": "OLD-3", "company_name": "Old Sender", "total": 70.0, "line_items": [],
    })
    je_id = await _false_sales_entry(session, company_id, posted, 70.0)
    await session.commit()

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, ""))
    await move_legacy_imports(session)
    await session.commit()

    doc = await session.get(Projection, {"company_id": company_id, "entity_id": posted})
    assert doc is not None and doc.entity_type == "doc"
    je = await session.get(Projection, {"company_id": company_id, "entity_id": je_id})
    assert je.state["status"] == "posted"


def test_dependency_detection_reads_references_not_substrings():
    from celerp_docs.received_legacy import depended_on

    docs = {"doc:a", "doc:b", "doc:c", "doc:d", "doc:e"}
    rows = [
        ("doc:a", "k1", {}, {}),                                          # its own later event
        ("je:auto:doc:b:fin", "k2", {}, {}),                              # entry id names it
        ("je:x", "je:doc:c:invoice.finalized:c", {}, {}),                 # key names it
        ("pay:1", "k3", {"lines": [{"source": "doc:d"}]}, {"n": ["x"]}),  # nested reference
        ("item:1", "k4", {"note": "see doc:e later"}, {"ref": "doc:ee"}), # text that only contains it
    ]
    assert depended_on(rows, docs) == {"doc:a", "doc:b", "doc:c", "doc:d"}


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


@pytest.mark.asyncio
async def test_doctor_voids_only_recognition_entries_with_no_finalize_or_receipt(client, session):
    """Earlier Doctor versions posted sales and receiving entries from status
    alone, so a legacy import could carry an entry for a sale that never
    happened. Doctor finds exactly those and voids only them."""
    from sqlalchemy import select

    from celerp.models.projections import Projection
    from celerp.services import auto_je

    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])

    legacy_inv, legacy_po = f"doc:rcv:li{uuid.uuid4().hex[:8]}", f"doc:rcv:lp{uuid.uuid4().hex[:8]}"
    await _legacy_import(session, company_id, legacy_inv, {
        "doc_type": "invoice", "ref_id": "OLD-4", "company_name": "Old Sender", "total": 80.0, "line_items": [],
    })
    await _legacy_import(session, company_id, legacy_po, {
        "doc_type": "purchase_order", "ref_id": "OLD-PO", "company_name": "Old Sender", "total": 40.0, "line_items": [],
    })
    false_sale = await _false_sales_entry(session, company_id, legacy_inv, 80.0)
    await auto_je.create_for_po_received(
        session, company_id=company_id, user_id=None, po_id=legacy_po, total=40.0,
        doc={"doc_type": "purchase_order", "total": 40.0, "currency": "USD"}, receive_date="2026-01-05",
    )
    await session.commit()
    false_receipt = next(p.entity_id for p in (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id, Projection.entity_id.like(f"je:auto:{legacy_po}:%"))
    )).scalars().all())

    created = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    finalized = created.json()["id"]
    assert (await client.post(f"/docs/{finalized}/finalize", headers=_h(tok))).status_code == 200
    imported = f"doc:imp-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=_h(tok), json={
        "entity_id": imported, "event_type": "doc.created", "source": "test",
        "idempotency_key": f"imp-{uuid.uuid4().hex}",
        "data": {"doc_type": "invoice", "status": "final", "total": 30.0, "currency": "USD",
                 "issue_date": "2026-02-01", "line_items": []},
    })
    assert r.status_code == 200, r.text

    def _check(resp):
        assert resp.status_code == 200, resp.text
        return next(c for c in resp.json()["results"] if c["check"] == "uncaused_recognition_jes")

    dry = _check(await client.post("/admin/doctor?checks=uncaused_recognition_jes", headers=_h(tok)))
    assert sorted(d["je_id"] for d in dry["details"]) == sorted([false_sale, false_receipt])
    assert dry["fixed"] == 0

    fixed = _check(await client.post("/admin/doctor?fix=true&checks=uncaused_recognition_jes", headers=_h(tok)))
    assert fixed["fixed"] == 2

    session.expire_all()
    status = {p.entity_id: p.state.get("status") for p in (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry")
    )).scalars().all()}
    assert status[false_sale] == "void" and status[false_receipt] == "void"
    assert status[f"je:auto:{finalized}:fin"] == "posted"
    assert status[f"je:auto:{imported}:fin"] == "posted"

    again = _check(await client.post("/admin/doctor?fix=true&checks=uncaused_recognition_jes", headers=_h(tok)))
    assert again["found"] == 0


@pytest.mark.asyncio
async def test_doctor_reports_an_uncaused_entry_in_a_locked_period_instead_of_voiding(client, session):
    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])
    legacy = f"doc:rcv:lk{uuid.uuid4().hex[:8]}"
    await _legacy_import(session, company_id, legacy, {
        "doc_type": "invoice", "ref_id": "OLD-5", "company_name": "Old Sender", "total": 20.0, "line_items": [],
    })
    je_id = await _false_sales_entry(session, company_id, legacy, 20.0)
    await session.commit()
    locked = await client.post("/accounting/period-lock", headers=_h(tok), json={"lock_date": "2026-06-30"})
    assert locked.status_code == 200, locked.text

    r = await client.post("/admin/doctor?fix=true&checks=uncaused_recognition_jes", headers=_h(tok))
    check = next(c for c in r.json()["results"] if c["check"] == "uncaused_recognition_jes")
    assert check["fixed"] == 0 and check["auto_fixable"] is False
    assert [d["je_id"] for d in check["details"]] == [je_id]
    assert "locked" in check["details"][0]["blocked_reason"].lower()


async def _issued_create(session, company_id, doc_id: str, total: float) -> None:
    """A doc.created that already reads as a finalized invoice but did not come
    through a snapshot import (a connector copy, or a create from before
    creation was draft-only)."""
    from celerp.events.engine import emit_event

    await emit_event(
        session, company_id=company_id, entity_id=doc_id, entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "status": "final", "total": total, "currency": "USD",
              "issue_date": "2026-01-05", "line_items": []},
        actor_id=None, location_id=None, source="api",
        idempotency_key=f"issued:{uuid.uuid4().hex}", metadata_={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["final", "sent", "paid", "received"])
async def test_a_new_document_is_always_created_as_a_draft(client, status):
    tok = await _token(client)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME", "status": status,
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    assert r.status_code == 422, r.text
    assert "draft" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [
    {"finalized": True},
    {"amount_paid": 50.0},
    {"amount_outstanding": 0},
    {"payments": [{"amount": 10.0, "payment_date": "2026-01-05"}]},
    {"received_items": [{"line_index": 0, "quantity": 1}]},
    {"fulfillment_status": "fulfilled"},
    {"converted_to": "doc:INV-1"},
    {"entity_type": "list"},
])
async def test_a_new_document_carries_no_lifecycle_or_payment_state(client, forged):
    """Creation owns no lifecycle or settlement state: a caller cannot create a
    document that already reads as finalized, paid, received or converted."""
    tok = await _token(client)
    before = (await client.get("/docs", headers=_h(tok))).json()
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME", **forged,
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100.0}],
        "subtotal": 100.0, "total": 100.0,
    })
    assert r.status_code == 422, r.text
    assert next(iter(forged)) in r.text
    assert (await client.get("/docs", headers=_h(tok))).json() == before


@pytest.mark.asyncio
async def test_a_created_document_is_unpaid_with_its_total_outstanding_and_finalizes_normally(client):
    tok = await _token(client)
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME", "currency": "USD",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 100.0}],
        "subtotal": 100.0, "total": 100.0,
    })
    doc_id = r.json()["id"]
    doc = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()
    assert (doc["status"], doc["amount_paid"], doc["amount_outstanding"]) == ("draft", 0, 100.0)
    fin = await client.post(f"/docs/{doc_id}/finalize", headers=_h(tok))
    assert fin.status_code == 200, fin.text
    assert fin.json().get("already_finalized") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [{"status": "finalized"}, {"result": "converted"}, {"finalized_at": "2026-01-05"}])
async def test_a_new_list_carries_no_lifecycle_state(client, forged):
    tok = await _token(client)
    r = await client.post("/lists", headers=_h(tok), json={"list_type": "quotation", **forged})
    assert r.status_code == 422, r.text


def test_the_published_create_contract_says_draft_only_and_takes_no_payment_state():
    from celerp_docs.routes import DocCreatePayload, ListCreatePayload

    for model in (DocCreatePayload, ListCreatePayload):
        props = model.model_json_schema()["properties"]
        assert props["status"].get("const") == "draft" or props["status"].get("enum") == ["draft"]
        assert "amount_paid" not in props and "amount_outstanding" not in props


@pytest.mark.asyncio
async def test_doctor_does_not_post_an_entry_for_a_create_that_only_looks_issued(client, session):
    """Only a finalize, a receipt, or a recorded snapshot import owes an entry.
    A doc.created whose payload happens to read "final" is not one of those."""
    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])
    doc_id = f"doc:iss-{uuid.uuid4().hex[:8]}"
    await _issued_create(session, company_id, doc_id, 60.0)
    await session.commit()

    r = await client.post("/admin/doctor?fix=true&checks=missing_jes", headers=_h(tok))
    missing = next(c for c in r.json()["results"] if c["check"] == "missing_jes")
    assert [d for d in missing["details"] if d["doc_id"] == doc_id] == []
    assert missing["fixed"] == 0


@pytest.mark.asyncio
async def test_doctor_holds_an_entry_on_an_unrecorded_issued_create_for_review(client, session):
    """An import from before imports were recorded looks exactly like a create
    that was issued with no finalize. Its entry may be owed, so Doctor reports
    it for review and never voids it on its own."""
    from sqlalchemy import select

    from celerp.models.projections import Projection

    tok = await _token(client)
    company_id = uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])
    doc_id = f"doc:iss-{uuid.uuid4().hex[:8]}"
    await _issued_create(session, company_id, doc_id, 45.0)
    je_id = await _false_sales_entry(session, company_id, doc_id, 45.0)
    await session.commit()

    r = await client.post("/admin/doctor?fix=true&checks=uncaused_recognition_jes", headers=_h(tok))
    check = next(c for c in r.json()["results"] if c["check"] == "uncaused_recognition_jes")
    assert [d["je_id"] for d in check["details"]] == [je_id]
    assert check["fixed"] == 0 and check["auto_fixable"] is False
    assert "review" in check["details"][0]["blocked_reason"].lower()

    session.expire_all()
    je = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_id == je_id))).scalar_one()
    assert je.state.get("status") == "posted"


# ---------------------------------------------------------------------------
# Revision identity
# ---------------------------------------------------------------------------

async def _revisions(session, company_id, rid: str) -> int:
    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry

    return (await session.execute(
        select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_id == rid)
    )).scalar_one()


async def _company_id(client: AsyncClient, tok: str) -> uuid.UUID:
    return uuid.UUID((await client.get("/companies/me", headers=_h(tok))).json()["id"])


@pytest.mark.asyncio
async def test_unnumbered_content_that_comes_back_is_a_new_revision(client):
    """A -> B -> A from a sender without revision numbers: the return to A is
    the current revision, not a repeat of the first one."""
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=None))
    await _import(client, tok, _bundle(_doc(price=150.0), revision=None))
    await _import(client, tok, _bundle(_doc(price=100.0), revision=None))

    received = await _received(client, tok, rid)
    assert received["total"] == 200.0
    assert received["revision_count"] == 3


@pytest.mark.asyncio
async def test_same_content_at_a_newer_revision_becomes_current(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    await _import(client, tok, _bundle(_doc(price=100.0), revision=3))

    received = await _received(client, tok, rid)
    assert received["total"] == 200.0
    assert received["revision_count"] == 3


@pytest.mark.asyncio
async def test_same_revision_with_different_content_is_rejected(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    r = await client.post("/docs/import-bundle", json=_bundle(_doc(price=999.0), revision=1),
                          headers=_h(tok), follow_redirects=False)
    assert r.status_code == 422
    assert "different content" in r.json()["detail"]
    received = await _received(client, tok, rid)
    assert received["total"] == 200.0
    assert received["revision_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [1, None])
async def test_the_same_arrival_again_records_nothing_new(client, session, revision):
    tok = await _token(client)
    company_id = await _company_id(client, tok)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=revision))
    await _import(client, tok, _bundle(_doc(price=150.0), revision=None if revision is None else 2))
    before = await _revisions(session, company_id, rid)
    await _import(client, tok, _bundle(_doc(price=150.0), revision=None if revision is None else 2))
    assert await _revisions(session, company_id, rid) == before
    assert (await _received(client, tok, rid))["revision_count"] == 2


@pytest.mark.asyncio
async def test_bundle_revision_counts_this_documents_own_changes(client):
    """The revision is the document's own event count: it grows with each
    change to the document and not with activity elsewhere in the books."""
    tok = await _token(client)

    async def _create() -> str:
        r = await client.post("/docs", headers=_h(tok), json={
            "doc_type": "invoice", "contact_name": "ACME",
            "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
        })
        assert r.status_code == 200, r.text
        return r.json()["id"]

    entity_id = await _create()
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]

    async def _revision() -> int:
        return (await client.get(f"/share/{token}/bundle")).json()["source"]["revision"]

    first = await _revision()
    await _create()
    assert await _revision() == first
    patched = await client.patch(f"/docs/{entity_id}", headers=_h(tok), json={
        "fields_changed": {"notes": {"old": None, "new": "revised"}},
    })
    assert patched.status_code == 200, patched.text
    assert await _revision() == first + 1


# ---------------------------------------------------------------------------
# Purchase order to quotation: money carries over exactly or not at all
# ---------------------------------------------------------------------------

def _po(**extra) -> dict:
    doc = _doc(doc_type="purchase_order", number="PO-5", price=100.0)
    doc.update(extra)
    return doc


def _tax(rate: float, *, compound: bool = False) -> list[dict]:
    return [{"code": "VAT", "rate": rate, "order": 1, "is_compound": compound, "label": "VAT"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    pytest.param({"currency": "EUR"}, id="foreign-currency"),
    pytest.param({"line_items": [{"description": "Widget", "quantity": 3, "unit_price": 33.33,
                                  "discount_pct": 12.5}]}, id="line-discount"),
    pytest.param({"doc_taxes": _tax(7.0), "discount": 15.0}, id="document-tax"),
    pytest.param({"line_items": [{"description": "Widget", "quantity": 2, "unit_price": 100.0,
                                  "taxes": _tax(10.0)}]}, id="line-tax"),
])
async def test_booked_quotation_keeps_currency_and_total_exactly(client, extra):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_po(**extra)))
    received = await _received(client, tok, rid)
    target = await _book(client, tok, rid)

    made = await _target(client, tok, target)
    assert made["list_type"] == "quotation"
    assert made["currency"] == received["currency"] == extra.get("currency", "USD")
    assert made["total"] == received["total"]
    assert (await _received(client, tok, rid))["revision_state"] == "booked"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, reason", [
    pytest.param({"shipping": 25.0}, "no shipping charge", id="shipping"),
    pytest.param({"doc_taxes": _tax(5.0, compound=True)}, "no compound tax", id="compound-document-tax"),
    pytest.param({"line_items": [{"description": "Widget", "quantity": 2, "unit_price": 100.0,
                                  "taxes": _tax(5.0, compound=True)}]}, "no compound tax", id="compound-line-tax"),
    pytest.param({"doc_taxes": _tax(7.0),
                  "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 100.0,
                                  "taxes": _tax(10.0)}]}, "not both", id="line-and-document-tax"),
])
async def test_quotation_that_would_change_the_total_is_not_booked(client, session, extra, reason):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_po(**extra)))
    r = await client.post(f"/docs/received/{rid}/book", headers=_h(tok))
    assert r.status_code == 422
    assert reason in r.json()["detail"]
    received = await _received(client, tok, rid)
    assert received["revision_state"] == "unbooked"
    assert received["booked_id"] is None


@pytest.mark.asyncio
async def test_document_target_keeps_shipping_and_every_tax_exactly(client):
    tok = await _token(client)
    doc = _doc(price=100.0)
    doc.update(currency="EUR", shipping=12.5, doc_taxes=_tax(5.0, compound=True),
               line_items=[{"description": "Widget", "quantity": 2, "unit_price": 100.0, "taxes": _tax(10.0)}])
    rid = await _import(client, tok, _bundle(doc))
    received = await _received(client, tok, rid)
    made = await _target(client, tok, await _book(client, tok, rid))
    assert made["currency"] == "EUR"
    assert made["total"] == received["total"]
    assert made["shipping"] == 12.5


# ---------------------------------------------------------------------------
# Discount and tax follow the rules every document uses
# ---------------------------------------------------------------------------

def _priced(**extra) -> dict:
    doc = _doc(price=250.0)  # 2 x 250 = 500
    doc.update(extra)
    return doc


def _taxed_line(**tax) -> list[dict]:
    return [{"description": "Widget", "quantity": 2, "unit_price": 250.0, **tax}]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, money", [
    pytest.param({"discount": 10.0, "discount_type": "percentage"},
                 {"subtotal": 500.0, "discount_amount": 50.0, "tax": 0.0, "total": 450.0},
                 id="percentage-discount"),
    pytest.param({"discount": 100.0, "line_items": _taxed_line(taxes=_tax(10.0))},
                 {"subtotal": 500.0, "discount_amount": 100.0, "tax": 40.0, "total": 440.0},
                 id="discount-and-line-tax"),
    pytest.param({"line_items": _taxed_line(tax_rate=7.0)},
                 {"subtotal": 500.0, "discount_amount": 0.0, "tax": 35.0, "total": 535.0},
                 id="legacy-line-tax-rate"),
    pytest.param({"discount": 10.0, "discount_type": "percentage", "tax_rate": 7.0},
                 {"subtotal": 500.0, "discount_amount": 50.0, "tax": 31.5, "total": 481.5},
                 id="legacy-header-tax-rate"),
])
async def test_received_money_follows_document_rules(client, extra, money):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_priced(**extra)))
    document = (await _received(client, tok, rid))["document"]
    assert {k: document[k] for k in money} == money

    made = await _target(client, tok, await _book(client, tok, rid))
    assert {k: made[k] for k in money} == money
    assert made["discount_type"] == extra.get("discount_type", "flat")


@pytest.mark.asyncio
async def test_tax_amount_without_a_rate_is_not_imported(client):
    tok = await _token(client)
    r = await client.post("/docs/import-bundle", json=_bundle(_priced(tax=35.0, total=535.0)),
                          headers=_h(tok), follow_redirects=False)
    assert r.status_code == 422
    assert r.json()["detail"] == t("documents.err_share_tax_no_rate", "en")
    listed = await client.get("/docs/received", headers=_h(tok))
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []


@pytest.mark.asyncio
async def test_quotation_keeps_a_percentage_discount(client):
    tok = await _token(client)
    po = _priced(doc_type="purchase_order", discount=10.0, discount_type="percentage")
    rid = await _import(client, tok, _bundle(po))
    made = await _target(client, tok, await _book(client, tok, rid))
    assert made["list_type"] == "quotation"
    assert (made["discount"], made["discount_type"], made["total"]) == (10.0, "percentage", 450.0)


# ---------------------------------------------------------------------------
# Update draft only when the revision still fits the draft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    pytest.param({"currency": "EUR"}, id="currency"),
    pytest.param({"doc_type": "proforma"}, id="type"),
])
async def test_revision_that_no_longer_fits_the_draft_needs_reconciliation(client, change):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    changed = _doc(price=150.0)
    changed.update(change)
    await _import(client, tok, _bundle(changed, revision=2))

    assert (await _received(client, tok, rid))["revision_state"] == "source_changed"
    r = await _update(client, tok, rid)
    assert r.status_code == 409
    made = await _target(client, tok, target)
    assert (made["doc_type"], made["currency"], made["total"]) == ("bill", "USD", 200.0)


@pytest.mark.asyncio
async def test_quotation_revision_it_cannot_carry_is_not_applied(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_po(), revision=1))
    target = await _book(client, tok, rid)
    await _import(client, tok, _bundle(_po(shipping=25.0), revision=2))

    assert (await _received(client, tok, rid))["revision_state"] == "source_changed"
    assert (await _update(client, tok, rid)).status_code == 409
    assert (await _target(client, tok, target))["total"] == 200.0


# ---------------------------------------------------------------------------
# Mark reconciled
# ---------------------------------------------------------------------------

async def _mark(client: AsyncClient, tok: str, rid: str):
    return await client.post(f"/docs/received/{rid}/mark-reconciled", headers=_h(tok))


@pytest.mark.asyncio
async def test_mark_reconciled_records_the_revision_without_touching_the_draft(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    edit = await client.patch(f"/docs/{target}", headers=_h(tok), json={
        "fields_changed": {"notes": {"old": None, "new": "checked against delivery"}},
    })
    assert edit.status_code == 200, edit.text
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    assert (await _received(client, tok, rid))["revision_state"] == "needs_reconciliation"
    before = await _target(client, tok, target)

    r = await _mark(client, tok, rid)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": target, "kind": "doc"}
    assert await _target(client, tok, target) == before
    assert (await _received(client, tok, rid))["revision_state"] == "booked"
    assert (await _mark(client, tok, rid)).status_code == 200

    await _import(client, tok, _bundle(_doc(price=175.0), revision=3))
    assert (await _received(client, tok, rid))["revision_state"] == "update_available"
    assert (await _update(client, tok, rid)).status_code == 200
    assert (await _target(client, tok, target))["total"] == 350.0


@pytest.mark.asyncio
async def test_mark_reconciled_after_a_change_the_draft_cannot_follow(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    target = await _book(client, tok, rid)
    changed = _doc(price=150.0)
    changed["currency"] = "EUR"
    await _import(client, tok, _bundle(changed, revision=2))
    assert (await _received(client, tok, rid))["revision_state"] == "source_changed"

    assert (await _mark(client, tok, rid)).status_code == 200
    assert (await _received(client, tok, rid))["revision_state"] == "booked"
    assert (await _target(client, tok, target))["currency"] == "USD"


@pytest.mark.asyncio
async def test_mark_reconciled_needs_a_booked_draft(client):
    tok = await _token(client)
    rid = await _import(client, tok, _bundle(_doc(price=100.0), revision=1))
    assert (await _mark(client, tok, rid)).status_code == 409

    target = await _book(client, tok, rid)
    assert (await client.post(f"/docs/{target}/void", headers=_h(tok), json={})).status_code == 200
    await _import(client, tok, _bundle(_doc(price=150.0), revision=2))
    r = await _mark(client, tok, rid)
    assert r.status_code == 409
    assert (await _received(client, tok, rid))["revision_state"] == "review_only"


# ---------------------------------------------------------------------------
# Untrusted numbers and currency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("patch_doc", [
    pytest.param(lambda d: d["line_items"][0].update(unit_price="NaN"), id="line-nan"),
    pytest.param(lambda d: d.update(shipping="Infinity"), id="shipping-inf"),
    pytest.param(lambda d: d.update(doc_taxes=[{"code": "VAT", "rate": 7, "order": "NaN"}]), id="tax-order-nan"),
    pytest.param(lambda d: d.update(discount=float("-inf")), id="discount-neg-inf"),
])
async def test_non_finite_number_is_rejected(client, patch_doc):
    tok = await _token(client)
    doc = _doc()
    patch_doc(doc)
    r = await client.post("/docs/import-bundle", content=json.dumps(_bundle(doc)),
                          headers={**_h(tok), "Content-Type": "application/json"}, follow_redirects=False)
    assert r.status_code == 422
    assert r.json()["detail"] == t("documents.err_share_bad_number", "en")


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", [None, "", "XXX", "usd"])
async def test_bundle_without_a_supported_currency_is_rejected(client, currency):
    tok = await _token(client)
    doc = _doc()
    if currency is None:
        doc.pop("currency")
    else:
        doc["currency"] = currency
    r = await client.post("/docs/import-bundle", json=_bundle(doc), headers=_h(tok), follow_redirects=False)
    assert r.status_code == 422
    assert "currency" in r.json()["detail"]


@pytest.mark.asyncio
async def test_list_with_an_unsupported_currency_is_rejected(client):
    tok = await _token(client)
    r = await client.post("/lists", headers=_h(tok), json={
        "list_type": "quotation", "currency": "XXX",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    assert r.status_code == 422
    assert "currency" in r.json()["detail"]


@pytest.mark.asyncio
async def test_shared_document_without_its_own_currency_goes_out_in_the_company_currency(client, session):
    from sqlalchemy.orm.attributes import flag_modified

    from celerp.models.projections import Projection

    tok = await _token(client)
    created = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}],
    })
    entity_id = created.json()["id"]
    row = await session.get(Projection, {"company_id": await _company_id(client, tok), "entity_id": entity_id})
    row.state.pop("currency", None)
    flag_modified(row, "state")
    await session.commit()
    token = (await client.post(f"/docs/{entity_id}/share", headers=_h(tok))).json()["token"]
    assert (await client.get(f"/share/{token}/bundle")).json()["doc"]["currency"] == "USD"


# ---------------------------------------------------------------------------
# Legacy imports that were shared on stay documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_legacy_import_with_a_share_link_stays_a_document(client, session, revoked):
    """A share link names the import by its id. Active or revoked, the link
    must keep resolving to the same document, so the import is not moved."""
    from datetime import datetime, timezone

    from celerp.migrations._data_reconcile import set_meta
    from celerp.models.projections import Projection
    from celerp.models.share import DocShareToken
    from celerp_docs.received_legacy import LEGACY_RECEIVED_KEY, move_legacy_imports

    tok = await _token(client)
    company_id = await _company_id(client, tok)
    shared = f"doc:rcv:legacy{uuid.uuid4().hex[:8]}"
    await _legacy_import(session, company_id, shared, {
        "doc_type": "invoice", "ref_id": "OLD-9", "company_name": "Old Sender", "total": 90.0, "line_items": [],
    })
    session.add(DocShareToken(
        token=uuid.uuid4().hex[:12], company_id=company_id, entity_id=shared,
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    ))
    await session.commit()

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, ""))
    result = await move_legacy_imports(session)
    await session.commit()

    assert result["moved"] == 0
    doc = await session.get(Projection, {"company_id": company_id, "entity_id": shared})
    assert doc is not None and doc.entity_type == "doc"


@pytest.mark.asyncio
async def test_legacy_import_queued_for_a_connected_platform_stays_a_document(client, session):
    from celerp.migrations._data_reconcile import set_meta
    from celerp.models.connector_config import OutboundQueue
    from celerp.models.projections import Projection
    from celerp_docs.received_legacy import LEGACY_RECEIVED_KEY, move_legacy_imports

    tok = await _token(client)
    company_id = await _company_id(client, tok)
    queued = f"doc:rcv:legacy{uuid.uuid4().hex[:8]}"
    await _legacy_import(session, company_id, queued, {
        "doc_type": "invoice", "ref_id": "OLD-10", "company_name": "Old Sender", "total": 10.0, "line_items": [],
    })
    session.add(OutboundQueue(company_id=str(company_id), connector="shopify", entity_type="doc", entity_id=queued))
    await session.commit()

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, ""))
    await move_legacy_imports(session)
    await session.commit()

    doc = await session.get(Projection, {"company_id": company_id, "entity_id": queued})
    assert doc is not None and doc.entity_type == "doc"
