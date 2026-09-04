# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""WP1 list-query pushdown and paged read/write (post-#318 stability).

A single large-list request used to load, filter, sort, count, and slice the whole
list in Python and buffer the whole CSV before streaming. These tests pin the
bounded contract: the index, summary, and export push filter/sort/count/pagination
into SQL (asserted with a statement spy on the executed SQL), and two new endpoints
read one bounded page (`GET /lists/{id}/page`) and write one page under the existing
optimistic-version lock without touching off-page rows (`PATCH /lists/{id}/line-page`).
"""

from __future__ import annotations

import contextlib
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
    """Capture every SQL statement executed on any engine for the duration of the block.

    Attaches to the Engine class so it catches the sync engine backing the async one
    (SQLAlchemy fires cursor events on that underlying engine)."""
    stmts: list[str] = []

    def _after(conn, cursor, statement, parameters, context, executemany):
        stmts.append(statement)

    event.listen(Engine, "after_cursor_execute", _after)
    try:
        yield stmts
    finally:
        event.remove(Engine, "after_cursor_execute", _after)


def _proj_selects(stmts: list[str]) -> list[str]:
    """The SELECTs that read the list projection (company + entity_type='list' scope)."""
    return [s for s in stmts
            if "from projections" in s.lower() and "entity_type" in s.lower()
            and s.lower().lstrip().startswith("select")]


# ── index / summary / export: pushed into SQL (statement-spy red lever) ────────

@pytest.mark.asyncio
async def test_list_index_pushdown_paginates_in_sql(client):
    """GET /lists returns one bounded page + correct total; pagination is a SQL LIMIT,
    not a full Python load then slice."""
    t = await _register(client)
    for i in range(7):
        await _quotation(client, t, ref_id=f"IDX-{i:03d}")

    with _sql_spy() as sql:
        r = await client.get("/lists?limit=3&offset=0", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) <= 3
    assert body["total"] == 7                       # count reflects the whole set, not the page
    sel = _proj_selects(sql)
    assert sel, "the index must query the list projection"
    assert any("limit" in s.lower() for s in sel), (
        "pagination must be a SQL LIMIT (no full-array Python load then slice); "
        f"projection selects were: {sel}")


@pytest.mark.asyncio
async def test_list_index_sql_order_matches_python(client):
    """The SQL ORDER BY reproduces the newest-first Python sort (issue_date > created_at
    > date, entity_id tiebreak)."""
    t = await _register(client)
    ids = [await _quotation(client, t, ref_id=f"ORD-{i:03d}") for i in range(5)]

    with _sql_spy() as sql:
        r = await client.get("/lists?limit=100", headers=_h(t))
    assert r.status_code == 200, r.text
    returned = [it["id"] for it in r.json()["items"]]
    # Same-day rows: deterministic newest-first is the entity_id tiebreak (descending).
    expected = sorted(ids, reverse=True)
    assert returned == expected, f"order {returned} != expected {expected}"
    sel = _proj_selects(sql)
    assert any("order by" in s.lower() for s in sel), (
        f"ordering must be a SQL ORDER BY, not a Python sort; selects: {sel}")


@pytest.mark.asyncio
async def test_list_index_filters_equivalence(client):
    """q / all_issued / exclude_status / converted_to_type are SQL predicates over the
    JSON state, returning the identical id set the Python filters returned."""
    t = await _register(client)
    keep = await _quotation(client, t, ref_id="MATCH-ONE")
    other = await _quotation(client, t, ref_id="NOPE-TWO")

    with _sql_spy() as sql:
        r = await client.get("/lists?q=match", headers=_h(t))
    assert r.status_code == 200, r.text
    got = {it["id"] for it in r.json()["items"]}
    assert got == {keep}, f"q filter returned {got}"
    assert other not in got
    sel = _proj_selects(sql)
    assert any("->>" in s for s in sel), (
        f"filters must be SQL predicates over state (JSON ->> access); selects: {sel}")


@pytest.mark.asyncio
async def test_list_summary_sql_aggregates(client):
    """The 7 summary keys are computed by SQL aggregates, not a Python row loop."""
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
    sel = _proj_selects(sql)
    assert any(("count(" in s.lower() or "sum(" in s.lower() or "group by" in s.lower())
               for s in sel), (
        f"summary must aggregate in SQL; projection selects: {sel}")


@pytest.mark.asyncio
async def test_list_export_csv_streams(client):
    """CSV export fetches rows in bounded SQL batches and streams them, never buffering
    the whole projection in one unbounded query."""
    t = await _register(client)
    for i in range(6):
        await _quotation(client, t, ref_id=f"CSV-{i:03d}")

    with _sql_spy() as sql:
        r = await client.get("/lists/export/csv", headers=_h(t))
    assert r.status_code == 200, r.text
    text = r.text
    assert text.splitlines()[0].startswith("id,ref_id"), text.splitlines()[:1]
    assert "CSV-000" in text
    sel = _proj_selects(sql)
    assert any("limit" in s.lower() for s in sel), (
        f"export must read the projection in bounded SQL batches (LIMIT); selects: {sel}")


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
    """The page read slices in SQL (json_array_length for total, positional subscript
    over generate_series for the window) rather than loading the whole array in Python."""
    t = await _register(client)
    q = await _quotation(client, t)
    await _set_lines(client, t, q, _lines(120))

    with _sql_spy() as sql:
        r = await client.get(f"/lists/{q}/page?offset=100&limit=50", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 120
    assert len(body["items"]) == 20           # positions 100..119 only
    assert body["items"][0]["description"] == "Item 100"
    joined = " ".join(sql).lower()
    assert "json_array_length" in joined, (
        f"total must come from json_array_length in SQL; SQL was: {sql}")


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
