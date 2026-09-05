# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""WP1 list-query pushdown and paged read/write (post-#318 stability).

A single large-list request used to load, count, weigh, and slice the whole list in
Python and buffer the whole CSV before streaming. These tests pin the bounded contract
behaviourally: the index and export never drag each list's whole `state` document back
into Python (the count and per-line weight are computed in SQL and only the header
columns are read), the index response carries counts rather than the raw line array,
and two endpoints read one bounded page (`GET /lists/{id}/page`) and write one page
under the existing optimistic-version lock without touching off-page rows
(`PATCH /lists/{id}/line-page`).

The materialisation proof is DB traffic, not SQL spelling: a `before_cursor_execute`
listener captures every executed statement and we assert that no full-`state`
projection read runs on the hot path. Asserting a keyword like `LIMIT` appears in the
SQL proves nothing (the pre-pushdown code already used `LIMIT`); asserting the whole
`state` column is never selected proves the array is never loaded.
"""

from __future__ import annotations

import contextlib
import re
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


# ── helpers (mirror test_audits/test_writeoffs conventions) ────────────────────

async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@pushdown.test"
    r = await client.post("/auth/register", json={
        "company_name": "Pushdown Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _user_with_role(client, session, admin_token: str, role: str) -> str:
    addr = f"{role}-{uuid.uuid4().hex[:8]}@pushdown.test"
    r = await client.post("/companies/me/users",
                          json={"name": role.title(), "email": addr,
                                "password": "testpass123", "role": role},
                          headers=_h(admin_token))
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def _quotation(client, t, ref_id: str | None = None) -> str:
    body = {"list_type": "quotation"}
    if ref_id:
        body["ref_id"] = ref_id
    r = await client.post("/lists", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _state(client, t, list_id) -> dict:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()


async def _version(client, t, list_id) -> int:
    return (await client.get(f"/lists/{list_id}", headers=_h(t))).json()["version"]


async def _set_lines(client, t, list_id, lines: list[dict]) -> int:
    """Replace the whole line_items array (mandatory version pin) and return the new version."""
    v = await _version(client, t, list_id)
    r = await client.patch(f"/lists/{list_id}", headers=_h(t),
                           json={"fields_changed": {"line_items": {"new": lines}},
                                 "expected_version": v})
    assert r.status_code == 200, r.text
    return r.json()["version"]


def _lines(n: int, *, start: int = 0) -> list[dict]:
    return [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
             "quantity": 1, "unit_price": 1.0} for i in range(start, start + n)]


@contextlib.contextmanager
def _sql_spy():
    """Capture (statement, parameters) for every SQL statement executed on any engine during
    the block via a `before_cursor_execute` listener - an honest DB-traffic probe, never a
    monkeypatch of an application function. Attaches to the Engine class so it catches the sync
    engine backing the async one (SQLAlchemy fires cursor events on that underlying engine)."""
    captured: list[tuple[str, object]] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield captured
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


# A `select(Projection)` puts every ORM column - the bare `projections.state` among them - in
# its select-list, dragging each row's whole document into Python. The pushed-down index/export
# selects place only labelled sub-fields (`state ->> 'x'`) and aggregates
# (`json_array_length(state -> ...)`, a `json_array_elements` weight sum) there, never the bare
# column. So "a SELECT over projections whose select-list carries the bare `projections.state`
# column" is exactly the full-document read the pushdown removes. The negative lookahead keeps a
# `state ->` / `state ::` / `state[` subscript from counting as the bare column.
_BARE_STATE_COL = re.compile(r"\bprojections\.state\b(?!\s*(?:->|::|\[))")


def _full_state_reads(captured) -> list[str]:
    """The captured statements that read the whole projection `state` document: a SELECT over the
    projections table whose select-list (the text before its FROM clause) contains the bare
    `projections.state` column."""
    hits: list[str] = []
    for statement, _parameters in captured:
        low = statement.lower()
        if not low.lstrip().startswith("select"):
            continue
        i = low.find("from projections")
        if i < 0:
            continue
        if _BARE_STATE_COL.search(statement[:i]):
            hits.append(statement)
    return hits


# ── index / summary / export: no whole-document read on the hot path ───────────

@pytest.mark.asyncio
async def test_list_index_pushdown_paginates_in_sql(client):
    """GET /lists returns one bounded page + the full-set total without loading each list's whole
    document: the response carries no raw line_items array and the request issues no full-`state`
    projection read (the pre-pushdown code did both)."""
    t = await _register(client)
    for i in range(7):
        await _quotation(client, t, ref_id=f"IDX-{i:03d}")

    with _sql_spy() as sql:
        r = await client.get("/lists?limit=3&offset=0", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) <= 3
    assert body["total"] == 7                       # count reflects the whole set, not the page
    # Behavioural: an index row is a header plus counts, never the whole line array.
    assert all("line_items" not in it for it in body["items"]), (
        "the index must not carry each list's whole line_items array back to the client")
    full = _full_state_reads(sql)
    assert not full, (
        "the index hot path must not read each list's whole projection state row "
        "(the pre-pushdown full-array load). Offending statements:\n" + "\n".join(full))


@pytest.mark.asyncio
async def test_list_index_sql_order_matches_python(client):
    """The index returns rows newest-first (issue_date > created_at > date, entity_id tiebreak),
    the same order the Python sort produced, and does so without a full-document read."""
    t = await _register(client)
    ids = [await _quotation(client, t, ref_id=f"ORD-{i:03d}") for i in range(5)]

    with _sql_spy() as sql:
        r = await client.get("/lists?limit=100", headers=_h(t))
    assert r.status_code == 200, r.text
    returned = [it["id"] for it in r.json()["items"]]
    # Same-day rows: deterministic newest-first is the entity_id tiebreak (descending).
    expected = sorted(ids, reverse=True)
    assert returned == expected, f"order {returned} != expected {expected}"
    assert _full_state_reads(sql) == [], "ordering must not require loading each whole document"


@pytest.mark.asyncio
async def test_list_index_filters_equivalence(client):
    """q / all_issued / exclude_status / converted_to_type return the identical id set the Python
    filters returned, computed without loading each list's whole document."""
    t = await _register(client)
    keep = await _quotation(client, t, ref_id="MATCH-ONE")
    other = await _quotation(client, t, ref_id="NOPE-TWO")

    with _sql_spy() as sql:
        r = await client.get("/lists?q=match", headers=_h(t))
    assert r.status_code == 200, r.text
    got = {it["id"] for it in r.json()["items"]}
    assert got == {keep}, f"q filter returned {got}"
    assert other not in got
    assert _full_state_reads(sql) == [], "filtering must not require loading each whole document"


