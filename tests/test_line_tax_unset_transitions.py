# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A line whose tax is still for the user to set ("--") never crosses a step that cannot be undone.

Each case is a document type, a real entry point and the state the document is in. Issuing,
receiving goods, converting into an issued document and importing an issued record are refused
with the line named, and change nothing: no event, no stock, no journal entry, the document as it
was. The same request sent again is refused the same way, and once the tax is chosen that same
request goes through. A conversion into a draft carries "--" across, where the draft's own Issue
refuses it.

The source check in test_line_tax_unset_issue stays as a tripwire for writers added later; the
route check at the end makes every action route on a document or list either a case here or
named with the reason "--" cannot cross it.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_helpers import doc_version
from test_money_stock_and_contact_invariants import _auth_company
from test_tax_refusal_draft_route import route
from ui.i18n import t

_UNSET = "documents.err_line_tax_unset"
_UNSET_RECEIVE = "documents.err_line_tax_unset_receive"
_UNSET_ISSUED = "documents.err_line_tax_unset_issued"


async def _item(client, auth, qty: float) -> tuple[str, str]:
    sku = f"TX-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": "Lot", "quantity": qty, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    return r.json()["id"], sku


async def _draft(client, auth, doc_type: str) -> dict:
    """A draft *doc_type* whose first line is taxed nothing and whose second is "--", each on
    an item of its own (in stock for a sale, a template for goods coming in)."""
    inbound = doc_type in ("purchase_order", "consignment_in", "bill")
    lines, received = [], []
    for i, taxes in enumerate(([], None)):
        item_id, sku = await _item(client, auth, 0 if inbound else 1)
        lines.append({"item_id": item_id, "sku": sku, "name": "Lot", "quantity": 1, "unit_price": 50.0,
                      "taxes": taxes})
        received.append({"item_id": item_id, "sku": sku, "name": "Lot", "quantity_received": 1,
                         "po_line_index": i, "receive_as": "stock"})
    if doc_type == "list":
        r = await client.post("/lists", headers=auth["headers"], json={"list_type": "quotation", "line_items": lines})
    else:
        r = await client.post("/docs", headers=auth["headers"], json={
            "doc_type": doc_type, "contact_id": "supplier:1" if inbound else "customer:1", "line_items": lines})
    assert r.status_code == 200, r.text
    return {"id": r.json()["id"], "doc_type": doc_type, "received": received, "items": [x["item_id"] for x in lines]}


async def _issued_by_an_older_version(client, session, auth, doc_type: str) -> dict:
    """A *doc_type* issued while both lines were taxed, then given "--" on its second line, as a
    record from a version that did not refuse it at Issue holds."""
    doc = await _draft(client, auth, doc_type)
    await _choose_no_tax(client, session, auth, doc)
    r = await client.post(f"/docs/{doc['id']}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc["id"]})
    await session.refresh(row)
    row.state["line_items"][1]["taxes"] = None
    flag_modified(row, "state")
    await session.commit()
    return doc


async def _state(session, auth, entity_id: str) -> dict | None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": entity_id})
    return row.state if row else None


async def _footprint(session, auth) -> tuple:
    """Everything a step that crossed would change: the company's events and every record's
    status, quantity and line taxes."""
    session.expire_all()
    cid = auth["company_id"]
    events = (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid))).scalar_one()
    rows = (await session.execute(select(Projection).where(Projection.company_id == cid))).scalars().all()
    return events, sorted(
        (r.entity_id, r.state.get("status"), r.state.get("quantity"),
         repr([li.get("taxes") for li in r.state.get("line_items") or [] if isinstance(li, dict)]))
        for r in rows)


