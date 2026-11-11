# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A record made from another record (a List duplicated or converted, a quotation converted, a
consignment bought) is built by one rule (record_copy.copy_of): the source's parties and its lines,
as a new draft; a conversion also keeps the money agreed and its term (record_copy.conversion_of,
test_record_conversion). Its status, outcome, sending, numbering, links and scans stay with the source, and so
does what each line records of its lifecycle (test_record_copy_lines).

The guard comes from the code: every door that creates a document or list either builds it through
copy_of (or conversion_of) or names every key it writes."""
from __future__ import annotations

import ast
from pathlib import Path

from test_tax_acceptance_matrix import _demo_company
from shipped_sources import APP_ROOTS

ROOT = Path(__file__).resolve().parent.parent
CREATED = {"doc.created", "list.created"}


def app_sources():
    for top in APP_ROOTS:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT)
            if "tests" in rel.parts or path.name.startswith(("test_", "conftest")):
                continue
            yield rel, path


def _creation_constants() -> set[str]:
    """Names the app binds to a creation event (``DOC_CREATED = "doc.created"``), so a door that
    passes the constant is found as surely as one that writes the string."""
    return {t.id for _, path in app_sources() for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value in CREATED
            for t in node.targets if isinstance(t, ast.Name)}


def _creates(fn: ast.AST, constants: set[str]) -> bool:
    """The function writes a new document or list: it passes a creation event to a call (an emit,
    an upsert, an import row), by its name or by a constant bound to it, or calls the document
    create service. A projection handler only compares against the event's name, so it is not a door."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", getattr(node.func, "attr", "")) == "create_doc_record":
            return True
        if any((isinstance(a, ast.Constant) and a.value in CREATED) or getattr(a, "id", getattr(a, "attr", None)) in constants
               for a in [*node.args, *(k.value for k in node.keywords)]):
            return True
    return False


def _state_like(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and "state" in node.id) or (isinstance(node, ast.Attribute) and node.attr == "state")


def _wholesale(fn: ast.AST) -> bool:
    """The function copies a source's keys without naming them: ``{k: v for k, v in x.items() if k
    not in ...}`` (a denylist) or ``{**state, ...}``."""
    for node in ast.walk(fn):
        if isinstance(node, ast.DictComp):
            for gen in node.generators:
                if (isinstance(gen.iter, ast.Call) and getattr(gen.iter.func, "attr", "") == "items"
                        and any(isinstance(op, ast.NotIn) for cond in gen.ifs for c in ast.walk(cond)
                                if isinstance(c, ast.Compare) for op in c.ops)):
                    return True
        if isinstance(node, ast.Dict) and any(k is None and _state_like(v) for k, v in zip(node.keys, node.values)):
            return True
    return False


def copy_doors() -> list[tuple[str, str, str]]:
    """Every function that creates a document or list, with how it builds it: ``conversion_of`` or
    ``copy_of`` (through record_copy), ``wholesale`` (copies unnamed keys of a source by its own rule) or ``named``
    (every key it writes is named: a payload, a literal or a fixed field set)."""
    rows, constants = [], _creation_constants()
    for rel, path in app_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _creates(fn, constants):
                continue
            calls = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(fn) if isinstance(n, ast.Call)}
            how = next((f for f in ("conversion_of", "copy_of") if f in calls), None) or (
                "wholesale" if _wholesale(fn) else "named")
            rows.append((str(rel), fn.name, how))
    return rows


def test_every_door_that_creates_a_record_names_what_it_takes():
    doors = copy_doors()
    assert [d for d in doors if d[2] == "wholesale"] == []
    assert {name for _, name, how in doors if how == "conversion_of"} == {"convert_list", "convert_doc", "buy_consignment"}, doors
    assert "duplicate_list" in {name for _, name, how in doors if how == "copy_of"}, doors


