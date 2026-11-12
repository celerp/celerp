# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Lines that came from outside the company: the marker an arrival writer puts on a record
and its lines, and the one line-key rule every later write of those lines meets.

Initial conditions per test: a fresh company with the seeded chart and no funds (funds were
never turned on), service items made through POST /items. Outside records arrive through the
document, List and journal imports (origin ``file_import``); local writes go through
POST /docs, PATCH /docs/{id}, and the List duplicate and convert routes. In a company
without funds an inbound ``fund_choice`` or ``fund_id`` is stored as sent, so the keys
exist to be kept, refused or copied.
"""
from __future__ import annotations

import ast
import re
import uuid

import pytest
from fasthtml.common import to_xml

from celerp.models.projections import Projection
from test_income_accounts import _finalize_entry, _issued, _item, _line, ui_client  # noqa: F401 (fixture)
from test_credit_note_settlement_owned import _post

pytestmark = pytest.mark.asyncio

ORIGIN = "file_import"


def _key(r) -> str:
    return r.json()["detail"]["message_key"]


async def _doc(client, auth, doc: str) -> dict:
    r = await client.get(f"/docs/{doc}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _imported(client, auth, lines: list[dict], **header) -> str:
    """A draft invoice brought in by the document import."""
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1], "line_items": lines,
                 "total": round(sum(li["line_total"] for li in lines), 2), **header}})
    assert r.status_code == 200, r.text
    return eid


async def _patch(client, auth, doc: str, **fields):
    version = (await _doc(client, auth, doc))["version"]
    return await client.patch(f"/docs/{doc}", headers=auth["headers"], json={
        "expected_version": version, "fields_changed": {k: {"new": v} for k, v in fields.items()}})


def _as_sent(line: dict, *, keep_id: bool = True, **change) -> dict:
    """A stored line as a writer that does not know the fund keys sends it back."""
    sent = {k: line[k] for k in ("item_id", "name", "quantity", "unit_price", "line_total") if k in line}
    if keep_id:
        sent["line_id"] = line["line_id"]
    return {**sent, **change}


async def _two_line_import(client, auth, **first) -> tuple[str, str, str]:
    a, b = await _item(client, auth, "Outside A"), await _item(client, auth, "Outside B")
    return await _imported(client, auth, [_line(a, "Outside A", 40, **first), _line(b, "Outside B", 60)]), a, b


async def _entry_state(session, company_id, entity_id) -> dict:
    session.expire_all()
    row = await session.get(Projection, {"company_id": company_id, "entity_id": entity_id})
    assert row is not None
    return row.state


def _amounts(entries: list[dict]) -> list[tuple]:
    return sorted((e["account"], float(e.get("debit") or 0), float(e.get("credit") or 0)) for e in entries)


# ── the record link ────────────────────────────────────────────────────────────────────

def _auto_posting_calls():
    from fund_census import sources

    for module, path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "_emit_auto_posted_je":
                    yield module, node


def _posted_from_writers() -> set[tuple[str, str]]:
    """(module, innermost function) of every write of ``posted_from`` in app code: a dict key,
    a keyword or an item assignment (a read such as ``line.get("posted_from")`` is none)."""
    from fund_census import sources

    found = set()
    for module, path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                here = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                written = (isinstance(child, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "posted_from" for k in child.keys)) or (
                    isinstance(child, ast.keyword) and child.arg == "posted_from") or (
                    isinstance(child, ast.Subscript) and isinstance(child.ctx, ast.Store)
                    and isinstance(child.slice, ast.Constant) and child.slice.value == "posted_from")
                if written:
                    found.add((module, here))
                walk(child, here)

        walk(tree, "<module>")
    return found


async def test_every_auto_posting_names_its_record(client, session, auth):
    calls = list(_auto_posting_calls())
    assert len(calls) > 30, "the census found too few auto postings to be reading the source"
    missing = [(m, c.lineno) for m, c in calls if not any(kw.arg == "record_id" for kw in c.keywords)]
    assert missing == [], f"auto postings that do not name their record: {missing}"
    assert {f for _, f in _posted_from_writers()} <= {"copy_posted_lines", "copy_fund_split"}, _posted_from_writers()

    item = await _item(client, auth, "Linked")
    doc = await _issued(client, auth, "invoice", [_line(item, "Linked", 25)])
    assert (await _finalize_entry(session, auth, doc)).get("record_id") == doc


# ── the marker and the line-key rule ───────────────────────────────────────────────────

async def test_marker_survives_every_line_write(client, auth):
    doc, a, _ = await _two_line_import(client, auth, fund_choice="unrestricted", fund_id="fund:sent")
    stored = (await _doc(client, auth, doc))["line_items"]
    assert [li.get("origin") for li in stored] == [ORIGIN, ORIGIN]
    first, second = stored

    # A whole-line write that does not know the keys keeps them (LK1).
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, name="Outside A renamed"), _as_sent(second)])
    assert r.status_code == 200, r.text
    kept = (await _doc(client, auth, doc))["line_items"]
    assert kept[0]["name"] == "Outside A renamed"
    assert {k: kept[0].get(k) for k in ("origin", "fund_choice", "fund_id")} == {
        "origin": ORIGIN, "fund_choice": "unrestricted", "fund_id": "fund:sent"}
    assert kept[1].get("origin") == ORIGIN

    # A local write cannot change where a line came from (LK2) ...
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, origin="migration"), _as_sent(second)])
    assert r.status_code == 409 and _key(r) == "posting.fund.origin_fixed", r.text
    # ... nor put an origin on a line it adds ...
    r = await _patch(client, auth, doc, line_items=[_as_sent(first), _as_sent(second),
                                                    _line(None, "Local", 5, origin=ORIGIN)])
    assert r.status_code == 422 and _key(r) == "posting.fund.key_not_taken", r.text
    # ... nor change the fund chosen for it without a grant (LK3).
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, fund_choice="fund:other"), _as_sent(second)])
    assert r.status_code == 409 and _key(r) == "posting.fund.choice_not_granted", r.text

    # Deleting a line is local authoring; the rest keep their marker.
    r = await _patch(client, auth, doc, line_items=[_as_sent(first)])
    assert r.status_code == 200, r.text
    assert [li.get("origin") for li in (await _doc(client, auth, doc))["line_items"]] == [ORIGIN]

    # A copy keeps the marker and the choice on every line it copies.
    eid = f"list:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/lists/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "list.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"list_type": "quotation", "status": "draft",
                 "line_items": [_line(a, "Outside A", 40, fund_choice="unrestricted")]}})
    assert r.status_code == 200, r.text
    source = (await client.get(f"/lists/{eid}", headers=auth["headers"])).json()
    assert [li.get("origin") for li in source["line_items"]] == [ORIGIN]
    assert source.get("origin") == ORIGIN

    r = await client.post(f"/lists/{eid}/duplicate", headers=auth["headers"])
    assert r.status_code == 200, r.text
    copy = (await client.get(f"/lists/{r.json()['id']}", headers=auth["headers"])).json()
    assert [(li.get("origin"), li.get("fund_choice")) for li in copy["line_items"]] == [(ORIGIN, "unrestricted")]
    assert copy["line_items"][0]["line_id"] != source["line_items"][0]["line_id"]
    assert copy.get("origin") == ORIGIN

    r = await client.post(f"/lists/{eid}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{eid}/convert", headers=auth["headers"], json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    body = r.json()
    invoice = await _doc(client, auth, body.get("target_doc_id") or body.get("id"))
    assert [(li.get("origin"), li.get("fund_choice")) for li in invoice["line_items"]] == [(ORIGIN, "unrestricted")]
    assert invoice.get("origin") == ORIGIN


async def test_header_fund_keys_follow_line_key_rule(client, auth):
    item = await _item(client, auth, "Header")
    # Without funds in use, inbound header keys are stored as sent and the arrival marker is
    # written only where no origin arrived (LK7).
    doc = await _imported(client, auth, [_line(item, "Header", 30)],
                          fund_choice="unrestricted", fund_choice_source="user")
    sent = await _imported(client, auth, [_line(item, "Header", 30)], origin="accountant")
    state = await _doc(client, auth, doc)
    assert (state.get("origin"), state.get("fund_choice"), state.get("fund_choice_source")) == (
        ORIGIN, "unrestricted", "user")
    assert (await _doc(client, auth, sent)).get("origin") == "accountant"

    # Omitted, they keep the stored value (LK1).
    r = await _patch(client, auth, doc, notes="Checked")
    assert r.status_code == 200, r.text
    state = await _doc(client, auth, doc)
    assert (state.get("origin"), state.get("fund_choice"), state.get("fund_choice_source")) == (
        ORIGIN, "unrestricted", "user")

    # A local write changes neither the record's origin (LK2) nor its fund without a grant (LK3).
    r = await _patch(client, auth, doc, origin="migration")
    assert r.status_code == 409 and _key(r) == "posting.fund.document_origin_fixed", r.text
    r = await _patch(client, auth, doc, fund_choice="fund:other")
    assert r.status_code == 409 and _key(r) == "posting.fund.document_choice_not_granted", r.text

    # A locally created record carries no origin, and a local write cannot give it one.
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [_line(item, "Header", 30)], "total": 30, "origin": ORIGIN})
    assert r.status_code == 422 and _key(r) == "posting.fund.key_not_taken", r.text
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [_line(item, "Header", 30)], "total": 30})
    assert r.status_code == 200, r.text
    assert "origin" not in await _doc(client, auth, r.json()["id"])


async def test_api_line_without_line_id_matched_or_local(client, auth):
    doc, a, _ = await _two_line_import(client, auth, fund_choice="unrestricted")
    first, second = (await _doc(client, auth, doc))["line_items"]

    # The first line is matched by position and item; the second is replaced by a local line.
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, keep_id=False), _line(None, "Local", 5)])
    assert r.status_code == 200, r.text
    matched, local = (await _doc(client, auth, doc))["line_items"]
    assert matched["line_id"] == first["line_id"]
    assert (matched.get("origin"), matched.get("fund_choice")) == (ORIGIN, "unrestricted")
    assert local["line_id"] not in {first["line_id"], second["line_id"]}
    assert "origin" not in local and "fund_choice" not in local


async def test_editor_sends_line_id_for_kept_lines(client, auth):
    """The line editor renders each stored line's id and posts it back with the row, so a
    marked line saved through the editor is a kept line, never a new one."""
    from ui.routes import documents

    doc, _, _ = await _two_line_import(client, auth)
    state = await _doc(client, auth, doc)
    page = to_xml(documents._doc_detail(state))
    for line in state["line_items"]:
        assert f'value="{line["line_id"]}" data-name="line_id"' in page or (
            f'data-name="line_id" value="{line["line_id"]}"' in page), line["line_id"]
    assert "line_id: _celerpRowLineId(row)" in page


# ── companies without funds ────────────────────────────────────────────────────────────

async def test_no_funds_company_outside_lines_unchanged(client, session, auth):
    """Golden: an outside record in a company that never used funds posts exactly as a local
    one; its lines additionally carry where they came from."""
    item = await _item(client, auth, "Golden")
    imported = await _imported(client, auth, [_line(item, "Golden", 70)])
    r = await _post(client, auth, f"/docs/{imported}/finalize")
    assert r.status_code == 200, r.text
    local = await _issued(client, auth, "invoice", [_line(item, "Golden", 70)])
    outside, own = await _finalize_entry(session, auth, imported), await _finalize_entry(session, auth, local)
    assert _amounts(outside["entries"]) == _amounts(own["entries"])
    state = await _doc(client, auth, imported)
    assert state["status"] != "draft" and state.get("origin") == ORIGIN
    assert [li.get("origin") for li in state["line_items"]] == [ORIGIN]

    # A header-only record from the batch import posts as before and carries the marker.
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1], "line_items": []}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert (await _doc(client, auth, eid)).get("origin") == ORIGIN

    # A journal import posts its lines as sent, the entry and every line marked.
    je = f"je:{uuid.uuid4()}"
    entries = [{"account": "1111", "debit": 12.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 12.0}]
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": "2026-01-15", "entries": entries}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    entry = await _entry_state(session, auth["company_id"], je)
    assert entry["status"] == "posted" and _amounts(entry["entries"]) == _amounts(entries)
    assert entry.get("origin") == ORIGIN and [e.get("origin") for e in entry["entries"]] == [ORIGIN, ORIGIN]


# ── fund choices ───────────────────────────────────────────────────────────────────────

async def _fund_company(client, auth) -> str:
    """Funds on, with one unrestricted fund; returns its id."""
    from test_funds import _fund, _funds_on

    r = await _funds_on(client, auth)
    assert r.status_code == 200, r.text
    return await _fund(client, auth, "General", restricted=False, purpose=None)


async def _choose(client, auth, doc: str, **body):
    version = (await _doc(client, auth, doc))["version"]
    return await client.post(f"/docs/{doc}/fund-choices", headers=auth["headers"],
                             json={"expected_version": version, **body})


def _choices(state: dict) -> tuple:
    return state.get("fund_choice"), [(li.get("fund_choice"), li.get("fund_id")) for li in state["line_items"]]


async def test_fund_choice_grant_built_only_in_peer_lines(client, auth):
    from fund_census import sources

    builders = set()
    for module, path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                here = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(child, ast.Call) and getattr(child.func, "id", getattr(child.func, "attr", None)) == "FundChoiceGrant":
                    builders.add((module, here))
                walk(child, here)

        walk(tree, "<module>")
    assert builders == {("celerp.services.peer_lines", "require_fund_choices"),
                        ("celerp.services.peer_lines", "granted_from_stored")}, builders

    # A write that sets a different choice without a grant is refused; the Fund control,
    # which builds one, sets it.
    fund = await _fund_company(client, auth)
    doc, _, _ = await _two_line_import(client, auth)
    first, second = (await _doc(client, auth, doc))["line_items"]
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, fund_choice=fund, fund_id=fund), _as_sent(second)])
    assert r.status_code == 409 and _key(r) == "posting.fund.choice_not_granted", r.text
    r = await _choose(client, auth, doc, lines={first["line_id"]: fund})
    assert r.status_code == 200, r.text
    assert _choices(await _doc(client, auth, doc)) == (None, [(fund, fund), (None, None)])


async def test_local_edit_of_marked_line_clears_choices_with_notice(client, auth, ui_client):
    from unittest.mock import AsyncMock

    from test_income_accounts import _ui_api

    fund = await _fund_company(client, auth)
    doc, _, _ = await _two_line_import(client, auth)
    first, second = (await _doc(client, auth, doc))["line_items"]
    chosen = ("unrestricted", [(fund, fund), ("unrestricted", None)])
    r = await _choose(client, auth, doc, document="unrestricted", lines={first["line_id"]: fund, second["line_id"]: "unrestricted"})
    assert r.status_code == 200, r.text
    assert _choices(await _doc(client, auth, doc)) == chosen

    # A description-only edit through the line editor keeps every choice.
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, name="Outside A, renamed"), _as_sent(second)])
    assert r.status_code == 200 and not r.json().get("fund_choices_cleared"), r.text
    assert _choices(await _doc(client, auth, doc)) == chosen

    # A unit price change on one marked line clears every choice on the document in that write.
    r = await _patch(client, auth, doc, line_items=[_as_sent(first, unit_price=45, line_total=45), _as_sent(second)])
    assert r.status_code == 200, r.text
    assert r.json()["fund_choices_cleared"] == {"lines": [1, 2], "document": True}
    state = await _doc(client, auth, doc)
    assert _choices(state) == (None, [(None, None), (None, None)])
    assert state["line_items"][0]["unit_price"] == 45 and state["line_items"][0]["origin"] == ORIGIN

    # A document discount change clears them too.
    r = await _choose(client, auth, doc, lines={first["line_id"]: fund})
    assert r.status_code == 200, r.text
    r = await _patch(client, auth, doc, discount=5, discount_type="flat")
    assert r.status_code == 200, r.text
    assert r.json()["fund_choices_cleared"] == {"lines": [1], "document": False}
    assert _choices(await _doc(client, auth, doc)) == (None, [(None, None), (None, None)])

    # The line editor shows the notice beside the cells, naming the lines.
    patch_doc = AsyncMock(return_value={"version": 9, "fund_choices_cleared": {"lines": [1, 2], "document": True}})
    with _ui_api(patch_doc=patch_doc, get_doc=AsyncMock(return_value=state)):
        r = await ui_client.post(f"/docs/{doc}/lines", json={"line_items": state["line_items"], "expected_version": 3})
    assert r.status_code == 200, r.text
    assert r.json()["fund_notice"] == (
        "Amounts changed, so the fund choices on lines 1, 2 and the fund for this document were cleared: choose again")


async def test_document_import_refuses_line_fund_id(client, auth):
    """In books that use funds, a batch import refuses each record whose file line carries
    fund_id, fund_choice or origin, for that record only, pointing to the import screen; the
    record beside them is created."""
    await _fund_company(client, auth)
    item = await _item(client, auth, "Filed")
    keys = (("fund_id", "fund:elsewhere"), ("fund_choice", "unrestricted"), ("origin", "accountant"), (None, None))
    eids = [f"doc:IMP-{uuid.uuid4().hex[:8]}" for _ in keys]
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1], "total": 15,
                 "line_items": [_line(item, "Filed", 10), _line(item, "Filed", 5, **({key: value} if key else {}))]}}
        for eid, (key, value) in zip(eids, keys)]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    errors = r.json()["errors"]
    for eid, (key, _) in zip(eids[:3], keys):
        assert f"{eid}: The file gives {key}. Choose funds on the import screen, not in the file." in errors, errors
    assert [(await client.get(f"/docs/{eid}", headers=auth["headers"])).status_code for eid in eids] == [404] * 3 + [200]


async def test_reserved_keys_kept_without_funds_and_checked_at_turn_on(client, session, auth):
    from test_funds import _funds_on

    item = await _item(client, auth, "Reserved")
    # Without funds in use, inbound keys are stored as sent: an import line's origin, a line's
    # fund and choice, and a journal line's fund.
    marked = await _imported(client, auth, [_line(item, "Reserved", 10, origin="accountant")])
    carried = await _imported(client, auth, [_line(item, "Reserved", 20, fund_choice="fund:elsewhere",
                                                   fund_id="fund:elsewhere")])
    assert (await _doc(client, auth, marked))["line_items"][0]["origin"] == "accountant"
    assert _choices(await _doc(client, auth, carried))[1] == [("fund:elsewhere", "fund:elsewhere")]
    je = f"je:{uuid.uuid4()}"
    entries = [{"account": "1111", "debit": 8.0, "credit": 0, "fund_id": "fund:elsewhere"},
               {"account": "4100", "debit": 0, "credit": 8.0, "fund_id": "fund:elsewhere"}]
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": "2026-01-15", "entries": entries}}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert [e.get("fund_id") for e in (await _entry_state(session, auth["company_id"], je))["entries"]] == [
        "fund:elsewhere", "fund:elsewhere"]

    # Turning funds on is refused while an unposted record carries a fund key, naming it; a
    # record that carries only its origin never blocks.
    r = await _funds_on(client, auth)
    assert r.status_code == 422 and _key(r) == "fund.turn_on_outside_keys", r.text
    params = r.json()["detail"]["params"]
    assert params["count"] == 1 and params["records"] == carried.split(":", 1)[1], params
    r = await _post(client, auth, f"/docs/{carried}/finalize")
    assert r.status_code == 200, r.text
    r = await _funds_on(client, auth)
    assert r.status_code == 200, r.text

    # With funds in use, a file carrying any of the keys is refused naming it.
    for key, value in (("origin", "accountant"), ("fund_choice", "unrestricted"), ("fund_id", "fund:elsewhere")):
        eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
        r = await client.post("/docs/import", headers=auth["headers"], json={
            "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1],
                     "line_items": [_line(item, "Reserved", 5, **{key: value})], "total": 5}})
        assert r.status_code == 422 and _key(r) == "posting.fund.file_key", r.text
        assert r.json()["detail"]["params"]["field"] == key
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": "2026-01-16", "entries": entries}}]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert "The file gives fund_id. Choose funds on the import screen" in r.text, r.text

    # Turning funds off is refused while an unposted record holds a local choice, naming it.
    r = await _choose(client, auth, marked, lines={(await _doc(client, auth, marked))["line_items"][0]["line_id"]: "unrestricted"})
    assert r.status_code == 200, r.text
    r = await _funds_on(client, auth, False)
    assert r.status_code == 422 and _key(r) == "fund.turn_off_choices_held", r.text
    assert r.json()["detail"]["params"]["records"] == marked.split(":", 1)[1]


# ── rule R: every leg of an outside record is covered by a choice ──────────────────────

async def _gift_fund(client, auth) -> str:
    from test_funds import _fund

    return await _fund(client, auth, "Library gift")


def _needs_choice(r, record: str, lines: list[int], origin: str = ORIGIN) -> None:
    assert r.status_code == 409 and _key(r) == "posting.fund.choice_needed", r.text
    detail = r.json()["detail"]
    assert detail["params"]["record"] == record and detail["params"]["source"]["message_key"] == f"fund.origin.{origin}"
    assert r.json()["fix"] == {"kind": "funds", "document": True, "lines": lines}, r.json()


async def test_outside_line_without_choice_refused_at_prepare(client, session, auth):
    """At the boundary: an outside journal line with no choice, and one whose fund differs from
    its choice, raise FundChoiceNeeded naming the line; the same entry passes in a company
    without funds in use."""
    from celerp.services.journal_accounts import FundChoiceNeeded, prepare_journal_entry

    def entry(**second):
        return {"memo": "Firm correction", "origin": "accountant", "entries": [
            {"account": "1111", "debit": 10.0, "credit": 0, "origin": "accountant", "fund_choice": "unrestricted"},
            {"account": "4100", "debit": 0, "credit": 10.0, "origin": "accountant", **second}]}

    await prepare_journal_entry(session, auth["company_id"], entry())
    await session.commit()
    gift = await _fund_company(client, auth) and await _gift_fund(client, auth)
    for second in ({}, {"fund_choice": gift}, {"fund_choice": "unrestricted", "fund_id": gift}):
        with pytest.raises(FundChoiceNeeded) as raised:
            await prepare_journal_entry(session, auth["company_id"], entry(**second))
        await session.rollback()
        assert raised.value.detail["message_key"] == "posting.fund.choice_needed", second
        assert raised.value.detail["params"]["record"] == "Firm correction"
        assert raised.value.detail["params"]["source"]["message_key"] == "fund.origin.accountant"
        assert raised.value.fix == {"kind": "funds", "document": True, "lines": [2]}, second
    await prepare_journal_entry(session, auth["company_id"], entry(fund_choice="unrestricted"))
    await session.rollback()
    whole = entry()
    whole["fund_choice"] = "unrestricted"
    await prepare_journal_entry(session, auth["company_id"], whole)
    await session.rollback()


@pytest.mark.parametrize("shape", ["lines", "bill", "po_receipt", "opening_held", "credit_note", "debit_note",
                                   "list_copy", "shared_book", "connector", "migration"])
async def test_outside_choice_check_covers_every_posting_entry_point(client, session, auth, monkeypatch, shape):
    """An outside record with a leg no choice covers is refused at finalize with the J9c
    refusal, nothing posts and the draft stays a draft; its twin with a choice posts with that
    fund on every leg the choice covers. With the check made a no-op the refusal disappears,
    so it is the check that refuses. A record imported issued with no lines (header money
    only) is rejected by the import the same way."""
    from celerp.services import peer_lines

    gift = await _fund_company(client, auth) and await _gift_fund(client, auth)
    if shape == "po_receipt":
        return await _po_receipt_needs_choice(client, session, auth, gift)
    if shape == "opening_held":
        return await _opening_held_needs_choice(client, session, auth, gift)
    if shape in _FINALIZED:
        made = _FINALIZED[shape](client, auth, session) if shape == "connector" else _FINALIZED[shape](client, auth)
        return await _finalize_needs_choice(client, session, auth, gift, *await made)
    if shape == "migration":
        return await _migration_needs_choice(client, session, auth)
    item = await _item(client, auth, "Outside")
    if shape == "lines":
        doc = await _imported(client, auth, [_line(item, "Outside", 40), _line(item, "Outside", 60)])
        uncovered = [1, 2]
    else:
        doc = await _imported(client, auth, [{**_line(None, "Supplies", 25), "receive_as": "expense"}], doc_type="bill")
        uncovered = [1]
    ref = (await _doc(client, auth, doc))["ref_id"]
    before = await _entry_ids(session, auth)
    _needs_choice(await _post(client, auth, f"/docs/{doc}/finalize"), ref, uncovered)
    assert (await _doc(client, auth, doc))["status"] == "draft"
    assert await _entry_ids(session, auth) == before

    if shape == "lines":
        # One line chosen: the other still needs the document's fund.
        first = (await _doc(client, auth, doc))["line_items"][0]["line_id"]
        assert (await _choose(client, auth, doc, lines={first: gift})).status_code == 200
        _needs_choice(await _post(client, auth, f"/docs/{doc}/finalize"), ref, [2])
        with monkeypatch.context() as patched:
            patched.setattr(peer_lines, "check_outside_record", _no_check)
            r = await _post(client, auth, f"/docs/{doc}/finalize")
            assert r.status_code == 200, r.text
    else:
        assert (await _choose(client, auth, doc, document=gift)).status_code == 200
        r = await _post(client, auth, f"/docs/{doc}/finalize")
        assert r.status_code == 200, r.text
        entry = await _entry_state(session, auth["company_id"], f"je:auto:{doc}:bill")
        assert {e.get("fund_id") for e in entry["entries"]} == {gift}, entry["entries"]

    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"doc_type": "invoice" if shape == "lines" else "bill", "status": "final", "ref_id": eid.split(":")[1],
                 "issue_date": "2026-02-01", "line_items": [], "shipping": 15, "total": 15,
                 "import_treatment": "record_now"}}]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert "choose the fund for this document" in r.text, r.text


async def _po_receipt_needs_choice(client, session, auth, gift: str) -> None:
    """An imported purchase order received with an expense line and a stock line: the receipt
    is refused naming the expense line only, since stock needs no fund. With a fund chosen on
    the expense line it and its share of the payable post to that fund, the stock and its
    share to none, and the bill the order becomes finalizes with no document choice. A
    restricted fund chosen on the stock line is refused by rule 7 at the receipt, as it is
    at a bill."""
    from test_receive_selected_lines import _post as _receive

    sku = f"OUT-{uuid.uuid4().hex[:6]}"
    doc = await _imported(client, auth, [{**_line(None, "Supplies", 25), "receive_as": "expense"},
                                         {"sku": sku, "name": "Shelf stock", "quantity": 2, "unit_price": 30,
                                          "line_total": 60}], doc_type="purchase_order")
    ref = (await _doc(client, auth, doc))["ref_id"]
    received = ({"po_line_index": 0, "name": "Supplies", "quantity_received": 1, "receive_as": "expense"},
                {"po_line_index": 1, "sku": sku, "name": "Shelf stock", "quantity_received": 2, "receive_as": "stock"})
    before = await _entry_ids(session, auth)
    _needs_choice(await _receive(client, auth, doc, *received), ref, [1])
    assert await _entry_ids(session, auth) == before

    first, second = [li["line_id"] for li in (await _doc(client, auth, doc))["line_items"]]
    assert (await _choose(client, auth, doc, lines={first: gift, second: gift})).status_code == 200
    r = await _receive(client, auth, doc, *received)
    assert r.status_code == 422 and _key(r) == "posting.fund.stock_unrestricted", r.text
    assert await _entry_ids(session, auth) == before
    assert (await _choose(client, auth, doc, lines={first: gift, second: "unrestricted"})).status_code == 200
    r = await _receive(client, auth, doc, *received)
    assert r.status_code == 200, r.text
    [receipt] = [e for e in await _entry_ids(session, auth) - before if e.startswith(f"je:auto:{doc}:rcv")]
    legs = (await _entry_state(session, auth["company_id"], receipt))["entries"]
    debits = {leg["account"]: leg.get("fund_id") for leg in legs if leg.get("debit")}
    credits = {(leg["account"], leg.get("fund_id")) for leg in legs if leg.get("credit")}
    assert len(debits) == 2 and set(debits.values()) == {gift, None}, legs
    assert len({account for account, _ in credits}) == 1 and {fund for _, fund in credits} == {gift, None}, legs

    r = await _post(client, auth, f"/docs/{doc}/finalize")
    assert r.status_code == 200, r.text


async def _opening_held_needs_choice(client, session, auth, gift: str) -> None:
    """A bill imported as held in the opening balances: voiding it takes out what they hold,
    so the void is refused naming its line until a fund is chosen for it; with one, every
    line of the reversal posts to that fund, with no document choice."""
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-01-01", "memo": "Opening balances", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "2110", "credit": 25}, {"account": "3200", "debit": 25}]})
    assert r.status_code == 200, r.text
    doc = await _imported(client, auth, [{**_line(None, "Supplies", 25), "receive_as": "expense"}], doc_type="bill",
                          status="awaiting_payment", contact_id="supplier:1", issue_date="2026-01-01",
                          amount_outstanding=25, amount_paid=0, import_treatment="opening_balances")
    ref = (await _doc(client, auth, doc))["ref_id"]
    before = await _entry_ids(session, auth)
    _needs_choice(await _post(client, auth, f"/docs/{doc}/void", {}), ref, [1])
    assert (await _doc(client, auth, doc))["status"] == "awaiting_payment"
    assert await _entry_ids(session, auth) == before

    first = (await _doc(client, auth, doc))["line_items"][0]["line_id"]
    assert (await _choose(client, auth, doc, lines={first: gift})).status_code == 200
    r = await _post(client, auth, f"/docs/{doc}/void", {})
    assert r.status_code == 200, r.text
    [reversal] = await _entry_ids(session, auth) - before
    legs = (await _entry_state(session, auth["company_id"], reversal))["entries"]
    assert {leg.get("fund_id") for leg in legs} == {gift}, legs


async def _finalize_needs_choice(client, session, auth, gift: str, doc: str, uncovered: list[int], origin: str) -> None:
    """Finalize of an outside draft is refused naming its uncovered lines, nothing posts and
    the draft stays a draft; with a document choice every leg posts to that fund."""
    ref = (await _doc(client, auth, doc))["ref_id"]
    before = await _entry_ids(session, auth)
    _needs_choice(await _post(client, auth, f"/docs/{doc}/finalize"), ref, uncovered, origin)
    assert (await _doc(client, auth, doc))["status"] == "draft"
    assert await _entry_ids(session, auth) == before
    assert (await _choose(client, auth, doc, document=gift)).status_code == 200
    r = await _post(client, auth, f"/docs/{doc}/finalize")
    assert r.status_code == 200, r.text
    posted = await _entry_ids(session, auth) - before
    assert posted
    for entry in posted:
        legs = (await _entry_state(session, auth["company_id"], entry))["entries"]
        assert {leg.get("fund_id") for leg in legs} == {gift}, legs


async def _note(client, auth, doc_type: str) -> tuple[str, list[int], str]:
    """An unlinked credit or debit note brought in by the document import."""
    line = {**_line(None, "Supplies", 25), "receive_as": "expense"} if doc_type == "debit_note" else \
        _line(await _item(client, auth, "Outside"), "Outside", 25)
    return await _imported(client, auth, [line], doc_type=doc_type, contact_id="contact:1"), [1], ORIGIN


async def _credit_note(client, auth):
    return await _note(client, auth, "credit_note")


async def _debit_note(client, auth):
    return await _note(client, auth, "debit_note")


async def _list_copy(client, auth) -> tuple[str, list[int], str]:
    """An imported List duplicated, the copy finalized and converted to an invoice."""
    item = await _item(client, auth, "Outside")
    eid = f"list:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/lists/import", headers=auth["headers"], json={
        "entity_id": eid, "event_type": "list.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": {"list_type": "quotation", "status": "draft", "line_items": [_line(item, "Outside", 40)]}})
    assert r.status_code == 200, r.text
    copy = (await client.post(f"/lists/{eid}/duplicate", headers=auth["headers"])).json()["id"]
    assert (await client.post(f"/lists/{copy}/finalize", headers=auth["headers"])).status_code == 200
    r = await client.post(f"/lists/{copy}/convert", headers=auth["headers"], json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    return r.json().get("target_doc_id") or r.json()["id"], [1], ORIGIN


async def _shared_book(client, auth) -> tuple[str, list[int], str]:
    """A supplier's invoice received as a shared document and booked as a draft bill."""
    from test_received_documents import _bundle, _doc as _sent

    sent = {**_sent("invoice", f"S-{uuid.uuid4().hex[:6]}"), "line_items": [
        {"description": "Supplies", "quantity": 1, "unit_price": 30}]}
    r = await client.post("/docs/import-bundle", json=_bundle(sent, document=f"doc:{uuid.uuid4().hex[:6]}"),
                          headers=auth["headers"], follow_redirects=False)
    assert r.status_code == 302, r.text
    rid = r.headers["location"].rsplit("/", 1)[-1]
    r = await client.post(f"/docs/received/{rid}/book", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["id"], [1], "shared_document"


async def _connector(client, auth, session) -> tuple[str, list[int], str]:
    """An invoice written by the accounting-system connector's upsert, as QuickBooks maps one."""
    from celerp.events.engine import connector_upsert

    qid = uuid.uuid4().hex[:8]
    await connector_upsert(
        session, company_id=auth["company_id"], entity_type="doc", event_type="doc.created",
        idem_key=f"quickbooks:invoice:{qid}", origin="accounting_system", data={
            "doc_type": "invoice", "ref_id": f"QB-{qid}", "status": "draft", "total": 50, "currency": "USD",
            "line_items": [{"name": "Consulting", "quantity": 1, "unit_price": 50, "line_total": 50}]})
    await session.commit()
    return f"doc:quickbooks:invoice:{qid}", [1], "accounting_system"


_FINALIZED = {"credit_note": _credit_note, "debit_note": _debit_note, "list_copy": _list_copy,
              "shared_book": _shared_book, "connector": _connector}


async def _migration_needs_choice(client, session, auth) -> None:
    """The migration sink's writer, the journal import marked ``migration``, rejects a record
    whose lines no choice covers with the J9c refusal, and posts nothing."""
    from celerp_accounting.import_service import AccImportRecord, import_journal_records

    before = await _entry_ids(session, auth)
    outcome = await import_journal_records(session, auth["company_id"], auth["user_id"], [
        AccImportRecord(entity_id=f"je:{uuid.uuid4()}", event_type="acc.journal_entry.created", source="migration",
                        idempotency_key=uuid.uuid4().hex, data={"status": "posted", "ts": "2026-01-16", "memo": "Migrated",
                        "entries": [{"account": "1111", "debit": 9.0, "credit": 0},
                                    {"account": "4100", "debit": 0, "credit": 9.0}]})], origin="migration")
    await session.commit()
    [record] = outcome.records
    assert record.status == "failed" and "choose the fund for this document" in str(record.message), record
    assert await _entry_ids(session, auth) == before


async def _no_check(*_args, **_kwargs) -> None:
    return None


async def _entry_ids(session, auth) -> set:
    from sqlalchemy import select

    session.expire_all()
    return set((await session.execute(select(Projection.entity_id).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all())


async def test_outside_journal_import_needs_a_choice(client, auth):
    """A journal import in a company with funds in use is rejected for the record with the
    J9c refusal: its lines arrive with no choice and the file may not carry one."""
    await _fund_company(client, auth)
    entries = [{"account": "1111", "debit": 9.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 9.0}]
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": "2026-01-16", "memo": "Imported",
                                                      "entries": entries}}]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert "choose the fund for this document" in r.text, r.text


