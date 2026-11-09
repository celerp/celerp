# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every automatic posting names its source by the exact record id.

GL Impact finds what a document or List posted by matching the source keys in each
entry's ledger metadata against the record's id. A posting that writes a derived
value into one of those keys (an id with a suffix, a composed string) is invisible
to the record it belongs to. `census()` walks the code for every door into that
boundary: each metadata literal carrying a source key and each keyword a source key
is passed under to the automatic poster. A value built in place (an f-string, a
concatenation) is a derived id and fails the guard, so a new door written that way
fails here before it reaches anyone's books.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp_accounting.routes import _DOC_SOURCE_KEYS

REPO = Path(__file__).resolve().parents[1]
_ROOTS = ("celerp", "default_modules")


def _poster_names() -> set[str]:
    """The functions of the automatic poster: a call to one of these is a door."""
    tree = ast.parse((REPO / "celerp/services/auto_je.py").read_text())
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def census() -> list[tuple[str, int, str, str, str]]:
    """(file, line, door, key, value source) for every source key handed to a posting."""
    posters = _poster_names()
    rows = []
    for root in _ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "/tests/" in str(path) or "/migrations/" in str(path):
                continue
            text = path.read_text()
            if not any(k in text for k in _DOC_SOURCE_KEYS):
                continue
            rel = str(path.relative_to(REPO))
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.Call):
                    continue
                name = _callee(node)
                for kw in node.keywords:
                    if kw.arg in ("metadata_", "metadata") and isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and k.value in _DOC_SOURCE_KEYS:
                                rows.append((rel, k.lineno, f"{name}(metadata)", k.value, ast.unparse(v)))
                    elif kw.arg in _DOC_SOURCE_KEYS and name in posters:
                        rows.append((rel, kw.value.lineno, name, kw.arg, ast.unparse(kw.value)))
    return rows


def violations() -> list[tuple[str, int, str, str, str]]:
    return [r for r in census() if r[4].startswith(("f'", 'f"')) or "+" in r[4]]


def test_every_posting_names_its_source_by_the_exact_record_id():
    assert census(), "the census found no doors; its walk is broken"
    assert violations() == []


async def _gl(client, h, record_id) -> dict:
    r = await client.get(f"/accounting/journal/by-doc/{record_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _unresolved_sources(session, company_id) -> list[tuple[str, str, str]]:
    """Every journal entry event whose source key names no record in the books."""
    session.expire_all()
    events = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id,
        LedgerEntry.event_type.like("acc.journal_entry.%")))).scalars().all()
    named = {(e.entity_id, k, str(v)) for e in events for k, v in (e.metadata_ or {}).items()
             if k in _DOC_SOURCE_KEYS and v}
    known = set((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_id.in_(sorted({v for _, _, v in named}))))).scalars().all())
    return sorted(row for row in named if row[2] not in known)


async def _written_off_then_corrected(client, session, prefix=None):
    """A demo item written off in full on a write-off List (numbered under ``prefix`` when given),
    then the disposed lot's cost corrected: the correction posts its own entry to the List's
    expense account."""
    from test_tax_acceptance_matrix import _demo_company

    auth, _contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    if prefix is not None:
        r = await client.patch("/docs/sequences/writeoff", headers=h, json={"prefix": prefix})
        assert r.status_code == 200, r.text
    items = (await client.get("/items?limit=50", headers=h)).json()
    items = items.get("items", items) if isinstance(items, dict) else items
    item = next(i for i in items if float(i.get("quantity") or 0) > 0 and float(i.get("cost_total") or 0) > 0)
    eid = item.get("entity_id") or item.get("id")
    qty, cost = float(item["quantity"]), float(item["cost_total"])
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [eid]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    line = next(l for l in (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"] if l["item_id"] == eid)
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h,
                          json={"line_id": line["line_id"], "qty_out": qty, "account": "6950"})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/lists/{wo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{wo}/write-off", headers=h)).status_code == 200
    r = await client.patch(f"/items/{eid}", headers=h,
                           json={"fields_changed": {"cost_total": {"old": cost, "new": round(cost * 1.5, 2)}}})
    assert r.status_code == 200, r.text
    return auth, wo, round(cost * 0.5, 2)


async def test_a_write_off_shows_the_cost_correction_it_posted(client, session):
    auth, wo, correction = await _written_off_then_corrected(client, session)
    h = auth["headers"]
    gl = await _gl(client, h, wo)
    assert len(gl["entries"]) == 2, [e["je_id"] for e in gl["entries"]]
    expense = sorted(l["debit"] for e in gl["entries"] for l in e["lines"] if l["account"] == "6950")
    assert correction in expense, expense
    assert all(wo not in e["memo"] for e in gl["entries"]), [e["memo"] for e in gl["entries"]]
    assert await _unresolved_sources(session, auth["company_id"]) == []

    # Undo takes both entries back with the List, and both stay on its GL Impact as voids.
    assert (await client.post(f"/lists/{wo}/undo-write-off", headers=h)).status_code == 200
    gl = await _gl(client, h, wo)
    assert [e["status"] for e in gl["entries"]] == ["void", "void"]
    assert gl["total_debit"] == gl["total_credit"] == 0
    assert await _unresolved_sources(session, auth["company_id"]) == []


@pytest.mark.parametrize("prefix", [None, "WO:cost:ABC"])
async def test_a_cost_correction_recorded_by_2_8_0_shows_on_its_write_off(client, session, prefix):
    """2.8.0 named a write-off's cost correction by its scope (List id, ":cost:", restatement, lot)
    rather than the List id. The ledger keeps those rows as written, so an upgraded company's
    write-off still lists them, read back to the List, even one whose own number holds ":cost:"."""
    auth, wo, correction = await _written_off_then_corrected(client, session, prefix)
    h = auth["headers"]
    session.expire_all()
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.entity_id.like(f"je:auto:{wo}:cost:%")))).scalars().all()
    assert rows, "the correction posted no entry under its scope"
    for row in rows:
        # 2.8.0 wrote the scope as the entry's list_id; its entry id is that scope, kind and cycle.
        scope = row.entity_id.removeprefix("je:auto:").removesuffix(":writeoff_cost:0")
        row.metadata_ = {**(row.metadata_ or {}), "list_id": scope}
    await session.commit()

    gl = await _gl(client, h, wo)
    assert gl["doc_id"] == wo
    assert len(gl["entries"]) == 2, [e["je_id"] for e in gl["entries"]]
    assert {e["je_id"] for e in gl["entries"]} >= {r.entity_id for r in rows}
    expense = [l["debit"] for e in gl["entries"] for l in e["lines"] if l["account"] == "6950"]
    assert correction in expense, expense


