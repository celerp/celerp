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
    """(module, innermost function) of every ``posted_from`` string or keyword in app code."""
    from fund_census import sources

    found = set()
    for module, path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, fn):
            for child in ast.iter_child_nodes(node):
                here = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if (isinstance(child, ast.Constant) and child.value == "posted_from") or (
                        isinstance(child, ast.keyword) and child.arg == "posted_from"):
                    found.add((module, here))
                walk(child, here)

        walk(tree, "<module>")
    return found


async def test_every_auto_posting_names_its_record(client, session, auth):
    calls = list(_auto_posting_calls())
    assert len(calls) > 30, "the census found too few auto postings to be reading the source"
    missing = [(m, c.lineno) for m, c in calls if not any(kw.arg == "record_id" for kw in c.keywords)]
    assert missing == [], f"auto postings that do not name their record: {missing}"
    assert {f for _, f in _posted_from_writers()} <= {"copy_posted_lines", "fund_open_shares"}, _posted_from_writers()

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

    # With funds in use, inbound data carrying any of the keys is refused naming it.
    for key, value in (("origin", "accountant"), ("fund_choice", "unrestricted"), ("fund_id", "fund:elsewhere")):
        eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
        r = await client.post("/docs/import", headers=auth["headers"], json={
            "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
            "data": {"doc_type": "invoice", "status": "draft", "ref_id": eid.split(":")[1],
                     "line_items": [_line(item, "Reserved", 5, **{key: value})], "total": 5}})
        assert r.status_code == 422 and _key(r) == "posting.fund.inbound_key", r.text
        assert r.json()["detail"]["params"]["field"] == key
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex, "data": {"status": "posted", "ts": "2026-01-16", "entries": entries}}]})
    assert r.status_code == 200 and r.json()["created"] == 0, r.text
    assert "came in with fund_id" in r.text, r.text

    # Turning funds off is refused while an unposted record holds a local choice, naming it.
    r = await _choose(client, auth, marked, lines={(await _doc(client, auth, marked))["line_items"][0]["line_id"]: "unrestricted"})
    assert r.status_code == 200, r.text
    r = await _funds_on(client, auth, False)
    assert r.status_code == 422 and _key(r) == "fund.turn_off_choices_held", r.text
    assert r.json()["detail"]["params"]["records"] == marked.split(":", 1)[1]
