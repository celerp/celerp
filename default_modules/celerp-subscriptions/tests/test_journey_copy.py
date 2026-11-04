# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What the agreement and list pages say after each step of a billing journey: the words
on a period, the dates in a message, what a bulk run did, and what the page redraws."""
from __future__ import annotations

import uuid

from bs4 import BeautifulSoup

from agreement_kit import agreement, freeze_day, generate_due


def _key() -> dict:
    return {"idempotency_key": str(uuid.uuid4())}


async def _period_row(ui, eid: str, k: int) -> str:
    """Period ``k``'s row in the agreement page's Periods table, as text."""
    page = BeautifulSoup((await ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    rows = page.select("#sub-periods tbody tr")
    start = next(r for r in (await ui.api.get(f"/subscriptions/{eid}/periods")).json()["rows"]
                 if r["period_index"] == k)["service_start"]
    return next(r.get_text(" ", strip=True) for r in rows if r.get("data-period") == str(k) or start in r.get_text())


async def test_only_a_period_whose_document_was_let_go_reads_reopened(owner_ui, monkeypatch):
    """Un-skipping a period opens it again; nothing was voided, so it does not read Reopened.
    Reopen after a void does."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    assert (await owner_ui.api.post(f"/subscriptions/{eid}/periods/2/skip", json={"reason": "closed"})).status_code == 200
    assert (await owner_ui.api.post(f"/subscriptions/{eid}/periods/2/unskip")).status_code == 200
    assert "Reopened" not in await _period_row(owner_ui, eid, 2)
    doc_id = (await generate_due(owner_ui.api, {}, eid)).json()["doc_id"]
    assert (await owner_ui.api.post(f"/docs/{doc_id}/void", json={"reason": "x", **_key()})).status_code == 200
    assert (await owner_ui.api.post(f"/subscriptions/{eid}/periods/0/reopen")).status_code == 200
    assert "Reopened" in await _period_row(owner_ui, eid, 0)


def test_every_subscriptions_table_scrolls_inside_its_own_box():
    """Class guard: a data table on the subscription pages is drawn only by _data_table, which
    puts it in the scroll box every data table has, so a wide row scrolls instead of pushing
    the page past the screen."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "ui/routes/subscriptions.py"
    outside = []
    for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != "_data_table":
            outside += [f"{fn.name}:{n.lineno}" for n in ast.walk(fn)
                        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Table"]
    assert not outside, outside


async def test_the_agreement_page_tables_sit_in_a_scroll_box(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    page = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    tables = page.select("#sub-periods table.data-table")
    assert tables and all(t.parent.get("class") == ["table-scroll-wrap"] for t in tables)


async def test_a_draft_points_from_its_action_bar_to_the_schedule(owner_ui, monkeypatch):
    """A new draft cannot be activated until its schedule is set, so the action bar beside
    Activate takes the user to the Schedule section."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-04-01", activate=False)
    page = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    activate = page.find("button", attrs={"hx-post": f"/subscriptions/{eid}/activate"})
    link = activate.parent.find("a", href="#sub-schedule")
    assert link is not None and page.find(id="sub-schedule") is not None, activate.parent
    assert link.get_text(strip=True) == "Set the schedule"
