# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Scanning adds to a document through the same batched primitive as a list: the scan bar
submits a comma-separated run under one run key to POST /docs/{id}/scan, the server
resolves every code against one inventory load, places each on its own line through the
document writer's own rules, and reports the codes it could not add. A retry of the same
run replays its recorded outcome."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from celerp.services.list_behavior import LIST_TYPES
from celerp_docs.sequences import _PREFIX_BY_DOC_TYPE
from test_helpers import reserve_item, sell_item

# Every numbered document type except the list types, which scan through /lists.
DOC_TYPES = sorted(set(_PREFIX_BY_DOC_TYPE) - {"list", *LIST_TYPES})


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Scan Co",
        "email": f"scan-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@scan.test"
    r = await client.post("/companies/me/users", headers=_h(admin_token),
                          json={"name": role.title(), "email": addr, "password": "testpass123", "role": role})
    assert r.status_code == 200, r.text
    from test_helpers import clear_sessions as _clear_tracker
    await _clear_tracker(session)
    r = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _item(client, t, sku, *, qty=5, **fields) -> str:
    body = {"status": "available", "sku": sku, "name": f"Name {sku}", "quantity": qty, "sell_by": "piece", **fields}
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _doc(client, t, doc_type="invoice", lines=None) -> str:
    r = await client.post("/docs", headers=_h(t), json={"doc_type": doc_type, "line_items": lines or []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get(client, t, doc_id) -> dict:
    r = await client.get(f"/docs/{doc_id}", headers=_h(t))
    assert r.status_code == 200, r.text
    return r.json()


async def _lines(client, t, doc_id) -> list[dict]:
    return (await _get(client, t, doc_id)).get("line_items") or []


def _ids(lines) -> list[str]:
    return [line.get("entity_id") or line.get("item_id") for line in lines]


async def _scan(client, t, doc_id, barcode, **extra):
    return await client.post(f"/docs/{doc_id}/scan", headers=_h(t), json={"barcode": barcode, **extra})


def _reasons(body) -> dict:
    return {f["code"]: f["reason"] for f in body["failed"]}


def _codes() -> tuple[str, ...]:
    n = str(uuid.uuid4().int)
    return n[:12], n[12:24]


async def _price_lists(client, t, *names):
    r = await client.patch("/companies/me/price-lists", headers=_h(t), json={
        "price_lists": [{"name": n} for n in names], "base_price_list": names[0]})
    assert r.status_code == 200, r.text


# ── Placement: every code type, every doc type ─────────────────────────────────


async def test_doc_scan_adds_by_every_code_type(client):
    t = await _register(client)
    bc, _ = _codes()
    by_barcode = await _item(client, t, "SC-BC", barcode=bc)
    by_rfid = await _item(client, t, "SC-RF", rfid_epc="E200AA01")
    by_gtin = await _item(client, t, "SC-GT", gtin="12345670")
    by_sku = await _item(client, t, "SC-SKU")
    doc = await _doc(client, t)

    r = await _scan(client, t, doc, f"{bc},e200aa01,12345670,SC-SKU")
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 4 and r.json()["failed"] == []
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [by_barcode, by_rfid, by_gtin, by_sku]
    assert [line["sku"] for line in lines] == ["SC-BC", "SC-RF", "SC-GT", "SC-SKU"]
    assert lines[0]["description"] == "Name SC-BC"


@pytest.mark.parametrize("doc_type", DOC_TYPES)
async def test_each_doc_type_scans_through_one_primitive(client, doc_type):
    t = await _register(client)
    bc, _ = _codes()
    item = await _item(client, t, f"T-{doc_type}", barcode=bc)
    doc = await _doc(client, t, doc_type)

    r = await _scan(client, t, doc, bc)
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == 1, r.json()
    assert _ids(await _lines(client, t, doc)) == [item]


async def test_doc_scan_quantity_and_batch_accumulation(client):
    """A lot that may not be split takes all it has on hand, and each batch appends in scan order."""
    t = await _register(client)
    a = await _item(client, t, "QA", qty=3, allow_splitting=False)
    b = await _item(client, t, "QB", qty=2, allow_splitting=False)
    c = await _item(client, t, "QC", qty=4, allow_splitting=False)
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "QA,QB")).json()["scanned"] == 2
    assert (await _scan(client, t, doc, "QC")).json()["scanned"] == 1
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [a, b, c]  # each batch appends in scan order
    assert [float(line["quantity"]) for line in lines] == [3.0, 2.0, 4.0]


async def test_doc_scan_prices_from_price_list(client):
    t = await _register(client)
    await _price_lists(client, t, "Retail", "Wholesale")
    await _item(client, t, "PR", retail_price=10.1234567891, wholesale_price=7.5)
    retail_doc = await _doc(client, t)
    wholesale_doc = await _doc(client, t)

    assert (await _scan(client, t, retail_doc, "PR")).json()["scanned"] == 1
    assert (await _scan(client, t, wholesale_doc, "PR", price_list="Wholesale")).json()["scanned"] == 1
    # No list given prices from the default list, rounded to the currency's rate places.
    from celerp.services.money import round_rate
    rounded = float(round_rate(10.1234567891, "USD"))
    assert rounded != 10.1234567891
    assert float((await _lines(client, t, retail_doc))[0]["unit_price"]) == rounded
    assert float((await _lines(client, t, wholesale_doc))[0]["unit_price"]) == 7.5


async def test_doc_scan_applies_default_sales_tax(client):
    t = await _register(client)
    r = await client.patch("/companies/me/taxes", headers=_h(t), json={"taxes": [
        {"name": "VAT", "rate": 7, "tax_type": "both", "is_default": True},
        {"name": "Zero", "rate": 0, "tax_type": "both", "is_default": False},
    ]})
    assert r.status_code == 200, r.text
    await _item(client, t, "TX", retail_price=100)
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "TX")).json()["scanned"] == 1
    line = (await _lines(client, t, doc))[0]
    assert [(tax["code"], float(tax["rate"])) for tax in line["taxes"]] == [("VAT", 7.0)]
    assert float((await _get(client, t, doc))["tax"]) > 0


async def _taxes(client, t, *taxes):
    r = await client.patch("/companies/me/taxes", headers=_h(t), json={"taxes": list(taxes)})
    assert r.status_code == 200, r.text