async def test_reversal_copies_origin_and_choice(client, session, auth):
    """Reversals and restores copy each posted line's origin, fund and choice and name the
    entry they copy: an accountant's journal posted before funds were in use reverses with
    the choice Unrestricted and needs no new choice; an outside invoice voided and restored
    puts back the fund it posted with; a credit note line linked to its line credits that fund."""
    from celerp.events.engine import emit_event
    from celerp.services import peer_journals
    from test_income_accounts import _line_ids

    cid, user = auth["company_id"], auth["user_id"]
    firm_je = f"je:{uuid.uuid4()}"
    await emit_event(session, origin="accountant", company_id=cid, entity_id=firm_je, entity_type="journal_entry",
                     event_type="acc.journal_entry.created", data={
                         "memo": "Firm accrual", "ts": "2026-01-10", "je_type": "manual", "status": "posted",
                         "peer_origin": {"journal": "j1", "firm": "Firm", "firm_company": str(uuid.uuid4())},
                         "entries": [{"account": "1111", "debit": 12.0, "credit": 0},
                                     {"account": "4100", "debit": 0, "credit": 12.0}]},
                     actor_id=None, location_id=None, source="connector", idempotency_key=uuid.uuid4().hex, metadata_={})
    await session.commit()
    gift = await _fund_company(client, auth) and await _gift_fund(client, auth)
    reversal = await peer_journals.reverse(session, cid, user, firm_je)
    state = await _entry_state(session, cid, reversal["je_id"])
    assert [(e["account"], e["debit"], e["credit"], e.get("origin"), e.get("fund_choice"), e.get("fund_id"),
             e.get("posted_from")) for e in state["entries"]] == [
        ("1111", 0, 12.0, "accountant", "unrestricted", None, firm_je),
        ("4100", 12.0, 0, "accountant", "unrestricted", None, firm_je)], state["entries"]
    assert state.get("origin") == "accountant"

    item = await _item(client, auth, "Outside")
    doc = await _imported(client, auth, [_line(item, "Outside", 40)])
    assert (await _choose(client, auth, doc, document=gift)).status_code == 200
    assert (await _post(client, auth, f"/docs/{doc}/finalize")).status_code == 200
    assert (await _post(client, auth, f"/docs/{doc}/void")).status_code == 200
    r = await _post(client, auth, f"/docs/{doc}/unvoid")
    assert r.status_code == 200, r.text
    restored = await _entry_state(session, cid, f"je:auto:{doc}:fin:unvoid:1")
    assert {e.get("fund_id") for e in restored["entries"]} == {gift}, restored["entries"]
    assert {e.get("posted_from") for e in restored["entries"]} == {f"je:auto:{doc}:fin"}

    other = await _imported(client, auth, [_line(item, "Outside", 30)])
    assert (await _choose(client, auth, other, document=gift)).status_code == 200
    assert (await _post(client, auth, f"/docs/{other}/finalize")).status_code == 200
    line_id = (await _line_ids(client, auth, other))[0]
    note = await _issued(client, auth, "credit_note", [_line(item, "Outside", 30, source_line_id=line_id)],
                         original_doc_id=other)
    entries = (await _finalize_entry(session, auth, note))["entries"]
    assert {e.get("fund_id") for e in entries} == {gift}, entries