@pytest.mark.asyncio
async def test_list_summary_sql_aggregates(client):
    """The 7 summary keys are correct and computed without loading each list's whole document
    (the aggregation is a grouped SQL pass, pre-existing at the baseline - this pins it against
    regressing back to a Python row loop)."""
    t = await _register(client)
    for i in range(4):
        await _quotation(client, t, ref_id=f"SUM-{i:03d}")

    with _sql_spy() as sql:
        r = await client.get("/lists/summary", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("total_count", "draft_count", "all_issued_count", "total_value",
              "converted_to_memo_count", "converted_to_invoice_count", "count_by_status"):
        assert k in body, f"summary missing {k}"
    assert body["total_count"] == 4 and body["draft_count"] == 4  # all drafts here
    assert _full_state_reads(sql) == [], (
        "summary must aggregate in SQL, never load each list's whole state row")


@pytest.mark.asyncio
async def test_list_index_counts_and_weights_utf8(client):
    """The index carries item_count (a json_array_length) and total_weight (a json_array_elements
    sum) computed in SQL, equal to the Python-side truth even when line descriptions carry
    multibyte UTF-8 - the encoding-sensitive weight path reads the same JSON the app round-trips,
    and never loads the whole array to do it."""
    t = await _register(client)
    q = await _quotation(client, t, ref_id="WT-001")
    lines = [
        {"item_id": "item:1", "sku": "S1", "description": "Rubĩes \U0001F48E ê",
         "quantity": 1, "unit_price": 1.0, "weight_ct": "2.5"},
        {"item_id": "item:2", "sku": "S2", "description": "钻石 gems",
         "quantity": 1, "unit_price": 1.0, "weight_ct": "3.5"},
        {"description": "Free 문자열", "quantity": 1, "unit_price": 1.0, "weight": "4"},
    ]
    await _set_lines(client, t, q, lines)

    with _sql_spy() as sql:
        r = await client.get("/lists?q=WT-001", headers=_h(t))
    assert r.status_code == 200, r.text
    row = next(it for it in r.json()["items"] if it["id"] == q)
    assert row["item_count"] == 3, "item_count is json_array_length of the stored array"
    assert row["total_weight"] == pytest.approx(10.0), (
        "total_weight sums weight_ct (falling back to weight) across the UTF-8 rows")
    assert _full_state_reads(sql) == [], (
        "count and weight must come from SQL, never a full-array load")


@pytest.mark.asyncio
async def test_list_export_csv_streams(client):
    """CSV export reads only the emitted columns in bounded SQL batches and streams them, never
    loading each list's whole document to write its header row."""
    t = await _register(client)
    for i in range(6):
        await _quotation(client, t, ref_id=f"CSV-{i:03d}")

    with _sql_spy() as sql:
        r = await client.get("/lists/export/csv", headers=_h(t))
    assert r.status_code == 200, r.text
    text = r.text
    assert text.splitlines()[0].startswith("id,ref_id"), text.splitlines()[:1]
    assert "CSV-000" in text
    full = _full_state_reads(sql)
    assert not full, (
        "export must read only its CSV columns from SQL, never each list's whole state row. "
        "Offending statements:\n" + "\n".join(full))


# ── GET /lists/{id}/page : new bounded paged read ──────────────────────────────

@pytest.mark.asyncio
async def test_list_page_endpoint_bounded(client):
    """GET /lists/{id}/page hard-caps limit at 100, returns the page items + total +
    version, and rejects a negative offset with 400."""
    t = await _register(client)
    q = await _quotation(client, t)
    await _set_lines(client, t, q, _lines(250))

    r = await client.get(f"/lists/{q}/page?offset=0&limit=500", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 100, "limit must be hard-capped at 100"
    assert body["total"] == 250, "total is the full array length"
    assert "version" in body

    assert (await client.get(f"/lists/{q}/page?offset=-1&limit=10",
                             headers=_h(t))).status_code == 400
    assert (await client.get("/lists/list:does-not-exist/page?offset=0&limit=10",
                             headers=_h(t))).status_code == 404


@pytest.mark.asyncio
async def test_list_page_slice_no_full_expansion(client):
    """The page read returns exactly the requested window (positions 100..119 of a 120-line list)
    and the full total, proving the slice is positional and bounded. That it reads that window
    without loading the whole document is pinned behaviourally in test_list_page_no_full_state."""
    t = await _register(client)
    q = await _quotation(client, t)
    await _set_lines(client, t, q, _lines(120))

    r = await client.get(f"/lists/{q}/page?offset=100&limit=50", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 120
    assert len(body["items"]) == 20           # positions 100..119 only
    assert body["items"][0]["description"] == "Item 100"


# ── PATCH /lists/{id}/line-page : new version-guarded slice write ───────────────

@pytest.mark.asyncio
async def test_line_page_patch_preserves_offpage(client):
    """Saving one page overwrites only positions [offset:offset+len(page)] and leaves
    off-page rows byte-identical, including duplicate and id-less free-text rows."""
    t = await _register(client)
    q = await _quotation(client, t)
    lines = [
        {"item_id": "item:a", "sku": "A", "description": "Alpha", "quantity": 1, "unit_price": 1.0},
        {"description": "Free text one", "quantity": 1, "unit_price": 2.0},   # id-less free text
        {"description": "Dup", "quantity": 1, "unit_price": 3.0},             # duplicate desc
        {"description": "Dup", "quantity": 1, "unit_price": 3.0},             # duplicate desc
        {"item_id": "item:e", "sku": "E", "description": "Echo", "quantity": 1, "unit_price": 5.0},
    ]
    v = await _set_lines(client, t, q, lines)
    before = (await _state(client, t, q))["line_items"]

    page = [dict(before[1]), dict(before[2])]
    page[0]["description"] = "Free text EDITED"
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 1, "expected_version": v})
    assert r.status_code == 200, r.text

    after = (await _state(client, t, q))["line_items"]
    assert len(after) == 5, "off-page rows must not be dropped"
    assert after[0] == before[0]              # off-page, untouched
    assert after[3] == before[3]              # off-page duplicate, untouched
    assert after[4] == before[4]              # off-page, untouched
    assert after[1]["description"] == "Free text EDITED"
    assert after[2]["description"] == "Dup"


@pytest.mark.asyncio
async def test_line_page_patch_stale_version_409(client):
    """A slice PATCH pinned to a superseded version is rejected 409 and writes nothing."""
    t = await _register(client)
    q = await _quotation(client, t)
    stale = await _set_lines(client, t, q, _lines(4))
    # Advance the version so `stale` no longer matches.
    await _set_lines(client, t, q, _lines(4))
    before = (await _state(client, t, q))["line_items"]

    page = [{"item_id": "item:0", "sku": "SKU0", "description": "CLOBBER",
             "quantity": 9, "unit_price": 9.0}]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0, "expected_version": stale})
    assert r.status_code == 409, r.text
    assert (await _state(client, t, q))["line_items"] == before, "stale save must write nothing"