_SALES_ONLY = {"name": "Sales tax", "rate": 8, "tax_type": "sales", "is_default": True}
_BOTH = {"name": "VAT", "rate": 7, "tax_type": "both", "is_default": True}


def _tax_codes(line) -> list[tuple[str, float]]:
    return [(tax["code"], float(tax["rate"])) for tax in line.get("taxes") or []]


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order", "consignment_in"])
async def test_buying_scan_prices_at_cost_and_skips_a_sales_only_default_tax(client, doc_type):
    t = await _register(client)
    await _taxes(client, t, _SALES_ONLY)
    await _item(client, t, "BC", retail_price=100, cost_price=40)
    doc = await _doc(client, t, doc_type)

    assert (await _scan(client, t, doc, "BC")).json()["scanned"] == 1
    [line] = await _lines(client, t, doc)
    assert float(line["unit_price"]) == 40.0
    assert _tax_codes(line) == []


@pytest.mark.parametrize("doc_type", ["bill", "purchase_order"])
async def test_buying_scan_takes_a_default_tax_that_applies_to_purchases(client, doc_type):
    t = await _register(client)
    await _taxes(client, t, _BOTH)
    await _item(client, t, "BV", retail_price=100, cost_price=40)
    doc = await _doc(client, t, doc_type)

    assert (await _scan(client, t, doc, "BV")).json()["scanned"] == 1
    [line] = await _lines(client, t, doc)
    assert (float(line["unit_price"]), _tax_codes(line)) == (40.0, [("VAT", 7.0)])


async def test_buying_scan_by_someone_who_cannot_see_costs_adds_the_line_unpriced(client, session):
    t = await _register(client)
    await _item(client, t, "BH", retail_price=100, cost_price=40)
    doc = await _doc(client, t, "bill")
    operator = await _user_with_role(client, session, t, "operator")

    r = await _scan(client, operator, doc, "BH")
    assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
    [line] = await _lines(client, t, doc)
    assert float(line.get("unit_price") or 0) == 0.0


async def test_selling_scan_keeps_the_retail_price_and_the_default_tax(client):
    t = await _register(client)
    await _taxes(client, t, _SALES_ONLY)
    await _item(client, t, "SC", retail_price=100, cost_price=40)
    doc = await _doc(client, t, "invoice")

    assert (await _scan(client, t, doc, "SC")).json()["scanned"] == 1
    [line] = await _lines(client, t, doc)
    assert (float(line["unit_price"]), _tax_codes(line)) == (100.0, [("Sales tax", 8.0)])


async def test_doc_scan_returns_version(client):
    t = await _register(client)
    await _item(client, t, "VR")
    doc = await _doc(client, t)

    r = await _scan(client, t, doc, "VR")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == (await _get(client, t, doc))["version"]


# ── Per-code reporting ──────────────────────────────────────────────────────────


async def test_doc_scan_reports_unknown_code(client):
    t = await _register(client)
    good = await _item(client, t, "UK-GOOD")
    doc = await _doc(client, t)

    r = await _scan(client, t, doc, "UK-GOOD,NO-SUCH-CODE")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 1
    assert _reasons(body) == {"NO-SUCH-CODE": "unknown_code"}
    assert "NO-SUCH-CODE" in body["failed"][0]["label"]
    assert _ids(await _lines(client, t, doc)) == [good]


async def test_doc_scan_reports_ambiguous_and_collision(client, monkeypatch):
    import celerp_inventory.services as inv
    from celerp_inventory.services import ResolveResult
    real = inv.ItemIndex.resolve

    t = await _register(client)
    bc, _ = _codes()
    await _item(client, t, "AMB", allow_splitting=False)
    await _item(client, t, "AMB", allow_splitting=False)
    await _item(client, t, "COL", barcode=bc)

    def _collide(index, codes):
        base = real(index, codes)
        got = base.get(bc)
        if got is not None and got.kind == "barcode":
            base[bc] = ResolveResult("barcode", list(got.matches) * 2)
        return base

    monkeypatch.setattr(inv.ItemIndex, "resolve", _collide)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, f"AMB,{bc}")).json()
    assert body["scanned"] == 0
    assert _reasons(body) == {"AMB": "ambiguous_sku", bc: "duplicate_barcode"}
    assert await _lines(client, t, doc) == []


async def test_product_code_of_several_whole_pieces_asks_for_the_piece(client):
    """A SKU naming several pieces that may not be split never picks one for the operator: the
    piece handed over must be the piece on the document, so the report says to scan its own code."""
    t = await _register(client)
    for _ in range(3):
        await _item(client, t, "PCS", qty=1, allow_splitting=False)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, "PCS")).json()
    assert body["scanned"] == 0 and _reasons(body) == {"PCS": "ambiguous_sku"}, body
    assert body["failed"][0]["label"] == "PCS: 3 pieces share this code - scan the piece's own barcode or tag"
    assert await _lines(client, t, doc) == []


@pytest.mark.parametrize(("method", "first"), [("fifo", True), ("lifo", False)])
async def test_splittable_product_code_binds_the_first_lot_in_pick_order(client, method, first):
    t = await _register(client)
    older = await _item(client, t, "ORD", qty=2, allow_splitting=True, pick_method=method)
    newer = await _item(client, t, "ORD", qty=3, allow_splitting=True, pick_method=method)
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "ORD")).json()["scanned"] == 1
    assert _ids(await _lines(client, t, doc)) == [older if first else newer]


