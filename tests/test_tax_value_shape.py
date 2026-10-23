# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document's taxes (``doc_taxes``) and a line's taxes (``taxes``) are a list of tax
objects or absent. Every write refuses any other shape with an error naming the entry it cannot
read, and stores nothing: the document edit, the list create, edit and its line pages, and every
import. A stored value of another shape, from an older version, is never dropped by a path that
recalculates or rewrites money (an edit, a conversion, a share import, the editor's save): it is
refused, naming the entry, until the user removes it or picks the tax again. Every page and
printout still shows such a document."""
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

# Each value of another shape and the entry its refusal names.
_BAD = [("x", "x"), ({"code": "VAT", "rate": 7}, "VAT"), (5, "5"), (["VAT"], "VAT")]
_TAX = [{"code": "VAT", "rate": 7.0, "amount": 0, "order": 0, "is_compound": False, "label": ""}]


def _refused(r, named: str) -> bool:
    return r.status_code == 422 and r.json().get("detail") == t("documents.err_tax_unreadable", tax=named)


async def _draft(client, auth) -> tuple[str, dict]:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    return doc_id, (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()


def _money(doc: dict) -> tuple:
    return doc.get("doc_taxes"), [li.get("taxes") for li in doc.get("line_items") or []], doc.get("tax"), doc.get("total")


@pytest.mark.parametrize(("bad", "named"), _BAD)
async def test_a_document_edit_refuses_document_taxes_of_another_shape(client, session, bad, named):
    auth = await _auth_company(session)
    doc_id, before = await _draft(client, auth)
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"doc_taxes": {"new": bad}}})
    assert _refused(r, named), r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert _money(after) == _money(before)


@pytest.mark.parametrize(("bad", "named"), _BAD)
async def test_a_document_edit_refuses_line_taxes_of_another_shape(client, session, bad, named):
    auth = await _auth_company(session)
    doc_id, before = await _draft(client, auth)
    lines = [{**before["line_items"][0], "taxes": bad}]
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert _refused(r, named), r.text
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


@pytest.mark.parametrize(("bad", "named"), _BAD)
async def test_a_list_edit_and_its_line_page_refuse_line_taxes_of_another_shape(client, session, bad, named):
    auth = await _auth_company(session)
    before = await _quotation(client, auth)
    list_id, lines = before["id"], [{**before["line_items"][0], "taxes": bad}]
    r = await client.patch(f"/lists/{list_id}", headers=auth["headers"],
                           json={"fields_changed": {"line_items": {"new": lines}}})
    assert _refused(r, named), r.text
    r = await client.patch(f"/lists/{list_id}/line-page", headers=auth["headers"], json={
        "line_items": lines, "offset": 0, "original_count": 1, "expected_version": before.get("version")})
    assert _refused(r, named), r.text
    after = (await client.get(f"/lists/{list_id}", headers=auth["headers"])).json()
    assert [li.get("taxes") for li in after["line_items"]] == [li.get("taxes") for li in before["line_items"]]


@pytest.mark.parametrize("field", ["doc_taxes", "taxes"])
async def test_an_import_refuses_taxes_of_another_shape(client, session, field):
    named = "x"
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
    assert _refused(r, named), r.text
    assert (await client.get(f"/docs/{eid}", headers=auth["headers"])).status_code == 404


def _list_record(line: dict) -> dict:
    return {"entity_id": f"list:IMP-{uuid.uuid4().hex[:8]}", "event_type": "list.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex,
            "data": {"list_type": "quotation", "status": "draft", "line_items": [line]}}


@pytest.mark.parametrize("writer", ["create", "import", "import_batch"])
async def test_every_list_write_refuses_line_taxes_of_another_shape(client, session, writer):
    named = "x"
    auth = await _auth_company(session)
    line = {"description": "S", "quantity": 1, "unit_price": 100.0, "taxes": "x"}
    if writer == "create":
        r = await client.post("/lists", headers=auth["headers"], json={"list_type": "quotation", "line_items": [line]})
        assert _refused(r, named), r.text
        return
    rec = _list_record(line)
    if writer == "import":
        r = await client.post("/lists/import", headers=auth["headers"], json=rec)
        assert _refused(r, named), r.text
    else:
        r = await client.post("/lists/import/batch", headers=auth["headers"], json={"records": [rec]})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 0
        assert any(t("documents.err_tax_unreadable", tax=named) in e for e in r.json()["errors"]), r.json()
    assert (await client.get(f"/lists/{rec['entity_id']}", headers=auth["headers"])).status_code == 404


_VAT_7 = {**_TAX[0], "amount": 7.0}
_LEVY = "Levy 5%"


async def _stored_with_a_levy_it_cannot_read(client, session, auth, where: str) -> tuple[str, dict]:
    """A $100 draft charging VAT 7% and a Levy 5% an older version stored as plain text (tax 12,
    total 112): no write takes such a value any more, so it is put straight into the projection."""
    doc_id, _ = await _draft(client, auth)
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == doc_id))).scalar_one()
    state = {**row.state, "tax": 12.0, "total": 112.0}
    if where == "doc_taxes":
        state["doc_taxes"] = [_VAT_7, _LEVY]
    else:
        state["line_items"] = [{**state["line_items"][0], "taxes": [_VAT_7, _LEVY]}]
    row.state = state
    flag_modified(row, "state")
    await session.commit()
    return doc_id, (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()


@pytest.mark.parametrize("where", ["doc_taxes", "taxes"])
@pytest.mark.parametrize("change", [{"discount": {"new": 5}}, {"shipping": {"new": 10}}])
async def test_an_edit_to_a_document_holding_a_tax_it_cannot_read_is_refused_naming_it(client, session, where,
                                                                                         change):
    auth = await _auth_company(session)
    doc_id, before = await _stored_with_a_levy_it_cannot_read(client, session, auth, where)
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={"fields_changed": change})
    assert _refused(r, _LEVY), r.text
    after = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    assert after.status_code == 200
    assert _money(after.json()) == _money(before) and (after.json()["tax"], after.json()["total"]) == (12.0, 112.0)


@pytest.mark.parametrize("where", ["doc_taxes", "taxes"])
async def test_removing_the_tax_it_cannot_read_lets_the_document_take_the_edit(client, session, where):
    auth = await _auth_company(session)
    doc_id, before = await _stored_with_a_levy_it_cannot_read(client, session, auth, where)
    if where == "doc_taxes":
        change = {"doc_taxes": {"new": [_VAT_7]}}
    else:
        change = {"line_items": {"new": [{**before["line_items"][0], "taxes": [_VAT_7]}]}}
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={"fields_changed": change})
    assert r.status_code == 200, r.text
    after = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert (after["tax"], after["total"]) == (7.0, 107.0)


async def test_a_memo_holding_a_tax_it_cannot_read_is_not_converted(client, session):
    """Billing part of a memo line scales its taxes; the Levy it cannot read is not dropped on
    the way, so the invoice is refused rather than made without it."""
    auth = await _auth_company(session)
    sku = f"TX-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku, "quantity": 1, "sell_by": "piece", "status": "available"})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    r = await client.post("/docs", headers=auth["headers"], json={"doc_type": "memo", "line_items": [
        {"entity_id": item, "sku": sku, "name": sku, "quantity": 1, "unit_price": 100.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    memo = r.json()["id"]
    for path, body in ((f"/docs/{memo}/finalize", {}), (f"/docs/{memo}/fulfill-lines", {"line_entity_ids": [item]})):
        r = await client.post(path, headers=auth["headers"], json=body)
        assert r.status_code == 200, r.text
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == memo))).scalar_one()
    # Two on the line, one still out: the invoice bills half the line.
    row.state = {**row.state, "line_items": [
        {**row.state["line_items"][0], "quantity": 2, "line_total": 200.0, "taxes": [{**_VAT_7, "amount": 14.0}, _LEVY]}]}
    flag_modified(row, "state")
    await session.commit()
    r = await client.post(f"/docs/{memo}/convert", headers=auth["headers"])
    assert _refused(r, _LEVY), r.text
    assert (await client.get(f"/docs/{memo}", headers=auth["headers"])).json()["status"] != "converted"


async def test_a_received_document_takes_only_its_tax_objects_as_a_list_rate():
    from celerp_docs.received import managed_fields

    doc = {"doc_taxes": ["VAT", *_TAX], "line_items": [{"description": "S", "quantity": 1, "taxes": ["VAT", *_TAX]}]}
    fields = managed_fields(doc, type("Target", (), {"kind": "list", "type": "quotation"})())
    assert fields["tax"] == 7.0 and fields["line_items"][0]["tax_rate"] == 7.0


@pytest.mark.parametrize("value, named", [
    (None, None), ([], None), (_TAX, None), (*_BAD[0],), (*_BAD[1],), (*_BAD[2],), (*_BAD[3],),
    ([*_TAX, _LEVY], _LEVY), ([*_TAX, ""], ""), ([*_TAX, 0], "0"), ([{"label": "Levy"}, "x" * 80], "x" * 60)])
async def test_unreadable_tax_names_the_first_entry_that_is_not_a_tax(value, named):
    from celerp.tax_regimes import unreadable_tax

    assert unreadable_tax(value) == named


@pytest.mark.parametrize("value, entries", [
    (None, []), ("x", []), (5, []), ({"code": "VAT"}, []), (["VAT", *_TAX], _TAX), (_TAX, _TAX)])
async def test_tax_entries_reads_only_tax_objects(value, entries):
    from celerp.tax_regimes import tax_entries

    assert tax_entries(value) == entries


_STORED = {"entity_id": "doc:INV-1", "id": "doc:INV-1", "doc_type": "invoice", "status": "draft",
           "ref_id": "INV-1", "currency": "USD", "subtotal": 100.0, "tax": 0.0, "total": 100.0}


async def test_the_editor_shows_a_tax_it_cannot_read_and_sends_it_back_as_it_is(ui_client):
    """The draft's line and document taxes ride along unchanged, the entry it cannot read
    included, so a save the user did not change the taxes in never drops it."""
    import json

    from bs4 import BeautifulSoup

    doc = {**_STORED, "tax": 12.0, "total": 112.0, "doc_taxes": [_VAT_7, _LEVY],
           "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
                           "taxes": [_VAT_7, _LEVY]}]}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        page = await ui_client.get("/docs/doc:INV-1")
    assert page.status_code == 200, page.text[-500:]
    soup = BeautifulSoup(page.text, "html.parser")
    assert json.loads(soup.select_one("#doc-taxes-state")["value"]) == [_VAT_7, _LEVY]
    assert [json.loads(x["value"]) for x in soup.select('[data-name="taxes_json"]')] == [[_VAT_7, _LEVY]]
    chosen = [o for o in soup.select('[data-name="tax_select"] option[selected]') if o["value"] != "|0"]
    assert [(o["value"], o.text) for o in chosen] == [("|stored", "VAT (7.0%) + Levy 5%")]
    assert [row.select_one(".total-label").text for row in soup.select("#doc-stored-tax-rows [data-doc-tax-index]")] \
        == ["VAT:", "Levy 5%:"]


@pytest.mark.parametrize(("bad", "named"), _BAD)
async def test_a_stored_tax_value_of_another_shape_still_shows_the_document(ui_client, bad, named):
    doc = {**_STORED, "doc_taxes": bad,
           "line_items": [{"description": "S", "quantity": 1, "unit_price": 100.0, "line_total": 100.0,
                           "taxes": bad}]}
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)):
        page = await ui_client.get("/docs/doc:INV-1")
        printed = await ui_client.get("/docs/doc:INV-1/print")
    assert page.status_code == 200, page.text[-500:]
    assert printed.status_code == 200, printed.text[-500:]


@pytest.mark.parametrize(("bad", "named"), _BAD)
async def test_a_stored_tax_value_of_another_shape_still_prints_and_checks(bad, named):
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