async def _second_fund(client, auth, name: str = "Board reserve") -> str:
    from test_funds import _fund

    return await _fund(client, auth, name, restricted=False, purpose=None)


def _refused_as(r, status: int, key: str) -> dict:
    assert r.status_code == status and _key(r) == key, r.text
    return r.json()["detail"]


async def test_document_fund_choices_route_validates(client, session, auth):
    """Each refusal of the fund-choices route names its reason and writes nothing: a user
    who cannot edit documents, a line not on the document or not from outside, a value that
    is no active fund of this company, a request choosing nothing, and a document that has
    posted, which Change fund corrects instead."""
    from test_helpers import invite_user

    fund = await _fund_company(client, auth)
    doc, _, _ = await _two_line_import(client, auth)
    first, second = (await _doc(client, auth, doc))["line_items"]
    assert (await _patch(client, auth, doc, line_items=[
        _as_sent(first), _as_sent(second), _line(None, "Local", 5)])).status_code == 200
    stored = await _doc(client, auth, doc)
    local = stored["line_items"][2]["line_id"]
    archived = await _second_fund(client, auth, "Closed appeal")
    r = await client.post(f"/accounting/funds/{archived}/archive", headers=auth["headers"])
    assert r.status_code == 200, r.text

    viewer = await invite_user(client, session, auth["headers"], f"viewer-{uuid.uuid4().hex[:6]}@example.com", "viewer")
    r = await client.post(f"/docs/{doc}/fund-choices", headers={"Authorization": f"Bearer {viewer}"},
                          json={"expected_version": stored["version"], "document": fund})
    assert r.status_code == 403, r.text
    for body, key, params in (
            ({"lines": {"no-such-line": fund}}, "posting.fund.choice_line_unknown", {}),
            ({"lines": {local: fund}}, "posting.fund.choice_local_line", {"line": 3}),
            ({"lines": {first["line_id"]: "not-a-fund"}}, "posting.fund.unknown", {"fund": "not-a-fund"}),
            ({"all": archived}, "posting.fund.archived", {"fund": "Closed appeal"}),
            ({"lines": {first["line_id"]: None}}, "posting.fund.choice_missing", {"line": 1}),
            ({}, "posting.fund.choice_empty", {}),
            ({"lines": {}}, "posting.fund.choice_empty", {}),
            ({"document": "not-a-fund"}, "posting.fund.unknown", {"fund": "not-a-fund"}),
            ({"document": None}, "posting.fund.document_choice_missing", {})):
        detail = _refused_as(await _choose(client, auth, doc, **body), 422, key)
        assert detail.get("params", {}) == params, (body, detail)
        assert await _doc(client, auth, doc) == stored, body

    assert (await _choose(client, auth, doc, document=fund)).status_code == 200
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    posted = await _doc(client, auth, doc)
    _refused_as(await _choose(client, auth, doc, document="unrestricted"), 409, "posting.fund.document_posted")
    assert await _doc(client, auth, doc) == posted