async def _choose_no_tax(client, session, auth, doc: dict) -> None:
    """The user's fix: the "--" line set to No Tax on the draft."""
    kind = "lists" if doc["doc_type"] == "list" else "docs"
    lines = (await _state(session, auth, doc["id"]))["line_items"]
    lines[1]["taxes"] = []
    if kind == "lists":
        version = (await client.get(f"/lists/{doc['id']}", headers=auth["headers"])).json().get("version")
    else:
        version = await doc_version(client, auth["headers"], doc["id"])
    r = await client.patch(f"/{kind}/{doc['id']}", headers=auth["headers"], json={
        "expected_version": version, "fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text


def _finalize(client, auth, doc, key):
    return client.post(f"/docs/{doc['id']}/finalize", headers=auth["headers"])


def _send(client, auth, doc, key):
    return client.post(f"/docs/{doc['id']}/send", headers=auth["headers"],
                       json={"sent_via": "manual", "idempotency_key": key})


def _receive(client, auth, doc, key):
    return client.post(f"/docs/{doc['id']}/receive", headers=auth["headers"],
                       json={"location_id": "", "received_items": doc["received"], "idempotency_key": key})


def _convert(client, auth, doc, key):
    return client.post(f"/docs/{doc['id']}/convert", headers=auth["headers"], json={"idempotency_key": key})


def _list_finalize(client, auth, doc, key):
    return client.post(f"/lists/{doc['id']}/finalize", headers=auth["headers"])


_ENTRIES = {"finalize": _finalize, "send": _send, "receive": _receive, "convert": _convert,
            "list_finalize": _list_finalize}

# (doc_type, entry point, the message key that refuses it) for a draft.
_DRAFT_REFUSED = [
    ("invoice", "finalize", _UNSET), ("invoice", "send", _UNSET),
    ("bill", "finalize", _UNSET),
    ("purchase_order", "finalize", _UNSET),
    ("purchase_order", "receive", _UNSET_RECEIVE),
    ("consignment_in", "finalize", _UNSET), ("consignment_in", "receive", _UNSET_RECEIVE),
    ("memo", "finalize", _UNSET), ("memo", "send", _UNSET),
    ("quotation", "finalize", _UNSET), ("quotation", "send", _UNSET),
    ("credit_note", "finalize", _UNSET),
    ("list", "list_finalize", _UNSET),
]


@pytest.mark.parametrize(("doc_type", "entry", "key"), _DRAFT_REFUSED, ids=lambda v: v if isinstance(v, str) else "")
async def test_a_draft_step_that_cannot_be_undone_refuses_the_unset_line_and_changes_nothing(
        client, session, doc_type, entry, key):
    auth = await _auth_company(session)
    doc = await _draft(client, auth, doc_type)
    before = await _footprint(session, auth)
    op = uuid.uuid4().hex
    for _attempt in range(2):  # the same request again: refused the same way
        r = await _ENTRIES[entry](client, auth, doc, op)
        assert r.status_code == 422 and r.json()["detail"] == t(key, line=2), r.text
        assert await _footprint(session, auth) == before

    await _choose_no_tax(client, session, auth, doc)
    r = await _ENTRIES[entry](client, auth, doc, op)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc["id"]))["status"] != "draft"


@pytest.mark.parametrize(("doc_type", "entry", "steps"), [
    ("purchase_order", "receive", ("revert",)),
    ("consignment_in", "receive", ("revert",)),
], ids=lambda v: v if isinstance(v, str) else "")
async def test_an_issued_document_holding_an_unset_line_is_refused_with_the_way_back(
        client, session, doc_type, entry, steps):
    auth = await _auth_company(session)
    doc = await _issued_by_an_older_version(client, session, auth, doc_type)
    before = await _footprint(session, auth)
    op = uuid.uuid4().hex
    for _attempt in range(2):
        r = await _ENTRIES[entry](client, auth, doc, op)
        assert r.status_code == 422 and r.json()["detail"] == t(_UNSET_ISSUED, line=2, route=route(*steps)), r.text
        assert await _footprint(session, auth) == before


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
async def test_a_type_that_is_never_sent_says_so_before_any_tax(client, session, doc_type):
    auth = await _auth_company(session)
    doc = await _draft(client, auth, doc_type)
    before = await _footprint(session, auth)
    r = await _send(client, auth, doc, uuid.uuid4().hex)
    assert r.status_code == 409 and r.json()["detail"] == t("documents.err_send_type"), r.text
    assert await _footprint(session, auth) == before


