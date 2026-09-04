# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Regression: the paged-read hot path must not materialize the whole list document.

`GET /lists/{id}/page` returns a small window of line_items. It must obtain the
header/version/total via bounded SQL only. If it also does a by-primary-key fetch of
the projection row (SQLAlchemy `session.get(Projection, pk)`), it drags the entire
`state` JSON (every line_item) back into Python on every page request, which is the
wasted DB round-trip this test pins out of existence.

The observable behavior asserted is DB TRAFFIC: during one small-page request against
a large list we capture every SQL statement executed on the engine and assert that the
unbounded by-PK full-row fetch of the projection (the SELECT whose select-list carries
the raw `projections.state` column for this list's entity_id) is ABSENT. The bounded
window/total queries (which reference `state -> ...` / `json_array_length`, never the
bare `state` column) may be present.
"""

from __future__ import annotations

import contextlib
import re
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


# ── helpers (mirror test_list_query_pushdown conventions) ──────────────────────

async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@nofullstate.test"
    r = await client.post("/auth/register", json={
        "company_name": "No Full State Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _quotation(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _lines(n: int) -> list[dict]:
    return [{"item_id": f"item:{i}", "sku": f"SKU{i}", "description": f"Item {i}",
             "quantity": 1, "unit_price": 1.0} for i in range(n)]


async def _set_lines(client, t, list_id, lines: list[dict]) -> int:
    v = (await client.get(f"/lists/{list_id}", headers=_h(t))).json()["version"]
    r = await client.patch(f"/lists/{list_id}", headers=_h(t),
                           json={"fields_changed": {"line_items": {"new": lines}},
                                 "expected_version": v})
    assert r.status_code == 200, r.text
    return r.json()["version"]


@contextlib.contextmanager
def _sql_spy():
    """Capture (statement, parameters) for every SQL statement executed on any engine
    during the block. Attaches to the Engine class so it catches the sync engine backing
    the async one, exactly as the pushdown tests do; this is the honest DB-traffic probe
    (a before_cursor_execute listener), not a monkeypatch of any application function."""
    captured: list[tuple[str, object]] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield captured
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


# The select-list of `session.get(Projection, pk)` carries the raw projection columns,
# among them the bare `projections.state` column. The bounded page queries never place
# the bare state column in their select list (they select json_array_length(...) and a
# positional subscript over state -> 'line_items'). So "a SELECT whose select-list, up to
# its FROM clause, contains the bare projections.state column reference" is precisely the
# full-document fetch being removed.
_BARE_STATE_COL = re.compile(r"\bprojections\.state\b")


def _full_state_fetches(captured, entity_id: str) -> list[str]:
    """The captured statements that are a by-PK full-row read of the projection: a SELECT
    over the projections table whose select-list (text before FROM) contains the bare
    projections.state column, scoped to this list's entity_id in its parameters."""
    hits: list[str] = []
    for statement, parameters in captured:
        low = statement.lower()
        if not low.lstrip().startswith("select"):
            continue
        if "from projections" not in low:
            continue
        select_list = statement[:low.index("from projections")]
        if not _BARE_STATE_COL.search(select_list):
            continue
        # This list's row is the target: the entity_id appears in the bound parameters.
        flat = _param_values(parameters)
        if any(entity_id == v for v in flat):
            hits.append(statement)
    return hits


def _param_values(parameters) -> list:
    if parameters is None:
        return []
    if isinstance(parameters, dict):
        return list(parameters.values())
    if isinstance(parameters, (list, tuple)):
        out = []
        for p in parameters:
            if isinstance(p, dict):
                out.extend(p.values())
            elif isinstance(p, (list, tuple)):
                out.extend(p)
            else:
                out.append(p)
        return out
    return [parameters]


@pytest.mark.asyncio
async def test_get_list_page_issues_no_full_state_query_on_large_list(client):
    """A small page of a large list must not trigger a by-PK fetch of the whole projection
    state row. Behavioral proof: the page request's captured DB traffic contains no
    unbounded full-`state` SELECT for the target list id (the wasted round-trip)."""
    t = await _register(client)
    q = await _quotation(client, t)
    await _set_lines(client, t, q, _lines(300))

    with _sql_spy() as captured:
        r = await client.get(f"/lists/{q}/page?offset=0&limit=50", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 300, "total is the full array length"
    assert len(body["items"]) == 50, "one bounded page only"

    full_fetches = _full_state_fetches(captured, q)
    assert not full_fetches, (
        "the page hot path must not fetch the whole list document: a by-primary-key "
        "SELECT returning the projection's full `state` row for this list ran during "
        f"the small-page request (wasted round-trip). Offending statements:\n"
        + "\n".join(full_fetches))