async def test_doc_scan_refuses_held_lot(client):
    t = await _register(client)
    held_bc, out_bc = _codes()
    reserved = await _item(client, t, "HELD", qty=1, barcode=held_bc)
    holder = await reserve_item(client, _h(t), reserved)
    holder_number = (await _get(client, t, holder))["ref_id"]

    out = await _item(client, t, "OUT", qty=1, barcode=out_bc, allow_splitting=False)
    memo = await _doc(client, t, "memo", [{"entity_id": out, "sku": "OUT", "name": "OUT", "quantity": 1,
                                           "unit_price": 10, "sell_by": "piece"}])
    assert (await client.post(f"/docs/{memo}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [out]})
    assert r.status_code == 200, r.text
    memo_number = (await _get(client, t, memo))["ref_id"]

    doc = await _doc(client, t)
    body = (await _scan(client, t, doc, f"{held_bc},{out_bc},HELD")).json()
    assert body["scanned"] == 0, body
    labels = {f["code"]: f["label"] for f in body["failed"]}
    assert holder_number in labels[held_bc]
    assert memo_number in labels[out_bc]
    assert "HELD" in labels  # a SKU whose only lot is held elsewhere is refused, not substituted
    assert await _lines(client, t, doc) == []

    # The invoice made from the memo carries the memo's own lot already: a scan of it is reported
    # as already on the invoice, not refused as held by the memo and not added twice.
    conv = await client.post(f"/docs/{memo}/convert", headers=_h(t), json={})
    assert conv.status_code == 200, conv.text
    invoice = conv.json()["target_doc_id"]
    assert _ids(await _lines(client, t, invoice)) == [out]
    body = (await _scan(client, t, invoice, out_bc)).json()
    assert _reasons(body) == {out_bc: "duplicate_scan"}, body
    assert "already on this document" in body["failed"][0]["label"]
    assert _ids(await _lines(client, t, invoice)) == [out]


@pytest.mark.parametrize("doc_type", ["invoice", "memo"])
async def test_doc_scan_refuses_duplicate_unique_item(client, doc_type):
    t = await _register(client)
    bc, _ = _codes()
    item = await _item(client, t, "UNIQ", qty=1, barcode=bc, allow_splitting=False)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, f"{bc},{bc}")).json()
    assert body["scanned"] == 1
    assert _reasons(body) == {bc: "duplicate_scan"}
    body = (await _scan(client, t, doc, bc)).json()
    assert body["scanned"] == 0 and _reasons(body) == {bc: "duplicate_scan"}
    assert _ids(await _lines(client, t, doc)) == [item]


async def test_doc_scan_reports_draft_and_zero_qty(client):
    t = await _register(client)
    draft_bc, zero_bc = _codes()
    await _item(client, t, "DRAFT", barcode=draft_bc, status="draft")
    await _item(client, t, "ZERO", qty=0, barcode=zero_bc)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, f"{draft_bc},{zero_bc}")).json()
    assert body["scanned"] == 0
    assert _reasons(body) == {draft_bc: "draft_item", zero_bc: "invalid_quantity"}
    assert await _lines(client, t, doc) == []


async def test_doc_scan_credit_note_prefers_sold_lot(client):
    t = await _register(client)
    sold = await _item(client, t, "CN", qty=1)
    await sell_item(client, _h(t), sold)
    await _item(client, t, "CN", qty=1)
    note = await _doc(client, t, "credit_note")

    body = (await _scan(client, t, note, "CN")).json()
    assert body["scanned"] == 1, body
    assert _ids(await _lines(client, t, note)) == [sold]


# ── Batch bounds, price list, idempotency ───────────────────────────────────────


async def test_doc_scan_rejects_bad_batches(client):
    from celerp_docs.routes import MAX_CODE_LEN, MAX_SCAN_BATCH

    t = await _register(client)
    doc = await _doc(client, t)
    for barcode in (" , ", ",".join(f"C{i}" for i in range(MAX_SCAN_BATCH + 1)), "X" * (MAX_CODE_LEN + 1)):
        r = await _scan(client, t, doc, barcode)
        assert r.status_code == 422, (barcode[:20], r.status_code, r.text)
    assert await _lines(client, t, doc) == []


async def _quotation_list(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation", "line_items": []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_scan_rejects_unknown_price_list(client):
    t = await _register(client)
    await _item(client, t, "PL", retail_price=5)
    doc = await _doc(client, t)
    lst = await _quotation_list(client, t)

    for path in (f"/docs/{doc}/scan", f"/lists/{lst}/scan"):
        r = await client.post(path, headers=_h(t), json={"barcode": "PL", "price_list": "No Such List"})
        assert r.status_code == 422, (path, r.status_code, r.text)
    assert await _lines(client, t, doc) == []
    assert (await client.get(f"/lists/{lst}", headers=_h(t))).json()["line_items"] == []


async def test_scan_cost_list_needs_cost_permission(client, session):
    t = await _register(client)
    await _price_lists(client, t, "Retail", "Cost")
    await _item(client, t, "CP", retail_price=5, cost_price=2)
    doc = await _doc(client, t)
    lst = await _quotation_list(client, t)
    operator = await _user_with_role(client, session, t, "operator")

    for path in (f"/docs/{doc}/scan", f"/lists/{lst}/scan"):
        r = await client.post(path, headers=_h(operator), json={"barcode": "CP", "price_list": "Cost"})
        assert r.status_code == 403, (path, r.status_code, r.text)
    assert await _lines(client, t, doc) == []


async def test_doc_scan_resubmit_replays(client):
    t = await _register(client)
    item = await _item(client, t, "RP")
    doc = await _doc(client, t)

    first = await _scan(client, t, doc, "RP", run_key="run-1")
    again = await _scan(client, t, doc, "RP", run_key="run-1")
    assert first.status_code == again.status_code == 200
    assert again.json()["duplicate"] is True and again.json()["scanned"] == 1
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [item]
    # The replay names the lines the run touched, so a retried run keeps them highlighted.
    assert [r["line_id"] for r in again.json()["results"]] == [lines[0]["line_id"]]
    assert again.json()["results"] == first.json()["results"]


@pytest.mark.parametrize("on", ["doc", "list"])
async def test_a_retried_run_that_placed_nothing_replays_its_first_answer(client, on):
    """A run whose every code failed is recorded too: retried after one of its codes became an
    item, it returns its first answer and adds nothing, as a run that placed lines does."""
    t = await _register(client)
    record = await _doc(client, t) if on == "doc" else await _quotation_list(client, t)
    path = f"/{'docs' if on == 'doc' else 'lists'}/{record}/scan"
    body = {"barcode": "LATE1,LATE2", "run_key": "run-none"}

    first = await client.post(path, headers=_h(t), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["scanned"] == 0 and len(first.json()["failed"]) == 2
    await _item(client, t, "LATE1")
    again = await client.post(path, headers=_h(t), json=body)
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is True and again.json()["scanned"] == 0
    assert again.json()["results"] == first.json()["results"]
    assert again.json()["failed"] == first.json()["failed"]
    read = await client.get(f"/{'docs' if on == 'doc' else 'lists'}/{record}", headers=_h(t))
    assert read.json().get("line_items") == []


async def test_doc_scan_reused_key_conflicts(client):
    t = await _register(client)
    await _item(client, t, "K1")
    await _item(client, t, "K2")
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "K1", run_key="run-k")).status_code == 200
    r = await _scan(client, t, doc, "K2", run_key="run-k")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "scan_run_conflict"
    assert r.json()["version"] == (await _get(client, t, doc))["version"]
    assert [line["sku"] for line in await _lines(client, t, doc)] == ["K1"]