async def test_a_list_whose_number_holds_cost_keeps_its_own_postings(client, session):
    """A List's number is free text. A write-off numbered "WO:cost:ABC-..." is that List exactly: its
    postings show on its own GL Impact and in a party-filtered ledger, never on the List numbered
    "WO" that names a customer."""
    auth, wo, correction = await _written_off_then_corrected(client, session, "WO:cost:ABC")
    h = auth["headers"]
    assert wo.startswith("list:WO:cost:ABC"), wo
    contacts = (await client.get("/crm/contacts?limit=500", headers=h)).json()
    contacts = contacts.get("items", contacts) if isinstance(contacts, dict) else contacts
    customer = next(c.get("entity_id") or c.get("id") for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "ref_id": "WO", "contact_id": customer,
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 100}]})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "list:WO"

    gl = await _gl(client, h, wo)
    assert len(gl["entries"]) == 2, [e["je_id"] for e in gl["entries"]]
    expense = [l["debit"] for e in gl["entries"] for l in e["lines"] if l["account"] == "6950"]
    assert correction in expense, expense
    assert (await _gl(client, h, "list:WO"))["entries"] == []

    ledger = await client.get("/accounting/ledger/6950", headers=h, params={"contact_id": customer})
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["lines"] == [], ledger.json()["lines"]
    unattributed = await client.get("/accounting/ledger/6950", headers=h, params={"contact_id": ""})
    assert correction in [l["debit"] for l in unattributed.json()["lines"]], unattributed.json()["lines"]


async def test_a_purchase_order_received_in_parts_names_the_items_of_each_receipt(client, session):
    from test_tax_acceptance_matrix import _contact, _demo_company

    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={
        "doc_type": "purchase_order", "contact_id": _contact(contacts, "purchase_order"),
        "line_items": [{"sku": "PO-A", "name": "PO-A", "quantity": 4, "unit_price": 25.0},
                       {"sku": "PO-B", "name": "PO-B", "quantity": 2, "unit_price": 10.0}]})
    assert r.status_code == 200, r.text
    po = r.json()["id"]
    loc = (await client.post("/companies/me/locations", headers=h,
                             json={"name": "Receiving", "type": "warehouse"})).json()["id"]
    for received in ([(0, "PO-A", 1)], [(0, "PO-A", 3), (1, "PO-B", 2)]):
        r = await client.post(f"/docs/{po}/receive", headers=h, json={"location_id": loc, "received_items": [
            {"po_line_index": i, "sku": sku, "name": sku, "quantity_received": q, "receive_as": "stock"}
            for i, sku, q in received]})
        assert r.status_code == 200, r.text
    gl = await _gl(client, h, po)
    receipts = sorted(([(l["item"], l["quantity"], l["debit"]) for l in e["lines"] if l.get("item")], e["items_status"])
                      for e in gl["entries"])
    assert receipts == [
        ([("PO-A", 1, 25.0)], "expanded"),
        ([("PO-A", 3, 75.0), ("PO-B", 2, 20.0)], "expanded"),
    ], receipts


if __name__ == "__main__":
    for row in census():
        print("\t".join(map(str, row)))
