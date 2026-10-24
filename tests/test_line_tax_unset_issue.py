# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A line whose tax is still for the user to set ("--", line_tax_unset) is never issued.

It charges nothing until the user chooses its tax or No Tax, so every writer that issues a
document or list (finalize, send or mark sent, a purchase order's bill, list finalize, an import
of an issued record, buying a consignment) refuses it, naming the line. A draft keeps it: a
conversion into a draft carries "--" across, and that draft's own Issue refuses it.
"""
from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_consignment_in_sale import _consign
from test_money_stock_and_contact_invariants import _auth_company
from ui.i18n import t

_LINES = [{"description": "Taxed", "quantity": 1, "unit_price": 100.0, "taxes": []},
          {"description": "Unset", "quantity": 1, "unit_price": 50.0, "taxes": None}]


def _refused(r, line: int = 2, key: str = "documents.err_line_tax_unset") -> bool:
    return r.status_code == 422 and r.json().get("detail") == t(key, line=line)


async def _doc(client, auth, doc_type: str = "invoice", lines: list | None = None) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1" if doc_type == "purchase_order" else None,
        "line_items": [dict(line) for line in lines or _LINES]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await _get(client, auth, doc_id))["line_items"][1]["taxes"] is None
    return doc_id


async def _get(client, auth, doc_id: str) -> dict:
    return (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()


async def test_finalize_refuses_a_line_whose_tax_is_unset_until_it_is_chosen(client, session):
    auth = await _auth_company(session)
    doc_id = await _doc(client, auth)
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert _refused(r), r.text
    assert (await _get(client, auth, doc_id))["status"] == "draft"

    lines = (await _get(client, auth, doc_id))["line_items"]
    lines[1]["taxes"] = []
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("payload", [{"sent_via": "manual"}, {"sent_via": "email", "sent_to": "buyer@example.test"}])
async def test_sending_a_draft_refuses_a_line_whose_tax_is_unset(client, session, payload):
    auth = await _auth_company(session)
    doc_id = await _doc(client, auth)
    r = await client.post(f"/docs/{doc_id}/send", headers=auth["headers"], json=payload)
    assert _refused(r), r.text
    assert (await _get(client, auth, doc_id))["status"] == "draft"


async def test_a_purchase_order_with_a_line_whose_tax_is_unset_is_not_issued(client, session):
    auth = await _auth_company(session)
    doc_id = await _doc(client, auth, "purchase_order")
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert _refused(r), r.text
    po = await _get(client, auth, doc_id)
    assert po["status"] == "draft" and not po.get("converted_to")


async def test_the_created_event_keeps_a_line_whose_tax_is_unset_apart_from_no_tax(client, session):
    auth = await _auth_company(session)
    doc_id = await _doc(client, auth)
    entry = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == doc_id, LedgerEntry.event_type == "doc.created"))).scalar_one()
    lines = entry.data["line_items"]
    assert lines[0]["taxes"] == [] and "taxes" in lines[1] and lines[1]["taxes"] is None


async def test_list_finalize_refuses_a_line_whose_tax_is_unset(client, session):
    auth = await _auth_company(session)
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "line_items": [dict(line) for line in _LINES]})
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]
    r = await client.post(f"/lists/{list_id}/finalize", headers=auth["headers"])
    assert _refused(r), r.text
    assert (await client.get(f"/lists/{list_id}", headers=auth["headers"])).json()["status"] == "draft"


async def test_an_import_of_an_issued_document_refuses_a_line_whose_tax_is_unset(client, session):
    auth = await _auth_company(session)
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "final", "doc_number": eid[4:], "currency": "USD",
                 "line_items": [dict(line) for line in _LINES], "subtotal": 150.0, "tax": 0.0, "total": 150.0}})
    assert _refused(r), r.text
    assert (await client.get(f"/docs/{eid}", headers=auth["headers"])).status_code == 404


async def test_an_import_of_a_draft_keeps_a_line_whose_tax_is_unset(client, session):
    auth = await _auth_company(session)
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "draft", "line_items": [dict(line) for line in _LINES]}})
    assert r.status_code == 200, r.text
    assert (await _get(client, auth, eid))["line_items"][1]["taxes"] is None


async def test_buying_a_consignment_refuses_a_line_whose_tax_is_unset(client, session):
    auth = await _auth_company(session)
    consignment, _lot = await _consign(client, session, auth)
    # A consignment issued by an older version, before its line's "--" was refused at Issue.
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == consignment))).scalar_one()
    row.state["line_items"][0]["taxes"] = None
    flag_modified(row, "state")
    await session.commit()
    r = await client.post(f"/docs/{consignment}/convert", headers=auth["headers"])
    assert _refused(r, line=1, key="documents.err_line_tax_unset_issued"), r.text
    assert not (await _get(client, auth, consignment)).get("converted_to")


async def test_a_quotation_converts_with_its_unset_line_and_the_invoice_is_refused_at_issue(client, session):
    auth = await _auth_company(session)
    quotation = await _doc(client, auth, "quotation")
    r = await client.post(f"/docs/{quotation}/convert", headers=auth["headers"])
    assert r.status_code == 200, r.text
    invoice = r.json()["target_doc_id"]
    assert (await _get(client, auth, invoice))["line_items"][1]["taxes"] is None
    r = await client.post(f"/docs/{invoice}/finalize", headers=auth["headers"])
    assert _refused(r), r.text


# Writers that issue lines no user picks a tax for, each with why "--" cannot reach it.
_ISSUES_WITHOUT_LINE_TAX = {
    "_supplier_credit_for_return": "its one line, for the goods returned, is built with no taxes",
    "imported_opening_snapshot": "create_imported_doc refuses an issued record's unset line before it",
    "write_off_stock": "a write-off's lines are stock taken off, with no tax",
}
_ISSUING = re.compile(r'event_type\s*=\s*"doc\.(sent|finalized)"|"list\.finalized"|"finalized": True')


def test_every_writer_that_issues_a_document_refuses_a_line_whose_tax_is_unset():
    """A writer added later that sends, finalizes or creates an issued document or list either
    calls refuse_unset_line_tax or is named above with its reason."""
    root = Path(__file__).resolve().parent.parent
    issuing = {}
    for top in ("celerp", "default_modules"):
        for path in (root / top).rglob("*.py"):
            if "/tests/" in str(path) or path.name in ("doc_projections.py", "schemas.py"):
                continue
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.col_offset == 0:
                    body = ast.get_source_segment(source, node)
                    if _ISSUING.search(body):
                        issuing[node.name] = "refuse_unset_line_tax(" in body
    unguarded = sorted(name for name, refuses in issuing.items() if not refuses)
    assert unguarded == sorted(_ISSUES_WITHOUT_LINE_TAX), unguarded
    assert {"finalize_document", "mark_doc_sent", "finalize_list_record", "buy_consignment"} <= issuing.keys()
    import_service = (root / "default_modules/celerp-docs/celerp_docs/import_service.py").read_text()
    assert import_service.index("refuse_unset_line_tax(") < import_service.index("imported_opening_snapshot(")