# ── Status, writer refusals, permission, read failures ──────────────────────────


async def test_doc_scan_refused_when_not_draft(client):
    t = await _register(client)
    first = await _item(client, t, "ND1")
    await _item(client, t, "ND2")
    finalized = await _doc(client, t, lines=[{"entity_id": first, "sku": "ND1", "description": "ND1",
                                              "quantity": 1, "unit_price": 10}])
    assert (await client.post(f"/docs/{finalized}/finalize", headers=_h(t))).status_code == 200
    void = await _doc(client, t)
    assert (await client.post(f"/docs/{void}/void", headers=_h(t), json={})).status_code == 200

    for doc, key in ((finalized, "docs.edit_locked"), (void, "docs.edit_void")):
        before = await _get(client, t, doc)
        r = await _scan(client, t, doc, "ND2")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["message_key"] == key
        after = await _get(client, t, doc)
        assert after["version"] == before["version"] and after["line_items"] == before["line_items"]


async def test_doc_scan_writer_refusal_commits_nothing(client, monkeypatch):
    import celerp.events.engine as engine

    def _refuse(*_a, **_k):
        raise HTTPException(status_code=409, detail="refused by the writer")

    t = await _register(client)
    await _item(client, t, "WR")
    doc = await _doc(client, t)
    before = await _get(client, t, doc)
    monkeypatch.setattr(engine, "assert_document_item_uniqueness", _refuse)

    r = await _scan(client, t, doc, "WR", run_key="run-w")
    assert r.status_code == 409, r.text
    after = await _get(client, t, doc)
    assert after["version"] == before["version"]
    assert after["line_items"] == [] and not after.get("scan_runs")


async def test_doc_scan_requires_edit_documents(client, session):
    t = await _register(client)
    await _item(client, t, "PERM")
    doc = await _doc(client, t)
    viewer = await _user_with_role(client, session, t, "viewer")

    r = await _scan(client, viewer, doc, "PERM")
    assert r.status_code == 403, r.text
    assert await _lines(client, t, doc) == []


@pytest.mark.parametrize("reader", ["_get_unit_map", "get_price_config"])
async def test_doc_scan_db_read_failure_writes_nothing(client, monkeypatch, reader):
    import celerp_docs.routes as docs

    async def _fail(*_a, **_k):
        raise RuntimeError("database read failed")

    t = await _register(client)
    await _item(client, t, "RF")
    doc = await _doc(client, t)
    before = await _get(client, t, doc)
    monkeypatch.setattr(docs, reader, _fail)

    try:
        r = await _scan(client, t, doc, "RF", run_key="run-r")
    except RuntimeError:
        pass
    else:
        assert r.status_code == 500, r.text
    after = await _get(client, t, doc)
    assert after["version"] == before["version"]
    assert after["line_items"] == [] and not after.get("scan_runs")


# ── The per-type config drives placement and the status gate ────────────────────


async def test_scan_behavior_config_drives_doc_scan(client, monkeypatch):
    import celerp.services.list_behavior as lb
    from celerp.services.list_behavior import ScanBehavior, scan_behavior
    from celerp_docs.routes import _scan_status_allowed

    assert {dt: isinstance(scan_behavior("docs", dt), ScanBehavior) for dt in DOC_TYPES} == {dt: True for dt in DOC_TYPES}
    assert scan_behavior("docs", "credit_note").lots == "sold"

    t = await _register(client)
    sold = await _item(client, t, "CFG", qty=1)
    await sell_item(client, _h(t), sold)
    available = await _item(client, t, "CFG", qty=1)
    monkeypatch.setitem(lb.DOC_SCAN_BEHAVIOR, "credit_note", ScanBehavior("noop", True, "consolidate"))
    note = await _doc(client, t, "credit_note")
    body = (await _scan(client, t, note, "CFG")).json()
    assert body["scanned"] == 1, body
    assert _ids(await _lines(client, t, note)) == [available]

    assert not _scan_status_allowed(scan_behavior("docs", "invoice"), "finalized")
    monkeypatch.setitem(lb.DOC_SCAN_BEHAVIOR, "invoice", ScanBehavior("count", True, "consolidate"))
    assert _scan_status_allowed(scan_behavior("docs", "invoice"), "finalized")


# ── Quantity: a repeat raises the line, a whole piece is placed once ────────────


async def _list_scan(client, t, list_id, barcode, **extra):
    return await client.post(f"/lists/{list_id}/scan", headers=_h(t), json={"barcode": barcode, **extra})


async def _list_lines(client, t, list_id) -> list[dict]:
    r = await client.get(f"/lists/{list_id}", headers=_h(t))
    assert r.status_code == 200, r.text
    return r.json().get("line_items") or []


# Documents that buy stock in, and documents that sell or take it out (shipping docs scan as lists).
BUY_SIDE = ["purchase_order", "subscription_po", "bill", "consignment_in"]
SALES_SIDE = ["invoice", "memo", "proforma", "subscription_invoice", "credit_note", "debit_note",
              "production_order"]


@pytest.mark.parametrize("doc_type", BUY_SIDE)
async def test_buy_side_scan_of_an_out_of_stock_item_raises_its_line_per_scan(client, doc_type):
    """On a document that buys stock in, a splittable item with nothing on hand starts a line of one,
    and every repeat raises that line by one, in one run and across runs."""
    t = await _register(client)
    item = await _item(client, t, "BS-ZERO", qty=0)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, "BS-ZERO,BS-ZERO")).json()
    assert body["scanned"] == 2 and body["failed"] == [], body
    assert (await _scan(client, t, doc, "BS-ZERO")).json()["scanned"] == 1
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [item]
    assert [float(line["quantity"]) for line in lines] == [3.0]