async def test_a_finalized_list_holding_an_unset_line_keeps_its_type_and_names_the_way_back(client, session):
    """A draft write-off is finalized by its Write-off, not by Issue, so a quotation re-typed to one
    and written off holds its "--" line once Undo puts it back to finalized. Re-typing it into a
    quotation again would issue that line, so it is refused with the way back to a draft."""
    auth = await _auth_company(session)
    doc = await _draft(client, auth, "list")
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc["id"]})
    await session.refresh(row)
    row.state.update({"list_type": "writeoff", "status": "finalized"})
    flag_modified(row, "state")
    await session.commit()
    before = await _footprint(session, auth)
    for _attempt in range(2):
        r = await client.post(f"/lists/{doc['id']}/change-type", headers=auth["headers"],
                              json={"list_type": "quotation"})
        assert r.status_code == 422 and r.json()["detail"] == t(_UNSET_ISSUED, line=2, route=route("revert")), r.text
        assert await _footprint(session, auth) == before

    r = await client.post(f"/lists/{doc['id']}/revert-to-draft", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _choose_no_tax(client, session, auth, doc)
    r = await client.post(f"/lists/{doc['id']}/change-type", headers=auth["headers"], json={"list_type": "quotation"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("doc_type", ["quotation"])
async def test_a_conversion_into_a_draft_carries_the_unset_line_to_the_draft_that_refuses_it(
        client, session, doc_type):
    auth = await _auth_company(session)
    doc = await _draft(client, auth, doc_type)
    r = await _convert(client, auth, doc, uuid.uuid4().hex)
    assert r.status_code == 200, r.text
    target = {"id": r.json()["target_doc_id"], "doc_type": "invoice"}
    state = await _state(session, auth, target["id"])
    assert state["status"] == "draft" and state["line_items"][1]["taxes"] is None
    before = await _footprint(session, auth)
    r = await _finalize(client, auth, target, None)
    assert r.status_code == 422 and r.json()["detail"] == t(_UNSET, line=2), r.text
    assert await _footprint(session, auth) == before


@pytest.mark.parametrize(("doc_type", "entry"), [("memo", "convert"), ("purchase_order", "convert"),
                                                 ("consignment_in", "convert")])
async def test_a_conversion_a_draft_cannot_make_changes_nothing(client, session, doc_type, entry):
    """Only an issued memo or consignment converts; a draft is refused before any write."""
    auth = await _auth_company(session)
    doc = await _draft(client, auth, doc_type)
    before = await _footprint(session, auth)
    r = await _ENTRIES[entry](client, auth, doc, uuid.uuid4().hex)
    assert r.status_code in (409, 422), r.text
    assert await _footprint(session, auth) == before


def _import(status: str, doc_type: str) -> dict:
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    data = {"doc_type": doc_type, "status": status, "currency": "USD", "line_items": [
        {"description": "Taxed", "quantity": 1, "unit_price": 100.0, "taxes": []},
        {"description": "Unset", "quantity": 1, "unit_price": 50.0, "taxes": None}]}
    if status != "draft":
        data.update({"doc_number": eid[4:], "subtotal": 150.0, "tax": 0.0, "total": 150.0})
    return {"entity_id": eid, "event_type": "doc.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex, "data": data}


@pytest.mark.parametrize(("doc_type", "status"), [
    ("invoice", "final"), ("invoice", "sent"), ("purchase_order", "sent"),
    ("purchase_order", "received"), ("consignment_in", "final"), ("memo", "sent"),
])
async def test_an_import_of_an_issued_record_refuses_the_unset_line_and_its_retry_too(
        client, session, doc_type, status):
    auth = await _auth_company(session)
    body = _import(status, doc_type)
    before = await _footprint(session, auth)
    for _attempt in range(2):
        r = await client.post("/docs/import", headers=auth["headers"], json=body)
        assert r.status_code == 422 and r.json()["detail"] == t(_UNSET, line=2), r.text
        assert await _footprint(session, auth) == before
    body["data"]["line_items"][1]["taxes"] = []
    r = await client.post("/docs/import", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("doc_type", ["invoice", "purchase_order", "memo"])
async def test_an_import_of_a_draft_keeps_the_unset_line(client, session, doc_type):
    auth = await _auth_company(session)
    body = _import("draft", doc_type)
    r = await client.post("/docs/import", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, body["entity_id"]))["line_items"][1]["taxes"] is None


# Every action route on a document or list: a case above, or why "--" cannot cross it.
_COVERED = {"/docs/{entity_id}/finalize", "/docs/{entity_id}/send", "/docs/{entity_id}/receive",
            "/docs/{entity_id}/convert", "/lists/{entity_id}/finalize", "/lists/{entity_id}/change-type",
            "/docs/import"}
_CANNOT_CROSS = {
    "/docs/{entity_id}": "edits a draft's fields; the line's tax is chosen here",
    "/lists/{entity_id}": "edits a draft list's fields",
    "/lists/{entity_id}/line-page": "edits a draft list's lines",
    "/docs/{entity_id}/reprice": "reprices a draft, which stays a draft",
    "/lists/{entity_id}/reprice": "reprices a draft list, which stays a draft",
    "/docs/{entity_id}/scan": "adds lines to the document; a scanned line is taxed as it is added",
    "/docs/{entity_id}/void": "voids; nothing is charged",
    "/lists/{entity_id}/void": "voids; nothing is charged",
    "/docs/{entity_id}/unvoid": "restores what an older version issued, as it charged before the void",
    "/docs/{entity_id}/revert-to-draft": "returns to the draft where the tax is chosen",
    "/lists/{entity_id}/revert-to-draft": "returns to the draft where the tax is chosen",
    "/docs/{entity_id}/close": "closes an issued memo",
    "/docs/{entity_id}/reopen": "reopens a closed memo",
    "/docs/{entity_id}/renumber": "changes the number only",
    "/docs/{entity_id}/payment": "an issued document only (PAYABLE_STATUSES)",
    "/docs/{entity_id}/refund": "gives back a payment",
    "/docs/{entity_id}/void-payment": "voids a payment",
    "/docs/{entity_id}/apply-to-invoice": "applies an issued credit note",
    "/docs/{entity_id}/apply-to-bill": "applies an issued credit note",
    "/docs/{entity_id}/cn-refund": "refunds an issued credit note",
    "/docs/{entity_id}/receive-return": "an issued credit note only",
    "/docs/{entity_id}/return-items": "sends received goods back; a debit note's lines carry no taxes key",
    "/docs/{entity_id}/fulfill-lines": "ships goods; the document's status and taxes stay as they were",
    "/docs/{entity_id}/revert-lines": "takes shipped goods back",
    "/docs/{entity_id}/invoice-lines": "draws lines of an issued memo, which Issue already refused with a tax unset",
    "/docs/{entity_id}/reserve-lines": "ledger-neutral reservation",
    "/lists/{entity_id}/reserve-lines": "ledger-neutral reservation",
    "/docs/{entity_id}/set-available": "releases a reservation",
    "/lists/{entity_id}/set-available": "releases a reservation",
    "/lists/{entity_id}/convert": "a finalized list only, which Issue already refused",
    "/lists/{entity_id}/duplicate": "makes a new draft list",
    "/lists/{entity_id}/recalculate-total": "re-totals the lines as they stand; each line keeps its taxes",
    "/lists/{entity_id}/restore-total": "gives back the totals and lines held before a recalculation",
    "/lists/{entity_id}/send": "records a finalized list as sent, which Issue or a re-type already refused",
    "/lists/{entity_id}/unmark-sent": "clears the sent mark only",
    "/lists/{entity_id}/scan": "adds lines to a draft list, or checks off an audit's count",
    "/lists/{entity_id}/set-scanned": "marks audit lines as counted",
    "/lists/{entity_id}/line/{item_id}": "sets an audit line's count",
    "/lists/{entity_id}/adjust": "applies an audit's count to stock at cost; nothing is charged",
    "/lists/{entity_id}/undo-adjust": "reverses an audit's stock adjustment",
    "/lists/{entity_id}/writeoff-line": "edits a draft write-off's quantity and account",
    "/lists/{entity_id}/write-off": "writes stock off at cost, nothing is charged; a re-type to a quotation refuses the line",
    "/lists/{entity_id}/undo-write-off": "restores the written-off stock; the list stays a write-off",
    "/lists/{entity_id}/move": "relocates a finalized transfer's items",
    "/lists/{entity_id}/notes": "notes only",
    "/lists/{entity_id}/notes/{note_id}": "notes only",
    "/docs/{entity_id}/notes": "notes only",
    "/docs/{entity_id}/notes/{note_id}": "notes only",
    "/docs/{entity_id}/files": "attachments only",
    "/docs/{entity_id}/files/{file_id}/tag": "attachments only",
    "/docs/{entity_id}/files/{file_id}/description": "attachments only",
    "/docs/{entity_id}/share": "a share link only",
}


def test_every_action_route_on_a_document_or_list_is_a_case_or_named():
    from celerp.main import app

    # The OpenAPI paths, not app.routes: an included router is one opaque entry there.
    routes = {path for path, ops in app.openapi()["paths"].items()
              if ops.keys() & {"post", "put", "patch"}
              and (path.startswith(("/docs/{entity_id}", "/lists/{entity_id}")) or path == "/docs/import")}
    assert routes - _COVERED - _CANNOT_CROSS.keys() == set()
    assert (_COVERED | _CANNOT_CROSS.keys()) - routes == set()
