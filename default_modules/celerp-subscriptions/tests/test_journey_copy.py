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


def _toast(r) -> dict:
    import json
    return json.loads(r.headers["HX-Trigger"])["celerpToast"]


async def test_every_message_in_a_billing_journey_reads_a_day_one_way(owner_ui, monkeypatch):
    """Bill early while a period is due is refused naming its bill date; Generate due then
    names the period it billed. Both read their days the one way running text reads a day."""
    freeze_day(monkeypatch, "2026-08-15")
    eid = await agreement(owner_ui.api, {}, start="2026-08-04")
    early = _toast(await owner_ui.post(f"/subscriptions/{eid}/bill-early",
                                       data={"idempotency_key": "e-1", "period_index": "0"}))["message"]
    billed = _toast(await owner_ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "g-1"}))["message"]
    assert "4 Aug 2026" in early, early
    assert "4 Aug 2026" in billed and "3 Sep 2026" in billed, billed
    assert "2026-08-04" not in early + billed


def test_every_subscriptions_sentence_naming_a_day_reads_it_as_a_date():
    """Class guard: a subscriptions sentence that takes a day (date, start, end, day, or a
    spread of them) is built by sentence(), which reads each ISO day the way refusals do,
    never by t() with the raw day."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "ui/routes/subscriptions.py"
    raw = [f"{n.lineno}" for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
           if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "t"
           and any(k.arg in (None, "date", "start", "end", "day") for k in n.keywords)]
    assert not raw, raw


_CHANGED = {"anchor_date": "2026-04-05", "frequency": "weekly", "custom_interval_days": "10", "billing_timing": "arrears",
            "term_start": "2026-04-01", "term_end": "2027-04-01", "cancel_at_period_end": "true",
            "catchup_periods": "3", "generation_mode": "manual_due", "auto_send": "true"}


def _comparable(soup) -> str:
    """The page body without what a save is expected to change elsewhere (History, the
    per-render request keys)."""
    import re
    for el in soup.select("#doc-activity, .activity-section, [id^=activity], script"):
        el.decompose()
    html = re.sub(r"\s+", " ", str(soup.body)).replace("> <", "><")
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "KEY", html)


async def test_a_schedule_save_redraws_every_element_that_shows_the_schedule(owner_ui, monkeypatch):
    """Class guard: for every schedule and billing setting, the page with the save's answer
    swapped in (each element by its id) reads as the page does on reload. An element showing
    the schedule that the answer leaves out fails, whatever its id."""
    from celerp_subscriptions.routes import ScheduleBody

    assert set(_CHANGED) == set(ScheduleBody.model_fields)
    freeze_day(monkeypatch, "2026-03-15")
    for field, value in _CHANGED.items():
        eid = await agreement(owner_ui.api, {}, start="2026-04-01")
        r = await owner_ui.api.patch(f"/subscriptions/{eid}/schedule", json={"generation_mode": "auto_invoice"})
        assert r.status_code == 200, r.text
        page = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/{field}", data={"value": value})
        assert r.status_code == 200 and "error" not in r.headers.get("HX-Trigger", ""), (field, r.headers, r.text[:300])
        for el in BeautifulSoup(r.text, "html.parser").find_all(attrs={"hx-swap-oob": True}):
            del el["hx-swap-oob"]
            old = page.find(id=el["id"])
            assert old is not None, (field, el["id"])
            old.replace_with(el)
        fresh = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
        assert _comparable(page) == _comparable(fresh), field