@pytest.mark.parametrize("doc_type", BUY_SIDE)
async def test_buy_side_repeat_of_a_lot_that_may_not_be_split_is_another_line(client, doc_type):
    """Buying in, a repeat of a lot that may not be split is another lot of the same item: a second
    line at the lot's quantity, never reported."""
    t = await _register(client)
    lot = await _item(client, t, "BS-WHOLE", qty=5, allow_splitting=False)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, "BS-WHOLE,BS-WHOLE")).json()
    assert body["scanned"] == 2 and body["failed"] == [], body
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot, lot]
    assert [float(line["quantity"]) for line in lines] == [5.0, 5.0]


@pytest.mark.parametrize("doc_type", [*SALES_SIDE, "quotation", *BUY_SIDE])
async def test_splittable_lot_scanned_three_times_is_one_line_of_three(client, doc_type):
    """A splittable lot starts a line of one and each repeat raises it by one, on every document
    type, and every scan's result names that line."""
    t = await _register(client)
    bc, _ = _codes()
    lot = await _item(client, t, "SPL", qty=5, allow_splitting=True, barcode=bc)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, "SPL,SPL")).json()
    assert body["scanned"] == 2 and body["failed"] == [], body
    assert (await _scan(client, t, doc, bc)).json()["scanned"] == 1  # the barcode raises the same line
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot]
    assert [float(line["quantity"]) for line in lines] == [3.0]
    assert {r["line_id"] for r in body["results"]} == {lines[0]["line_id"]}


@pytest.mark.parametrize("doc_type", SALES_SIDE)
async def test_selling_scan_never_takes_a_line_past_what_is_on_hand(client, doc_type):
    """Where goods leave stock, a repeat that would take the line past what is on hand is reported
    per code and never placed."""
    t = await _register(client)
    lot = await _item(client, t, "CAP", qty=2, allow_splitting=True)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, "CAP,CAP,CAP")).json()
    assert body["scanned"] == 2 and _reasons(body) == {"CAP": "insufficient_stock"}, body
    assert body["failed"][0]["label"] == "CAP: insufficient stock - 3 on this document, available 2"
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot] and float(lines[0]["quantity"]) == 2.0


# A credit note takes the sold lot back, so a product code naming several lots is ambiguous there.
@pytest.mark.parametrize("doc_type", [d for d in SALES_SIDE if d != "credit_note"])
async def test_selling_scan_caps_every_line_of_a_product_at_its_on_hand(client, doc_type):
    """Where goods leave stock, the cap counts every line drawing on the product's stock: the line a
    product code binds and the lines its lots' own barcodes add, in either order."""
    t = await _register(client)
    older_bc, newer_bc = _codes()
    await _item(client, t, "POOL", qty=15, allow_splitting=True, barcode=older_bc)
    await _item(client, t, "POOL", qty=5, allow_splitting=True, barcode=newer_bc)
    label = "POOL: insufficient stock - 21 on this document, available 20"

    doc = await _doc(client, t, doc_type)
    assert (await _scan(client, t, doc, ",".join(["POOL"] * 20))).json()["scanned"] == 20
    body = (await _scan(client, t, doc, newer_bc)).json()
    assert body["scanned"] == 0 and _reasons(body) == {newer_bc: "insufficient_stock"}, body
    assert body["failed"][0]["label"] == label
    assert sum(float(line["quantity"]) for line in await _lines(client, t, doc)) == 20.0

    doc = await _doc(client, t, doc_type)
    assert (await _scan(client, t, doc, ",".join([newer_bc] * 5))).json()["scanned"] == 5
    body = (await _scan(client, t, doc, ",".join(["POOL"] * 20))).json()
    assert body["scanned"] == 15 and [f["reason"] for f in body["failed"]] == ["insufficient_stock"] * 5, body
    assert body["failed"][0]["label"] == label
    assert sum(float(line["quantity"]) for line in await _lines(client, t, doc)) == 20.0


@pytest.mark.parametrize("doc_type", [d for d in SALES_SIDE if d != "credit_note"])
async def test_selling_scan_cap_counts_a_lot_made_whole_after_the_product_line(client, doc_type):
    """A lot switched to may-not-split after a product line already holds the product's whole
    on-hand still draws on the same stock: its scan is refused, not added on top."""
    t = await _register(client)
    older_bc, newer_bc = _codes()
    await _item(client, t, "POOL", qty=15, allow_splitting=True, barcode=older_bc)
    newer = await _item(client, t, "POOL", qty=5, allow_splitting=True, barcode=newer_bc)
    doc = await _doc(client, t, doc_type)
    assert (await _scan(client, t, doc, ",".join(["POOL"] * 20))).json()["scanned"] == 20

    r = await client.patch(f"/items/{newer}", headers=_h(t),
                           json={"fields_changed": {"allow_splitting": {"new": False}}})
    assert r.status_code == 200, r.text
    body = (await _scan(client, t, doc, newer_bc)).json()
    assert body["scanned"] == 0 and _reasons(body) == {newer_bc: "insufficient_stock"}, body
    assert body["failed"][0]["label"] == "POOL: insufficient stock - 25 on this document, available 20"
    assert sum(float(line["quantity"]) for line in await _lines(client, t, doc)) == 20.0


async def test_selling_scan_names_the_holder_of_a_reserved_lot_before_the_product_cap(client):
    """A lot reserved on another document is refused naming that document, even when the product's
    free stock is already on this one: the specific reason wins over the stock cap."""
    t = await _register(client)
    older_bc, newer_bc = _codes()
    await _item(client, t, "POOL", qty=15, allow_splitting=True, barcode=older_bc)
    newer = await _item(client, t, "POOL", qty=5, allow_splitting=True, barcode=newer_bc)
    holder_number = (await _get(client, t, await reserve_item(client, _h(t), newer)))["ref_id"]
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, ",".join(["POOL"] * 15))).json()["scanned"] == 15
    body = (await _scan(client, t, doc, newer_bc)).json()
    assert body["scanned"] == 0 and _reasons(body) == {newer_bc: "unavailable"}, body
    assert holder_number in body["failed"][0]["label"]


