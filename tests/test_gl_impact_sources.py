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
from celerp.services.ledger_sources import SOURCE_KEYS

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
            if not any(k in text for k in SOURCE_KEYS):
                continue
            rel = str(path.relative_to(REPO))
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.Call):
                    continue
                name = _callee(node)
                for kw in node.keywords:
                    if kw.arg in ("metadata_", "metadata") and isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and k.value in SOURCE_KEYS:
                                rows.append((rel, k.lineno, f"{name}(metadata)", k.value, ast.unparse(v)))
                    elif kw.arg in SOURCE_KEYS and name in posters:
                        rows.append((rel, kw.value.lineno, name, kw.arg, ast.unparse(kw.value)))
    return rows


def violations() -> list[tuple[str, int, str, str, str]]:
    return [r for r in census() if r[4].startswith(("f'", 'f"')) or "+" in r[4]]


def test_every_posting_names_its_source_by_the_exact_record_id():
    assert census(), "the census found no doors; its walk is broken"
    assert violations() == []


# Which entries a record posted is read from their metadata (ledger_sources), never from entry-id
# text: one List's number can begin with another's. Every other place that matches entry-id text
# is listed here with why it is not deciding what a List posted. A Document's id begins "doc:", so
# a match on a Document's own id prefix never reaches a List's entries.
_ENTRY_ID_TEXT_ALLOWED = {
    ("celerp/services/ledger_sources.py", "<module>"): "the 2.8.0 cost-correction shape, read back in source_meta",
    ("celerp/services/ledger_sources.py", "linked_events"): "a superset of candidates; names_record decides",
    ("celerp/services/auto_je.py", "list_adjustment_scope"): "the scope of an entry already selected by the rule",
    ("celerp/services/auto_je.py", "landed_clearing"): "a Document's landed-cost entries",
    ("celerp/services/auto_je.py", "void_landed_capitalisation"): "a Document's landed-cost entries",
    ("celerp/services/auto_je.py", "reverse_return_charges"): "a Document's return entries",
    ("celerp/services/auto_je.py", "restore_return_charges"): "a Document's return entries",
    ("celerp/services/auto_je.py", "_finalize_doc"): "a Document's finalize entries",
    ("celerp/services/auto_je.py", "_recognitions"): "a Document's finalize entries",
    ("celerp/services/auto_je.py", "_open_invoices"): "a Document's finalize entries",
    ("celerp/services/cogs_backfill.py", "_je_doc_id"): "a Document's entries, checked against real Documents",
    ("default_modules/celerp-admin/celerp_admin/routes.py", "_check_posting_origins"): "every automatic entry, no record",
    ("default_modules/celerp-docs/celerp_docs/legacy_credit_notes.py", "_posted_entry"): "a credit note's entries",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "unvoid_doc"): "a Document's entries",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "_posted_journal_entries"): "a Document's entries",
    ("default_modules/celerp-docs/celerp_docs/routes.py", "delete_payment"): "a Document's payment entries",
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/sales.py", "_cleared"): "a Document's entries",
    ("default_modules/celerp-inventory/celerp_inventory/routes.py", "undo_import_batch"): "an opening-stock batch's entries",
    ("ui/components/activity.py", "entity_url"): "a display link, moves no money",
}
_ENTRY_ID_MARKERS = ("je:auto:", ":cost:", ":writeoff")
_TEXT_MATCHES = {"startswith", "endswith", "like", "ilike", "notlike", "removeprefix", "removesuffix", "split",
                 "rsplit", "partition", "rpartition", "find", "rfind", "index", "match", "fullmatch", "search",
                 "compile", "sub"}


def entry_id_text_matches() -> set[tuple[str, str]]:
    """(file, function) for every text match whose call carries an entry-id marker."""
    found = set()

    def visit(node, rel, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in _TEXT_MATCHES
                    and any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and any(m in n.value for m in _ENTRY_ID_MARKERS) for n in ast.walk(child))):
                found.add((rel, inner))
            visit(child, rel, inner)

    for root in (*_ROOTS, "ui"):
        for path in sorted((REPO / root).rglob("*.py")):
            if "/tests/" in str(path) or "/migrations/" in str(path):
                continue
            text = path.read_text()
            if any(m in text for m in _ENTRY_ID_MARKERS):
                visit(ast.parse(text), str(path.relative_to(REPO)), "<module>")
    return found


def test_no_entry_id_text_decides_what_a_list_posted():
    assert entry_id_text_matches() == set(_ENTRY_ID_TEXT_ALLOWED)


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
             if k in SOURCE_KEYS and v}
    known = set((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_id.in_(sorted({v for _, _, v in named}))))).scalars().all())
    return sorted(row for row in named if row[2] not in known)