def test_the_census_finds_a_door_that_names_the_creation_event_by_a_constant():
    names = {name for _, name, _ in copy_doors()}
    assert {"create_imported_doc", "_doc_record"} <= names


async def _scenario(client, session):
    """A quotation List finalized, marked as sent and converted to an invoice, then duplicated."""
    auth, contacts = await _demo_company(client, session, "USD")
    h = auth["headers"]
    cust = next(c for c in contacts if c.get("contact_type") in ("customer", "both"))
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cust["id"], "payment_terms": "Net 30", "notes": "Kept",
        "customer_note": "For you", "discount": 5, "tax": 7,
        "line_items": [{"name": "A", "quantity": 2, "unit_price": 50}]})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    assert (await client.post(f"/lists/{lid}/finalize", headers=h)).status_code == 200
    assert (await client.post(f"/lists/{lid}/send", headers=h, json={"sent_via": "manual"})).status_code == 200
    assert (await client.post(f"/docs/{lid}/share", headers=h)).status_code == 200
    r = await client.post(f"/lists/{lid}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{lid}/duplicate", headers=h)
    assert r.status_code == 200, r.text
    return auth, contacts, lid, r.json()["id"]


async def test_a_duplicate_of_a_converted_sent_list_is_a_fresh_draft_that_converts(client, session):
    auth, _, lid, dup = await _scenario(client, session)
    h = auth["headers"]
    src = (await client.get(f"/lists/{lid}", headers=h)).json()
    copy = (await client.get(f"/lists/{dup}", headers=h)).json()
    for key in ("result", "converted_to", "converted_to_type", "sent_at", "sent_via", "sent_to", "finalized_at",
                "share_token"):
        assert not copy.get(key), (key, copy.get(key))
    assert copy["status"] == "draft" and copy["source_list_id"] == lid
    for key in ("contact_id", "contact_name", "list_type", "payment_terms"):
        assert copy.get(key) == src.get(key), key
    for key in ("notes", "customer_note", "discount", "tax"):  # for the user to fill on the copy
        assert src.get(key) and not copy.get(key), (key, copy.get(key))
    assert [li["name"] for li in copy["line_items"]] == ["A"]
    # The draft copy is refused for not being finalized, never as "already converted".
    r = await client.post(f"/lists/{dup}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "documents.err_list_convert_status", r.text
    assert (await client.post(f"/lists/{dup}/finalize", headers=h)).status_code == 200
    from celerp.services.list_behavior import list_fact

    assert not list_fact((await client.get(f"/lists/{dup}", headers=h)).json(), "sent_at")
    assert (await client.post(f"/lists/{dup}/send", headers=h, json={"sent_via": "manual"})).status_code == 200
    r = await client.post(f"/lists/{dup}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    inv = (await client.get(f"/docs/{r.json()['target_doc_id']}", headers=h)).json()
    for key in ("converted_to", "converted_to_type", "sent_at", "sent_via", "result", "finalized_at"):
        assert not inv.get(key), (key, inv.get(key))
    assert inv["source_list_id"] == dup and inv["status"] == "draft"


def test_a_list_names_its_conversion_and_sending_only_beside_the_status_that_makes_them_true():
    from celerp.services.list_behavior import list_fact

    copied = {"result": "converted", "converted_to": "doc:INV-1", "sent_at": "2026-11-01T00:00:00+00:00"}
    assert list_fact({**copied, "status": "draft"}, "converted_to") is None
    assert list_fact({**copied, "status": "finalized"}, "converted_to") is None
    assert list_fact({**copied, "status": "draft"}, "sent_at") is None
    assert list_fact({**copied, "status": "closed"}, "converted_to") == "doc:INV-1"
    assert list_fact({**copied, "status": "finalized"}, "sent_at") == copied["sent_at"]
    assert list_fact({**copied, "status": "closed", "result": "accepted"}, "converted_to") is None


if __name__ == "__main__":  # the census: python tests/test_record_copy.py
    for row in copy_doors():
        print("\t".join(row))