async def test_selling_scan_cap_draws_on_the_product_even_when_its_sku_is_another_items_barcode(client):
    """The stock a product's lines draw on is every lot carrying its SKU, whatever other item happens
    to use that SKU as a barcode."""
    t = await _register(client)
    older_bc, newer_bc = _codes()
    shared = "7070" + older_bc[:6]
    await _item(client, t, shared, qty=15, allow_splitting=True, barcode=older_bc, gtin="96385074")
    await _item(client, t, shared, qty=5, allow_splitting=True, barcode=newer_bc, gtin="96385074")
    await _item(client, t, "OTHER", qty=1, barcode=shared)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, ",".join(["96385074"] * 20))).json()
    assert body["scanned"] == 20 and body["failed"] == [], body
    body = (await _scan(client, t, doc, newer_bc)).json()
    assert _reasons(body) == {newer_bc: "insufficient_stock"}, body
    assert body["failed"][0]["label"] == f"{shared}: insufficient stock - 21 on this document, available 20"


async def test_a_scan_run_reads_inventory_once_however_many_products_it_names(client, _db_engine):
    """One scan run loads the company's items once and draws every product's stock from that load."""
    from contextlib import contextmanager
    from sqlalchemy import event

    t = await _register(client)
    for sku in ("ONE-A", "ONE-B", "ONE-C"):
        await _item(client, t, sku, qty=3, allow_splitting=True)
    doc = await _doc(client, t)
    loads: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "FROM projections" in statement and "entity_id" not in statement.split("WHERE", 1)[-1] \
                and "item" in repr(parameters):
            loads.append(statement)

    @contextmanager
    def listening():
        event.listen(_db_engine.sync_engine, "before_cursor_execute", record)
        try:
            yield
        finally:
            event.remove(_db_engine.sync_engine, "before_cursor_execute", record)

    with listening():
        body = (await _scan(client, t, doc, "ONE-A,ONE-B,ONE-C,ONE-A")).json()

    assert body["scanned"] == 4, body
    assert len(loads) == 1, loads


async def test_repeat_refusal_names_the_line_already_holding_the_piece(client):
    """A repeat reported as already on the document names that line, so the page highlights it."""
    t = await _register(client)
    await _item(client, t, "HOLD1", qty=1, allow_splitting=False)
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "HOLD1")).json()["scanned"] == 1
    body = (await _scan(client, t, doc, "HOLD1")).json()
    line_id = (await _lines(client, t, doc))[0]["line_id"]
    assert _reasons(body) == {"HOLD1": "duplicate_scan"}, body
    assert body["failed"][0]["line_id"] == line_id
    assert [r["line_id"] for r in body["results"]] == [line_id]


@pytest.mark.parametrize("doc_type", [*SALES_SIDE, "quotation"])
async def test_doc_scan_repeat_lot_is_reported_not_added_again(client, doc_type):
    """A lot that may not be split takes its on-hand quantity once; scanning it again is reported as
    already on the document and adds neither a line nor quantity."""
    t = await _register(client)
    lot = await _item(client, t, "RL", qty=5, allow_splitting=False)
    doc = await _doc(client, t, doc_type)

    body = (await _scan(client, t, doc, "RL,RL,RL")).json()
    assert body["scanned"] == 1, body
    assert [f["reason"] for f in body["failed"]] == ["duplicate_scan", "duplicate_scan"]
    assert all("already on this document" in f["label"] for f in body["failed"])
    body = (await _scan(client, t, doc, "RL")).json()
    assert body["scanned"] == 0 and _reasons(body) == {"RL": "duplicate_scan"}
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot] and float(lines[0]["quantity"]) == 5.0


async def test_shipping_doc_scan_repeat_lot_is_reported_not_added_again(client):
    t = await _register(client)
    lot = await _item(client, t, "SHR", qty=5, allow_splitting=False)
    split = await _item(client, t, "SHS", qty=5, allow_splitting=True)
    r = await client.post("/lists", headers=_h(t), json={"list_type": "shipping_doc", "line_items": []})
    assert r.status_code == 200, r.text
    lst = r.json()["id"]

    body = (await _list_scan(client, t, lst, "SHR,SHR,SHR")).json()
    assert body["scanned"] == 1 and [f["reason"] for f in body["failed"]] == ["duplicate_scan"] * 2, body
    assert (await _list_scan(client, t, lst, "SHS,SHS")).json()["scanned"] == 2
    lines = await _list_lines(client, t, lst)
    assert _ids(lines) == [lot, split]
    assert [float(line["quantity"]) for line in lines] == [5.0, 2.0]


async def test_quotation_scan_takes_on_hand_and_reports_a_repeat(client):
    t = await _register(client)
    lot = await _item(client, t, "QS", qty=5, allow_splitting=False)
    doc = await _doc(client, t, "quotation")

    body = (await _scan(client, t, doc, "QS,QS,QS")).json()
    assert body["scanned"] == 1 and _reasons(body) == {"QS": "duplicate_scan"}, body
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [lot] and float(lines[0]["quantity"]) == 5.0


async def test_quotation_scan_raises_an_out_of_stock_item_per_scan(client):
    t = await _register(client)
    item = await _item(client, t, "QZ", qty=0)
    doc = await _doc(client, t, "quotation")

    assert (await _scan(client, t, doc, "QZ,QZ")).json()["scanned"] == 2
    assert (await _scan(client, t, doc, "QZ")).json()["scanned"] == 1
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [item] and [float(line["quantity"]) for line in lines] == [3.0]


async def test_quote_list_scan_raises_an_out_of_stock_item_per_scan(client):
    t = await _register(client)
    item = await _item(client, t, "LQZ", qty=0)
    lst = await _quotation_list(client, t)

    assert (await _list_scan(client, t, lst, "LQZ,LQZ")).json()["scanned"] == 2
    assert (await _list_scan(client, t, lst, "LQZ")).json()["scanned"] == 1
    lines = await _list_lines(client, t, lst)
    assert _ids(lines) == [item] and [float(line["quantity"]) for line in lines] == [3.0]


