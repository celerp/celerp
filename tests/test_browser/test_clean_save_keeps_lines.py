# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An action on a list page saves the page's lines first. When nothing on the page was edited,
that save leaves the stored lines exactly as they were, for every list type and for lines made
by the app's own flows (scan, audit seed, write-off seed): no field is renamed, added or dropped."""
from __future__ import annotations

import uuid

import pytest

from celerp.services.list_behavior import LIST_TYPES

pytestmark = pytest.mark.browser


def _item(api, loc: str, **kw) -> str:
    r = api.post("/items", json={"status": "available", "sku": f"CS-{uuid.uuid4().hex[:8]}", "name": "Clean save item",
                                 "barcode": str(uuid.uuid4().int)[:12], "location_id": loc, **kw})
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json().get("entity_id")


def _seeded_list(api, kind: str) -> str:
    loc = str(api.post("/companies/me/locations", json={"name": f"Cs-{uuid.uuid4().hex[:6]}",
                                                           "type": "warehouse"}).json()["id"])
    a = _item(api, loc, sell_by="piece", quantity=5, pieces=5, weight=2.5, weight_unit="ct")
    b = _item(api, loc, sell_by="carat", quantity=3.2, weight=3.2, pieces=4)
    if kind == "audit":
        return api.post("/lists/audit", json={"location_id": loc}).json()["id"]
    if kind == "writeoff":
        return api.post("/lists/writeoff", json={"entity_ids": [a, b]}).json()["id"]
    r = api.post("/lists", json={"list_type": kind, "status": "draft", "line_items": []})
    lid = r.json().get("id") or r.json().get("entity_id")
    for eid in (a, b):
        s = api.post(f"/lists/{lid}/scan", json={"barcode": api.get(f"/items/{eid}").json().get("barcode")})
        assert s.status_code == 200, s.text
    return lid


def _line_changes(before: list[dict], after: list[dict]) -> list[dict]:
    """Per stored line, every field whose value differs (a field present on one side only included)."""
    if len(before) != len(after):
        return [{"line_count": (len(before), len(after))}]
    return [ch for x, y in zip(before, after)
            if (ch := {k: (x.get(k), y.get(k)) for k in sorted(set(x) | set(y)) if x.get(k) != y.get(k)})]


@pytest.mark.parametrize("kind", LIST_TYPES)
def test_a_clean_save_leaves_stored_lines_unchanged(page, ui_server, api, kind):
    lid = _seeded_list(api, kind)
    before = api.get(f"/lists/{lid}").json()["line_items"]
    assert before, kind
    page.goto(f"{ui_server}/lists/{lid}", wait_until="load")
    # The save every line action runs before it posts (persist_then_post).
    assert page.evaluate("async () => await _celerpPersist()") is not False
    after = api.get(f"/lists/{lid}").json()["line_items"]
    changes = _line_changes(before, after)
    assert changes == [], f"{kind}: {changes}"


def test_the_clean_save_check_sees_a_changed_line():
    """Self-check: a line set whose field changed, or that gained a field, is reported."""
    line = {"sku": "A", "quantity": 1}
    assert _line_changes([line], [{**line, "quantity": 2}]) == [{"quantity": (1, 2)}]
    assert _line_changes([line], [{**line, "name": "A"}]) == [{"name": (None, "A")}]
    assert _line_changes([line], [line]) == []
