# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's taxes (``doc_taxes``) and a line's taxes (``taxes``) are a list of tax
objects or absent. Every write refuses any other shape with the tax error and stores
nothing: the document edit, the list create, edit and its line pages, and every import. Every
page, printout and money recompute that reads taxes reads a stored value of another shape as
no taxes rather than failing."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from celerp.models.projections import Projection
from test_money_stock_and_contact_invariants import _auth_company
from test_ui import _authed, _mock_get_company, _mock_get_modules, ui_client  # noqa: F401  (fixtures)
from ui.i18n import t

pytestmark = pytest.mark.asyncio

_BAD = ["x", {"code": "VAT", "rate": 7}, 5, ["VAT"]]
_TAX = [{"code": "VAT", "rate": 7.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]


def _refused(r) -> bool:
    return r.status_code == 422 and r.json().get("detail") == t("documents.err_tax_invalid")


async def _draft(client, auth) -> tuple[str, dict]:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    return doc_id, (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()


def _money(doc: dict) -> tuple:
    return doc.get("doc_taxes"), [li.get("taxes") for li in doc.get("line_items") or []], doc.get("tax"), doc.get("total")


@pytest.mark.parametrize("bad", _BAD)
async def test_a_document_edit_refuses_document_taxes_of_another_shape(client, session, bad):
    auth = await _auth_company(session)
    doc_id, before = await _draft(client, auth)
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"doc_taxes": {"new": bad}}})
    assert _refused(r), r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert _money(after) == _money(before)


@pytest.mark.parametrize("bad", _BAD)
async def test_a_document_edit_refuses_line_taxes_of_another_shape(client, session, bad):
    auth = await _auth_company(session)
    doc_id, before = await _draft(client, auth)
    lines = [{**before["line_items"][0], "taxes": bad}]
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert _refused(r), r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert _money(after) == _money(before)


async def test_a_document_edit_with_taxes_as_a_list_of_objects_is_saved(client, session):
    auth = await _auth_company(session)
    doc_id, before = await _draft(client, auth)
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"doc_taxes": {"new": _TAX}}})
    assert r.status_code == 200, r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert after["doc_taxes"][0]["amount"] == 7.0 and after["total"] == 107.0


async def _quotation(client, auth) -> dict:
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    return (await client.get(f"/lists/{r.json()['id']}", headers=auth["headers"])).json()