@pytest.mark.parametrize("doc_type", ["out_of_stock_bill", "stocked_bill", "quotation", "invoice"])
@pytest.mark.parametrize("splitting", [True, False])
async def test_rfid_tag_read_twice_in_a_run_is_one_read_and_reported_in_a_later_run(client, doc_type, splitting):
    """An RFID tag names one physical piece: a reader passing it twice in one run is one read and
    says nothing, and the same tag in a later run is reported as already on the document."""
    t = await _register(client)
    item = await _item(client, t, "EP", qty=0 if doc_type in ("out_of_stock_bill", "quotation") else 1,
                       rfid_epc="E200BB01", allow_splitting=splitting)
    doc = await _doc(client, t, doc_type if doc_type in ("quotation", "invoice") else "bill")

    body = (await _scan(client, t, doc, "E200BB01,e200bb01,E200BB01")).json()
    assert body["scanned"] == 1 and body["failed"] == [] and len(body["results"]) == 1, body
    body = (await _scan(client, t, doc, "E200BB01")).json()
    assert body["scanned"] == 0 and _reasons(body) == {"E200BB01": "duplicate_scan"}, body
    assert "already on this document" in body["failed"][0]["label"]
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [item] and float(lines[0]["quantity"]) == 1.0


async def test_distinct_rfid_tags_in_one_run_each_add_their_piece(client):
    t = await _register(client)
    tags = [f"E200CC0{n}" for n in range(5)]
    pieces = [await _item(client, t, "TAGGED", qty=1, rfid_epc=tag, allow_splitting=False) for tag in tags]
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, ",".join(tags))).json()
    assert body["scanned"] == 5 and body["failed"] == [], body
    lines = await _lines(client, t, doc)
    assert _ids(lines) == pieces and [float(line["quantity"]) for line in lines] == [1.0] * 5


async def test_doc_scan_service_item_raises_its_line_per_scan(client):
    """An item kept in no stock has no quantity on hand: it starts a line of one and each repeat
    raises it by one, never capped."""
    t = await _register(client)
    item = await _item(client, t, "SVC", qty=0, inventory_type="service")
    doc = await _doc(client, t)

    assert (await _scan(client, t, doc, "SVC,SVC")).json()["scanned"] == 2
    assert (await _scan(client, t, doc, "SVC")).json()["scanned"] == 1
    lines = await _lines(client, t, doc)
    assert _ids(lines) == [item] and [float(line["quantity"]) for line in lines] == [3.0]


async def test_list_scan_service_item_raises_its_line_per_scan(client):
    t = await _register(client)
    item = await _item(client, t, "LCT", qty=0, inventory_type="service")
    lst = await _quotation_list(client, t)

    assert (await _list_scan(client, t, lst, "LCT,LCT")).json()["scanned"] == 2
    assert (await _list_scan(client, t, lst, "LCT")).json()["scanned"] == 1
    lines = await _list_lines(client, t, lst)
    assert _ids(lines) == [item] and [float(line["quantity"]) for line in lines] == [3.0]


async def test_two_open_pages_scanning_one_document_both_land(client):
    """Two scanners with the same document open each add a different item: both lines persist.
    The retired per-scan path saved each page's whole line set, so the later save dropped the
    other page's line."""
    t = await _register(client)
    bc_x, bc_y = _codes()
    x = await _item(client, t, "PG-X", barcode=bc_x)
    y = await _item(client, t, "PG-Y", barcode=bc_y)
    doc = await _doc(client, t)
    page_a, page_b = await _get(client, t, doc), await _get(client, t, doc)
    assert page_a["version"] == page_b["version"]

    assert (await _scan(client, t, doc, bc_x, run_key="page-a")).json()["scanned"] == 1
    assert (await _scan(client, t, doc, bc_y, run_key="page-b")).json()["scanned"] == 1
    assert _ids(await _lines(client, t, doc)) == [x, y]


# ── Lists read the same config through the same loop ────────────────────────────


def _patch_list_scan(monkeypatch, **changes):
    """Make every list scan read a changed scan config, to show the list path follows it."""
    import dataclasses

    import celerp_docs.routes as docs

    real = docs.scan_behavior

    def patched(kind, doc_type):
        sb = real(kind, doc_type)
        return dataclasses.replace(sb, **changes) if kind == "lists" else sb

    monkeypatch.setattr(docs, "scan_behavior", patched)


async def test_list_scan_reads_priced_from_config(client, monkeypatch):
    t = await _register(client)
    await _item(client, t, "LP", retail_price=9)
    lst = await _quotation_list(client, t)
    _patch_list_scan(monkeypatch, priced=False)

    assert (await _list_scan(client, t, lst, "LP")).json()["scanned"] == 1
    assert not (await _list_lines(client, t, lst))[0].get("unit_price")


async def test_list_scan_reads_lots_from_config(client, monkeypatch):
    t = await _register(client)
    await _item(client, t, "LL", qty=2, allow_splitting=True)
    await _item(client, t, "LL", qty=3, allow_splitting=True)
    lst = await _quotation_list(client, t)
    assert _reasons((await _list_scan(client, t, lst, "LL")).json()) == {"LL": "ambiguous_sku"}

    _patch_list_scan(monkeypatch, lots="consolidate")
    assert (await _list_scan(client, t, lst, "LL")).json()["scanned"] == 1
    assert [line["sku"] for line in await _list_lines(client, t, lst)] == ["LL"]


def test_one_scan_loop_serves_lists_and_documents():
    import inspect

    import celerp_docs.routes as docs

    for endpoint in (docs.scan_list, docs.scan_doc):
        source = inspect.getsource(endpoint)
        assert "_apply_scan_run(" in source and "for code in codes" not in source, endpoint.__name__
        assert "is_money_list" not in source and "require_positive" not in source, endpoint.__name__


# ── The scan ledger belongs to the record it was scanned on ─────────────────────