async def _stock(client, h) -> list[dict]:
    """The demo items with stock on hand at a cost."""
    items = (await client.get("/items?limit=50", headers=h)).json()
    items = items.get("items", items) if isinstance(items, dict) else items
    return [i for i in items if float(i.get("quantity") or 0) > 0 and float(i.get("cost_total") or 0) > 0]


async def _write_off(client, h, item, prefix=None) -> str:
    """``item`` written off in full to 6950 on a new write-off List, numbered under ``prefix`` when given."""
    if prefix is not None:
        r = await client.patch("/docs/sequences/writeoff", headers=h, json={"prefix": prefix})
        assert r.status_code == 200, r.text
    eid = item.get("entity_id") or item.get("id")
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [eid]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    line = next(l for l in (await client.get(f"/lists/{wo}", headers=h)).json()["line_items"] if l["item_id"] == eid)
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h,
                          json={"line_id": line["line_id"], "qty_out": float(item["quantity"]), "account": "6950"})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/lists/{wo}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{wo}/write-off", headers=h)).status_code == 200
    return wo


async def _correct_cost(client, h, item) -> float:
    """Raise ``item``'s cost by half; returns the correction a write-off of it posts."""
    cost = float(item["cost_total"])
    r = await client.patch(f"/items/{item.get('entity_id') or item.get('id')}", headers=h,
                           json={"fields_changed": {"cost_total": {"old": cost, "new": round(cost * 1.5, 2)}}})
    assert r.status_code == 200, r.text
    return round(cost * 0.5, 2)


async def _written_off_then_corrected(client, session, prefix=None):
    """A demo item written off in full on a write-off List (numbered under ``prefix`` when given),
    then the disposed lot's cost corrected: the correction posts its own entry to the List's
    expense account."""
    from test_tax_acceptance_matrix import _demo_company

    auth, _contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    item = (await _stock(client, h))[0]
    wo = await _write_off(client, h, item, prefix)
    return auth, wo, await _correct_cost(client, h, item)


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

    # Its own List's undo takes the 2.8.0 correction back with the write-off.
    assert (await client.post(f"/lists/{wo}/undo-write-off", headers=h)).status_code == 200
    assert _statuses(await _gl(client, h, wo)) == dict.fromkeys(_statuses(gl), "void")


def _statuses(gl) -> dict[str, str]:
    return {e["je_id"]: e["status"] for e in gl["entries"]}


@pytest.mark.parametrize("prefix", ["K", "WO"])
async def test_undoing_a_write_off_leaves_another_lists_corrections(client, session, prefix):
    """List B's number is List A's number followed by ":cost:" (numbers are free text). Both are
    written off and both items' costs corrected. Undoing A voids A's own entries and nothing of B's:
    B's correction stays posted and B's GL Impact is unchanged."""
    from test_tax_acceptance_matrix import _demo_company

    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    a_item, b_item = (await _stock(client, h))[:2]
    a = await _write_off(client, h, a_item, prefix)
    b = await _write_off(client, h, b_item, f"{a.removeprefix('list:')}:cost:Z")
    assert b.startswith(f"{a}:cost:Z"), b
    await _correct_cost(client, h, a_item)
    await _correct_cost(client, h, b_item)
    a_before, b_before = _statuses(await _gl(client, h, a)), _statuses(await _gl(client, h, b))
    assert len(a_before) == len(b_before) == 2, (a_before, b_before)
    assert set(b_before.values()) == {"posted"}, b_before

    assert (await client.post(f"/lists/{a}/undo-write-off", headers=h)).status_code == 200
    assert _statuses(await _gl(client, h, a)) == dict.fromkeys(a_before, "void")
    assert _statuses(await _gl(client, h, b)) == b_before
    session.expire_all()
    voided = (await session.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.event_type == "acc.journal_entry.voided"))).scalars().all()
    assert sorted(voided) == sorted(a_before), voided


async def test_a_list_numbered_like_a_2_8_0_scope_keeps_its_own_postings(client, session):
    """A write-off numbered like a 2.8.0 cost scope (":cost:", 24 hex, ":") is not one: only an entry
    whose id is that scope's own correction id is read back, so this List keeps its postings and the
    List numbered by the part before the marker shows none of them."""
    prefix = "WO:cost:0123456789abcdef01234567:X"
    auth, wo, correction = await _written_off_then_corrected(client, session, prefix)
    h = auth["headers"]
    assert wo.startswith(f"list:{prefix}"), wo
    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "ref_id": "WO",
                                                     "line_items": [{"name": "A", "quantity": 1, "unit_price": 100}]})
    assert r.status_code == 200 and r.json()["id"] == "list:WO", r.text
    gl = await _gl(client, h, wo)
    assert len(gl["entries"]) == 2, [e["je_id"] for e in gl["entries"]]
    assert correction in [l["debit"] for e in gl["entries"] for l in e["lines"] if l["account"] == "6950"]
    assert (await _gl(client, h, "list:WO"))["entries"] == []


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