@pytest.mark.parametrize("bad", _BAD)
async def test_a_list_edit_and_its_line_page_refuse_line_taxes_of_another_shape(client, session, bad):
    auth = await _auth_company(session)
    before = await _quotation(client, auth)
    list_id, lines = before["id"], [{**before["line_items"][0], "taxes": bad}]
    r = await client.patch(f"/lists/{list_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert _refused(r), r.text
    r = await client.patch(f"/lists/{list_id}/line-page", headers=auth["headers"], json={
        "line_items": lines, "offset": 0, "original_count": 1, "expected_version": before.get("version")})
    assert _refused(r), r.text
    after = (await client.get(f"/lists/{list_id}", headers=auth["headers"])).json()
    assert [li.get("taxes") for li in after["line_items"]] == [li.get("taxes") for li in before["line_items"]]


@pytest.mark.parametrize("field", ["doc_taxes", "taxes"])
async def test_an_import_refuses_taxes_of_another_shape(client, session, field):
    auth = await _auth_company(session)
    line = {"description": "S", "quantity": 1, "unit_price": 100.0}
    data = {"doc_type": "invoice", "status": "draft", "line_items": [line]}
    if field == "doc_taxes":
        data["doc_taxes"] = "x"
    else:
        line["taxes"] = "x"
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": data})
    assert _refused(r), r.text
    assert (await client.get(f"/docs/{eid}", headers=auth["headers"])).status_code == 404


def _list_record(line: dict) -> dict:
    return {"entity_id": f"list:IMP-{uuid.uuid4().hex[:8]}", "event_type": "list.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex,
            "data": {"list_type": "quotation", "status": "draft", "line_items": [line]}}


@pytest.mark.parametrize("writer", ["create", "import", "import_batch"])
async def test_every_list_write_refuses_line_taxes_of_another_shape(client, session, writer):
    auth = await _auth_company(session)
    line = {"description": "S", "quantity": 1, "unit_price": 100.0, "taxes": "x"}
    if writer == "create":
        r = await client.post("/lists", headers=auth["headers"], json={"list_type": "quotation", "line_items": [line]})
        assert _refused(r), r.text
        return
    rec = _list_record(line)
    if writer == "import":
        r = await client.post("/lists/import", headers=auth["headers"], json=rec)
        assert _refused(r), r.text
    else:
        r = await client.post("/lists/import/batch", headers=auth["headers"], json={"records": [rec]})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 0
        assert any(t("documents.err_tax_invalid") in e for e in r.json()["errors"]), r.json()
    assert (await client.get(f"/lists/{rec['entity_id']}", headers=auth["headers"])).status_code == 404


@pytest.mark.parametrize("where", ["doc_taxes", "taxes"])
async def test_a_document_holding_a_stored_tax_value_of_another_shape_still_takes_an_edit(client, session, where):
    auth = await _auth_company(session)
    doc_id, _ = await _draft(client, auth)
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == doc_id))).scalar_one()
    state = dict(row.state)
    if where == "doc_taxes":
        state["doc_taxes"] = ["x"]
    else:
        state["line_items"] = [{**state["line_items"][0], "taxes": ["x"]}]
    row.state = state
    flag_modified(row, "state")
    await session.commit()
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"discount": {"new": 5}}})
    assert r.status_code == 200, r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert (after["tax"], after["total"]) == (0.0, 95.0)


async def test_a_received_document_takes_only_its_tax_objects_as_a_list_rate():
    from celerp_docs.received import managed_fields

    doc = {"doc_taxes": ["VAT", *_TAX], "line_items": [{"description": "S", "quantity": 1, "taxes": ["VAT", *_TAX]}]}
    fields = managed_fields(doc, type("Target", (), {"kind": "list", "type": "quotation"})())
    assert fields["tax"] == 7.0 and fields["line_items"][0]["tax_rate"] == 7.0


@pytest.mark.parametrize("value, entries", [
    (None, []), ("x", []), (5, []), ({"code": "VAT"}, []), (["VAT", *_TAX], _TAX), (_TAX, _TAX)])
async def test_tax_entries_reads_only_tax_objects(value, entries):
    from celerp.tax_regimes import tax_entries

    assert tax_entries(value) == entries


_STORED = {"entity_id": "doc:INV-1", "id": "doc:INV-1", "doc_type": "invoice", "status": "draft",
           "ref_id": "INV-1", "currency": "USD", "subtotal": 100.0, "tax": 0.0, "total": 100.0}


@pytest.mark.parametrize("bad", _BAD)
async def test_a_stored_tax_value_of_another_shape_still_shows_the_document(ui_client, bad):
    doc = {**_STORED, "doc_taxes": bad,
           "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
                           "taxes": bad}]}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        page = await ui_client.get("/docs/doc:INV-1")
        printed = await ui_client.get("/docs/doc:INV-1/print")
    assert page.status_code == 200, page.text[-500:]
    assert printed.status_code == 200, printed.text[-500:]


@pytest.mark.parametrize("bad", _BAD)
async def test_a_stored_tax_value_of_another_shape_still_prints_and_checks(bad):
    from celerp.output.pdf import generate_document_pdf
    from celerp.services import auto_je
    from celerp_docs.received import unrepresentable

    doc = {**_STORED, "doc_taxes": bad,
           "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
                           "taxes": bad}]}
    assert generate_document_pdf(doc)[:4] == b"%PDF"
    auto_je.refuse_misplaced_tax({}, "invoice", doc, doc["line_items"])
    auto_je.bill_line_charges({**doc, "doc_type": "bill"})
    unrepresentable(doc, type("Target", (), {"kind": "list"})())