async def test_conversions_and_copies_start_without_the_scan_ledger(client):
    t = await _register(client)
    await _item(client, t, "LG-1", qty=1)
    await _item(client, t, "LG-2", qty=1)

    lst = await _quotation_list(client, t)
    assert (await _list_scan(client, t, lst, "LG-1", run_key="run-q")).json()["scanned"] == 1
    copy = await client.post(f"/lists/{lst}/duplicate", headers=_h(t))
    assert copy.status_code == 200, copy.text
    copied = (await client.get(f"/lists/{copy.json()['id']}", headers=_h(t))).json()
    assert not copied.get("scan_runs"), copied.get("scan_runs")
    assert (await client.post(f"/lists/{lst}/finalize", headers=_h(t))).status_code == 200
    conv = await client.post(f"/lists/{lst}/convert", headers=_h(t), json={"target_type": "invoice"})
    assert conv.status_code == 200, conv.text
    invoice = await _get(client, t, conv.json()["target_doc_id"])
    assert invoice["line_items"] and not invoice.get("scan_runs"), invoice.get("scan_runs")
    # The same key on the new invoice is a new run, not a replay of the quotation's.
    body = (await _scan(client, t, invoice["id"], "LG-2", run_key="run-q")).json()
    assert body["scanned"] == 1 and not body.get("duplicate"), body

    memo = await _doc(client, t, "memo")
    item = await _item(client, t, "LG-3", qty=1)
    assert (await _scan(client, t, memo, "LG-3", run_key="run-m")).json()["scanned"] == 1
    assert (await client.post(f"/docs/{memo}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [item]})
    assert r.status_code == 200, r.text
    conv = await client.post(f"/docs/{memo}/convert", headers=_h(t), json={})
    assert conv.status_code == 200, conv.text
    assert not (await _get(client, t, conv.json()["target_doc_id"])).get("scan_runs")


async def test_consignment_bill_starts_without_the_scan_ledger(client):
    """Converting a consignment to its vendor bill drops the consignment's scan runs, as every other
    conversion does: a run key replayed on the bill is a new run."""
    t = await _register(client)
    template = await _item(client, t, "LG-CON", qty=0)
    r = await client.post("/docs", headers=_h(t), json={
        "doc_type": "consignment_in", "contact_id": "supplier:1", "line_items": []})
    assert r.status_code == 200, r.text
    con = r.json()["id"]
    assert (await _scan(client, t, con, "LG-CON", run_key="run-c")).json()["scanned"] == 1
    assert (await _get(client, t, con)).get("scan_runs")
    assert (await client.post(f"/docs/{con}/finalize", headers=_h(t))).status_code == 200
    r = await client.post(f"/docs/{con}/receive", headers=_h(t), json={"location_id": "", "received_items": [
        {"item_id": template, "sku": "LG-CON", "name": "Lot", "quantity_received": 1,
         "po_line_index": 0, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    conv = await client.post(f"/docs/{con}/convert", headers=_h(t))
    assert conv.status_code == 200, conv.text
    bill = await _get(client, t, conv.json()["target_doc_id"])
    assert bill["doc_type"] == "bill" and not bill.get("scan_runs"), bill.get("scan_runs")


@pytest.mark.parametrize("kind", ["docs", "lists"])
async def test_patch_cannot_write_the_scan_ledger(client, kind):
    """The scan ledger is written only by the scan itself: a PATCH carrying it is refused like any
    other lifecycle-owned field, so a caller can neither forge a replay nor erase one."""
    t = await _register(client)
    await _item(client, t, "PT", qty=0, inventory_type="service")
    record = await (_doc(client, t) if kind == "docs" else _quotation_list(client, t))
    scan = _scan if kind == "docs" else _list_scan
    assert (await scan(client, t, record, "PT", run_key="run-p")).json()["scanned"] == 1

    r = await client.patch(f"/{kind}/{record}", headers=_h(t),
                           json={"fields_changed": {"scan_runs": {"old": None, "new": []}}})
    assert r.status_code == 422, r.text
    # The recorded run still replays, so the retry adds nothing.
    body = (await scan(client, t, record, "PT", run_key="run-p")).json()
    assert body.get("duplicate"), body
    reader = _lines if kind == "docs" else _list_lines
    assert float((await reader(client, t, record))[0]["quantity"]) == 1.0


async def test_old_scan_key_replays_after_recent_cache_pruned(client, monkeypatch):
    from celerp_docs import routes
    monkeypatch.setattr(routes, "_MAX_SCAN_RUNS", 1)
    token = await _register(client)
    await _item(client, token, "OLD-ONE", qty=2)
    await _item(client, token, "OLD-TWO", qty=2)
    doc = await _doc(client, token, "bill")
    first = await _scan(client, token, doc, "OLD-ONE", run_key="scan-old")
    assert first.status_code == 200 and first.json()["scanned"] == 1
    second = await _scan(client, token, doc, "OLD-TWO", run_key="scan-new")
    assert second.status_code == 200 and second.json()["scanned"] == 1
    previous = await _scan(client, token, doc, "OLD-ONE", run_key="scan-old")
    assert previous.status_code == 200 and previous.json()["duplicate"] is True
    assert previous.json()["results"] == first.json()["results"]
    assert len(await _lines(client, token, doc)) == 2
    mismatch = await _scan(client, token, doc, "OLD-TWO", run_key="scan-old")
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "scan_run_conflict"
    assert len(await _lines(client, token, doc)) == 2


async def test_failed_only_scan_key_is_durable(client):
    token = await _register(client)
    doc = await _doc(client, token)
    first = await _scan(client, token, doc, "AVAILABLE-LATER", run_key="scan-fail")
    assert first.status_code == 200 and first.json()["scanned"] == 0
    await _item(client, token, "AVAILABLE-LATER")
    replay = await _scan(client, token, doc, "AVAILABLE-LATER", run_key="scan-fail")
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    assert replay.json()["failed"] == first.json()["failed"]
    assert await _lines(client, token, doc) == []


async def test_list_scan_key_survives_recent_cache_prune(client, monkeypatch):
    from celerp_docs import routes
    monkeypatch.setattr(routes, "_MAX_SCAN_RUNS", 1)
    token = await _register(client)
    await _item(client, token, "LIST-ONE", qty=2)
    await _item(client, token, "LIST-TWO", qty=2)
    listing = await _quotation_list(client, token)
    a = await _list_scan(client, token, listing, "LIST-ONE", run_key="list-old")
    b = await _list_scan(client, token, listing, "LIST-TWO", run_key="list-new")
    assert a.status_code == b.status_code == 200
    replay = await _list_scan(client, token, listing, "LIST-ONE", run_key="list-old")
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    assert len(await _list_lines(client, token, listing)) == 2
