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

pytestmark = pytest.mark.asyncio

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
    from celerp.services.session_tracker import clear as _clear_tracker
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
    t = await _register(client)
    a = await _item(client, t, "QA", qty=3)
    b = await _item(client, t, "QB", qty=2)
    c = await _item(client, t, "QC", qty=4)
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
    import celerp_inventory.routes as inv
    from celerp_inventory.routes import ResolveResult, resolve_items_by_codes as _real

    t = await _register(client)
    bc, _ = _codes()
    await _item(client, t, "AMB", allow_splitting=False)
    await _item(client, t, "AMB", allow_splitting=False)
    await _item(client, t, "COL", barcode=bc)

    async def _collide(session, company_id, codes):
        base = await _real(session, company_id, codes)
        got = base.get(bc)
        if got is not None and got.kind == "barcode":
            base[bc] = ResolveResult("barcode", list(got.matches) * 2)
        return base

    monkeypatch.setattr(inv, "resolve_items_by_codes", _collide)
    doc = await _doc(client, t)

    body = (await _scan(client, t, doc, f"AMB,{bc}")).json()
    assert body["scanned"] == 0
    assert _reasons(body) == {"AMB": "ambiguous_sku", bc: "duplicate_barcode"}
    assert await _lines(client, t, doc) == []


async def test_doc_scan_refuses_held_lot(client):
    t = await _register(client)
    held_bc, out_bc = _codes()
    reserved = await _item(client, t, "HELD", qty=1, barcode=held_bc)
    holder = await reserve_item(client, _h(t), reserved)
    holder_number = (await _get(client, t, holder))["ref_id"]

    out = await _item(client, t, "OUT", qty=1, barcode=out_bc)
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

    # The invoice made from the memo may take the memo's own lot.
    conv = await client.post(f"/docs/{memo}/convert", headers=_h(t), json={})
    assert conv.status_code == 200, conv.text
    invoice = conv.json().get("target_doc_id") or conv.json().get("id")
    before = await _lines(client, t, invoice)
    body = (await _scan(client, t, invoice, out_bc)).json()
    assert body["scanned"] == 1, body
    assert _ids(await _lines(client, t, invoice)) == _ids(before) + [out]


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
    assert _ids(await _lines(client, t, doc)) == [item]


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