async def test_fund_choices_route_stale_version_refused(client, auth):
    """Two tabs on one document: the second save of a fund choice made from the older copy
    is refused as stale, so the page keeps its selection and offers Try again, and nothing
    is written; the same choice sent again changes nothing."""
    fund = await _fund_company(client, auth)
    doc, _, _ = await _two_line_import(client, auth)
    version = (await _doc(client, auth, doc))["version"]
    url = f"/docs/{doc}/fund-choices"
    r = await client.post(url, headers=auth["headers"], json={"expected_version": version, "document": fund})
    assert r.status_code == 200, r.text
    saved = await _doc(client, auth, doc)
    r = await client.post(url, headers=auth["headers"], json={"expected_version": version, "document": "unrestricted"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version", r.text
    assert await _doc(client, auth, doc) == saved
    r = await client.post(url, headers=auth["headers"], json={"expected_version": saved["version"], "document": fund})
    assert r.status_code == 200 and r.json() == {"event_id": None, "version": saved["version"]}, r.text


async def test_set_all_outside_lines_then_edit_one(client, auth, monkeypatch):
    """Set all outside lines writes the choice to every line from outside that has none and
    leaves a line already chosen as it is; a later edit of one line changes only that line.
    The document import review does the same before anything is created: Set all imported
    documents and one line's own Fund, kept through a revalidate and stored on confirm."""
    fund = await _fund_company(client, auth)
    other = await _second_fund(client, auth)
    a, b, c = [await _item(client, auth, name) for name in ("Out A", "Out B", "Out C")]
    doc = await _imported(client, auth, [_line(a, "Out A", 10), _line(b, "Out B", 20), _line(c, "Out C", 30)])
    first, second, third = (li["line_id"] for li in (await _doc(client, auth, doc))["line_items"])

    assert (await _choose(client, auth, doc, lines={first: other})).status_code == 200
    assert (await _choose(client, auth, doc, all=fund)).status_code == 200
    assert _choices(await _doc(client, auth, doc))[1] == [(other, other), (fund, fund), (fund, fund)]
    assert (await _choose(client, auth, doc, lines={second: "unrestricted"})).status_code == 200
    assert _choices(await _doc(client, auth, doc))[1] == [(other, other), ("unrestricted", None), (fund, fund)]

    # The document import review: no control chosen yet, then Set all and one line's own.
    from test_import_upsert import _numbered_via, _Routes, _staged_form
    from ui.routes import docs_import

    async def _get(_tok, path):
        r = await client.get(path, headers=auth["headers"])
        assert r.status_code == 200, r.text
        return r.json()

    async def _batch(_tok, path, records, upsert=False, fund_choices=None):
        return (await client.post(path, headers=auth["headers"], json={
            "records": records, "upsert": upsert, "fund_choices": fund_choices})).json()

    monkeypatch.setattr(docs_import, "_token", lambda request: "tok")
    monkeypatch.setattr(docs_import.api, "get_company", lambda tok: _get(tok, "/companies/me"))
    monkeypatch.setattr(docs_import.api, "list_funds", lambda tok: _get(tok, "/accounting/funds"))
    monkeypatch.setattr(docs_import.api, "batch_import", _batch)
    monkeypatch.setattr(docs_import.api, "numbered_ids", _numbered_via(client, auth["headers"]))
    routes = _Routes()
    docs_import.setup_routes(routes)
    number = f"FI-{uuid.uuid4().hex[:6]}"
    csv_text = ("doc_type,doc_number,date,status,total,line_stone_type,line_qty,line_unit_price,line_total_price\n"
                f"invoice,{number},2026-01-20,draft,30,Filed A,1,10,10\n"
                f"invoice,{number},2026-01-20,draft,30,Filed B,1,20,20\n")
    every, own = "fund_all", f"fund_line:invoice:{number}:2"

    def _picked(html: str, name: str) -> str:
        m = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', html)
        assert m, (name, html)
        return re.search(r'value="([^"]*)"', m.group(0)).group(1)

    html = to_xml(await routes.post_routes["/docs/import/revalidate"](await _staged_form("tok", csv_text)))
    assert _picked(html, every) == "" and _picked(html, f"fund_line:invoice:{number}:1") == "" and _picked(html, own) == ""
    assert "General" in html and "Board reserve" in html, html
    form = re.search(r"<form.*?</form>", html, re.S).group(0)
    assert every in form and own in form
    kept = await _staged_form("tok", csv_text, **{every: fund, own: other})
    html = to_xml(await routes.post_routes["/docs/import/revalidate"](kept))
    assert (_picked(html, every), _picked(html, own)) == (fund, other)
    to_xml(await routes.post_routes["/docs/import/confirm"](await _staged_form("tok", csv_text, **{every: fund, own: other})))
    rows = (await client.get("/docs", headers=auth["headers"], params={"q": number})).json()["items"]
    assert len(rows) == 1, rows
    state = await _doc(client, auth, rows[0]["id"])
    assert (state.get("fund_choice"), state.get("fund_choice_source")) == (fund, "user")
    assert _choices(state)[1] == [(None, None), (other, other)]


def _issued_import(entity_id: str, lines: list[dict]) -> dict:
    """An open invoice in an import file, booked now."""
    total = round(sum(li["line_total"] for li in lines), 2)
    return {"entity_id": entity_id, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "status": "awaiting_payment", "ref_id": entity_id.split(":")[1],
                     "issue_date": "2026-01-20", "line_items": lines, "total": total, "subtotal": total,
                     "amount_outstanding": total, "import_treatment": "record_now"}}


def _income_funds(entry: dict) -> list[tuple]:
    return sorted((e["account"], e.get("fund_id"), e.get("fund_choice")) for e in entry["entries"] if e.get("credit"))


async def test_import_fund_choices_per_record(client, session, auth):
    """The document import and the journal import take the fund of each record and line from
    ``fund_choices``: the record's choice covers its lines, a line's own choice wins for that
    line, and a record no choice covers is rejected naming its lines while the rest posts."""
    await _fund_company(client, auth)
    gift = await _gift_fund(client, auth)
    a, b = await _item(client, auth, "Filed A"), await _item(client, auth, "Filed B")
    chosen, left = f"doc:IMP-{uuid.uuid4().hex[:8]}", f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={
        "records": [_issued_import(chosen, [_line(a, "Filed A", 40), _line(b, "Filed B", 60)]),
                    _issued_import(left, [_line(a, "Filed A", 15)])],
        "fund_choices": {"records": {chosen: gift}, "lines": {f"{chosen}:2": "unrestricted"}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert "choose the fund for this document" in r.text and "Line 1 has no fund of its own" in r.text, r.text
    state = await _doc(client, auth, chosen)
    assert (state.get("fund_choice"), state.get("fund_choice_source")) == (gift, "user")
    assert _choices(state)[1] == [(None, None), ("unrestricted", None)]
    entry = await _finalize_entry(session, auth, chosen)
    assert {(e.get("fund_id"), e["credit"]) for e in entry["entries"] if e.get("credit")} == {(gift, 40), (None, 60)}
    assert (await client.get(f"/docs/{left}", headers=auth["headers"])).status_code == 404

    # The journal import: ``all`` covers every record and line, a line's own choice wins.
    first, second = f"je:{uuid.uuid4()}", f"je:{uuid.uuid4()}"
    entries = [{"account": "1111", "debit": 30.0, "credit": 0}, {"account": "4100", "debit": 0, "credit": 30.0}]

    def _journal(eid: str) -> dict:
        return {"entity_id": eid, "event_type": "acc.journal_entry.created", "source": "csv",
                "idempotency_key": uuid.uuid4().hex,
                "data": {"status": "posted", "ts": "2026-01-21", "memo": "Filed", "entries": entries}}

    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={
        "records": [_journal(first), _journal(second)],
        "fund_choices": {"all": gift, "lines": {f"{first}:1": "unrestricted", f"{first}:2": "unrestricted"}}})
    assert r.status_code == 200 and r.json()["created"] == 2, r.text
    one, two = [await _entry_state(session, auth["company_id"], eid) for eid in (first, second)]
    assert [(e.get("fund_id"), e.get("fund_choice")) for e in one["entries"]] == [(None, "unrestricted")] * 2
    assert [(e.get("fund_id"), e.get("fund_choice")) for e in two["entries"]] == [(gift, gift)] * 2
    assert two.get("fund_choice") == gift


async def test_import_fund_choices_refused_as_a_request(client, auth):
    """``fund_choices`` that cannot apply refuse the whole request and write nothing: a
    record or line the batch does not carry, a line that is not a number, a fund that is not
    this company's, and any choice while funds are off."""
    item = await _item(client, auth, "Filed")
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    record = _issued_import(eid, [_line(item, "Filed", 10)])
    send = lambda choices: client.post("/docs/import/batch", headers=auth["headers"],  # noqa: E731
                                       json={"records": [record], "fund_choices": choices})
    assert _key(await send({"all": "unrestricted"})) == "posting.fund.import_funds_off"
    await _fund_company(client, auth)
    for choices, key in (({"records": {"doc:elsewhere": "unrestricted"}}, "posting.fund.choice_record_unknown"),
                         ({"lines": {"doc:elsewhere:1": "unrestricted"}}, "posting.fund.choice_record_unknown"),
                         ({"lines": {f"{eid}:first": "unrestricted"}}, "posting.fund.import_line_unknown"),
                         ({"lines": {f"{eid}:2": "unrestricted"}}, "posting.fund.import_line_unknown"),
                         ({"all": "fund:elsewhere"}, "posting.fund.unknown")):
        r = await send(choices)
        assert r.status_code == 422 and _key(r) == key, (choices, r.text)
    assert (await client.get(f"/docs/{eid}", headers=auth["headers"])).status_code == 404


async def test_import_batch_refuses_fund_lines_and_movement_shapes(client, session, auth):
    """A journal import is refused for each record whose file line carries fund_id,
    fund_choice or origin (funds are chosen on the import screen), and for each record that
    would move restricted money: a fund_release record and a line on restricted net assets.
    The record beside them posts on the fund chosen for the import."""
    await _fund_company(client, auth)
    gift = await _gift_fund(client, auth)
    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    rna = next(a for a in accounts["roles"] if a["role"] == "restricted_net_assets")["code"]

    def _record(entries: list[dict], **data) -> dict:
        return {"entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
                "idempotency_key": uuid.uuid4().hex,
                "data": {"status": "posted", "ts": "2026-01-22", "memo": "Filed", "entries": entries, **data}}

    def _lines(credit: str = "4100", **extra) -> list[dict]:
        return [{"account": "1111", "debit": 25.0, "credit": 0},
                {"account": credit, "debit": 0, "credit": 25.0, **extra}]

    keyed = [_record(_lines(**{key: value})) for key, value in
             (("fund_id", gift), ("fund_choice", "unrestricted"), ("origin", "accountant"))]
    release, restricted, plain = _record(_lines(), je_type="fund_release"), _record(_lines(rna)), _record(_lines())
    records = [*keyed, release, restricted, plain]
    r = await client.post("/accounting/import/batch", headers=auth["headers"],
                          json={"records": records, "fund_choices": {"all": gift}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    errors = r.json()["errors"]
    for record, key in zip(keyed, ("fund_id", "fund_choice", "origin")):
        assert any(e.startswith(record["entity_id"]) and
                   f"The file gives {key}. Choose funds on the import screen, not in the file." in e
                   for e in errors), (key, errors)
    for record in (release, restricted):
        assert any(e.startswith(record["entity_id"]) and "moves money of" in e and
                   "Transfer between funds, Release from restriction or Reallocate restricted funds" in e
                   for e in errors), (record["data"], errors)
    posted = await _entry_state(session, auth["company_id"], plain["entity_id"])
    assert [e.get("fund_id") for e in posted["entries"]] == [gift, gift]
    for record in records[:-1]:
        assert await session.get(Projection, {"company_id": auth["company_id"], "entity_id": record["entity_id"]}) is None


async def test_import_named_authorizer_moves_nothing(client, session, auth):
    """A manager, who may import but not reallocate restricted funds, imports a
    fund_reallocation record that names the company's admin as its authorizer: it is refused
    as a movement outside the movement forms, and nothing posts."""
    from test_fund_waiting import _operator
    from test_funds import _fund

    await _fund_company(client, auth)
    gift, other = await _gift_fund(client, auth), await _fund(client, auth, "Library gift two")
    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    rna = next(a for a in accounts["roles"] if a["role"] == "restricted_net_assets")["code"]
    eid = f"je:{uuid.uuid4()}"
    before = await _entry_ids(session, auth)
    r = await client.post("/accounting/import/batch", headers=await _operator(session, auth, "manager"), json={
        "records": [{"entity_id": eid, "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     "data": {"status": "posted", "ts": "2026-01-23", "memo": "Reallocate", "je_type": "fund_reallocation",
                              "authorized_by": str(auth["user_id"]), "reason": "Donor asked",
                              "donor_authorization": "Letter of 2026-01-20",
                              "entries": [{"account": rna, "debit": 10.0, "credit": 0},
                                          {"account": "1111", "debit": 0, "credit": 10.0},
                                          {"account": "1111", "debit": 10.0, "credit": 0},
                                          {"account": rna, "debit": 0, "credit": 10.0}]}}],
        "fund_choices": {"lines": {f"{eid}:1": gift, f"{eid}:2": gift, f"{eid}:3": other, f"{eid}:4": other}}})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert "Restricted money moves only through Transfer between funds" in r.json()["errors"][0], r.text
    assert await _entry_ids(session, auth) == before


async def test_migration_sink_journal_refuses_movement_shapes(client, session, auth):
    """The migration sink's journal writer judges movement shapes as any import does: in
    books that hold restricted net assets, a movement-typed record and a line on restricted
    net assets are each rejected in the outcome, the plain record beside them posts."""
    from celerp_accounting.import_service import AccImportRecord, import_journal_records

    await _fund_company(client, auth)
    accounts = (await client.get("/accounting/posting-accounts", headers=auth["headers"])).json()
    rna = next(a for a in accounts["roles"] if a["role"] == "restricted_net_assets")["code"]
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": False})
    assert r.status_code == 200, r.text

    def _record(credit: str = "4100", **data) -> AccImportRecord:
        return AccImportRecord(entity_id=f"je:{uuid.uuid4()}", event_type="acc.journal_entry.created",
                               source="migration", idempotency_key=uuid.uuid4().hex,
                               data={"status": "posted", "ts": "2026-01-24", "memo": "Migrated", **data,
                                     "entries": [{"account": "1111", "debit": 7.0, "credit": 0},
                                                 {"account": credit, "debit": 0, "credit": 7.0}]})

    shaped = [_record(je_type=t) for t in ("fund_transfer", "fund_release", "fund_reallocation", "fund_opening")]
    shaped.append(_record(rna))
    plain = _record()
    outcome = await import_journal_records(session, auth["company_id"], auth["user_id"], [*shaped, plain],
                                           origin="migration")
    await session.commit()
    status = {rec.entity_id: (rec.status, str(rec.message)) for rec in outcome.records}
    for record in shaped:
        assert status[record.entity_id][0] == "failed" and "moves money of" in status[record.entity_id][1], status
    assert status[plain.entity_id][0] == "created", status
    ids = await _entry_ids(session, auth)
    assert plain.entity_id in ids and not ids & {rec.entity_id for rec in shaped}


async def test_every_line_writer_has_a_row():
    """Every line writer in the source (line_writer_census) has one row with a class whose
    cells cover each fund key; a row with no writer, a helper that is gone, a pending writer
    that now exists, an ARRIVAL writer whose emit passes no origin, and a connector_upsert
    call without the origin keyword each fail naming it."""
    from line_writer_census import ARRIVAL_CLASSES, CELLS, HELPERS, PENDING, ROWS, census, table

    writers, defined, bare_upserts = census()
    found = {w.key for w in writers}
    assert not found - ROWS.keys(), f"line writers with no row: {sorted(found - ROWS.keys())}"
    assert not ROWS.keys() - found, f"rows with no writer: {sorted(ROWS.keys() - found)}"
    assert not HELPERS.keys() - defined, f"helpers no longer defined: {sorted(HELPERS.keys() - defined)}"
    built = sorted(key for key in defined if key.rpartition("::")[2] in PENDING)
    assert not built, f"pending writers now built, give each a row: {built}"
    classes = {cls for cls, _ in [*ROWS.values(), *HELPERS.values(), *PENDING.values()]}
    assert all(len(CELLS.get(cls, ())) == 3 for cls in classes), sorted(classes - CELLS.keys())
    silent = sorted(f"{w.key}:{w.line}" for w in writers if ROWS[w.key][0] in ARRIVAL_CLASSES and not w.origin)
    assert not silent, f"ARRIVAL writers whose emit passes no origin: {silent}"
    assert not bare_upserts, f"connector_upsert calls without origin=: {bare_upserts}"
    assert len(table(writers)) == 2 + len(writers) + len(HELPERS) + len(PENDING)