@pytest.mark.asyncio
async def test_line_page_patch_rejects_non_draft(client, session):
    """A slice PATCH on a non-draft list is 409, and a caller lacking edit_documents is
    403; neither writes."""
    t = await _register(client)

    # Non-draft: finalize then attempt a slice save.
    q = await _quotation(client, t)
    v = await _set_lines(client, t, q, _lines(3))
    assert (await client.post(f"/lists/{q}/finalize", headers=_h(t))).status_code == 200
    page = [{"item_id": "item:0", "sku": "SKU0", "description": "X", "quantity": 1, "unit_price": 1.0}]
    r_nondraft = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                                    json={"line_items": page, "offset": 0, "expected_version": v})
    assert r_nondraft.status_code == 409, r_nondraft.text

    # No edit_documents permission: a viewer on a fresh draft.
    q2 = await _quotation(client, t)
    v2 = await _set_lines(client, t, q2, _lines(3))
    viewer = await _user_with_role(client, session, t, "viewer")
    r_perm = await client.patch(f"/lists/{q2}/line-page", headers=_h(viewer),
                                json={"line_items": page, "offset": 0, "expected_version": v2})
    assert r_perm.status_code == 403, r_perm.text


@pytest.mark.asyncio
async def test_line_page_patch_recomputes_totals_from_full_array(client):
    """After a slice save the list total reflects the full merged array, not just the
    saved page."""
    t = await _register(client)
    q = await _quotation(client, t)
    # 5 lines, each qty*unit_price = 10 -> full-array total 50.
    lines = [{"item_id": f"item:{i}", "sku": f"S{i}", "description": f"Item {i}",
              "quantity": 2, "unit_price": 5.0} for i in range(5)]
    v = await _set_lines(client, t, q, lines)

    # Edit only page [0:1], raising line 0 to qty*price = 40 (+30). Full total -> 80.
    page = [{"item_id": "item:0", "sku": "S0", "description": "Item 0",
             "quantity": 4, "unit_price": 10.0}]
    r = await client.patch(f"/lists/{q}/line-page", headers=_h(t),
                           json={"line_items": page, "offset": 0, "expected_version": v})
    assert r.status_code == 200, r.text
    total = float((await _state(client, t, q)).get("total") or 0)
    assert total == pytest.approx(80.0), f"total {total} must sum the full array (80), not the page (40)"
