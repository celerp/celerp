# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every subscriptions table fits the page at 1440 px with the longest sentence any of its cells can
hold: a sentence wraps (cell--note) and the last column's header and cells stay in view."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

pytestmark = pytest.mark.browser

_ROOT = Path(__file__).resolve().parents[2]
_CSS = (_ROOT / "ui" / "static" / "app.css").read_text()
_PAGE = _ROOT / "ui" / "routes" / "subscriptions.py"
_REF = "SUBI-2611-0014"


def _longest_sentence() -> str:
    """The longest subscriptions sentence in any language, its parameters filled."""
    texts = (v for f in (_ROOT / "ui" / "locales").glob("*.json")
             for k, v in json.loads(f.read_text()).items() if k.startswith("subscriptions."))
    return re.sub(r"\{\w+\}", _REF, max(texts, key=len))


def _tables() -> set[str]:
    """Every function of the subscriptions pages that draws a data table."""
    tree = ast.parse(_PAGE.read_text())
    return {f.name for f in tree.body if isinstance(f, ast.FunctionDef)
            and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_data_table" for c in ast.walk(f))}


def _drawn() -> dict:
    import ui.routes.subscriptions as page
    from ui.i18n import t

    long, eid = _longest_sentence(), "doc:agreement"
    period = {"period_index": 0, "service_start": "2026-07-01", "service_end": "2027-06-30", "bill_date": "2026-07-01"}
    subs = {eid: {"id": eid, "name": "Annual membership", "contact_name": "Member", "frequency": "annually",
                  "status": "active", "total": 120, "currency": "USD", "next_bill_date": "2026-07-01"}}
    items = [{"entity_id": eid, "periods": [period, {**period, "period_index": 1}]}]
    return {
        "_sub_table": lambda: page._sub_table([{**subs[eid], "schedule_error": long}], "sales"),
        "_periods_table": lambda: page._periods_table(eid, {"rows": [
            {**period, "status": "due", "reason": long}, {**period, "status": "billed", "state": "billed",
                                                          "doc_id": "doc:bill", "ref_id": _REF, "send": "failed",
                                                          "attempt": 1}]}),
        "_due_panel": lambda: page._due_panel({"items": items}, subs),
        "_outcome_table": lambda: page._outcome_table(
            [{**period, "entity_id": eid, "ok": False, "error": long},
             {**period, "entity_id": eid, "ok": True, "doc_id": "doc:bill", "ref_id": _REF}], subs, "key"),
        "_catch_up_card": lambda: page._catch_up_card(items, subs),
        "_summary_table": lambda: page._summary_table([
            {"entity_id": eid, "kind": "generation_failed", "period_index": 0, "at": "2026-07-01T00:00:00Z",
             "message": t("subscriptions.summary_generation_failed_because", ref=_REF, error=long)},
            {"entity_id": eid, "kind": "sent", "at": "2026-07-01T00:00:00Z", "message": long,
             "doc_id": "doc:bill", "ref_id": _REF}], subs),
        "_history_review": lambda: page._history_review(
            eid, [{"doc_id": "doc:old", "ref_id": _REF, "candidates": [period, {**period, "period_index": 1}]},
                  {"doc_id": "doc:none", "ref_id": _REF, "candidates": []}],
            [{"doc_id": "doc:aside", "ref_id": _REF}]),
    }


def test_every_subscriptions_table_is_drawn_here():
    """A new table on the subscriptions pages fails until it is drawn below."""
    assert _tables() == set(_drawn())


@pytest.mark.parametrize("table", sorted(_tables()))
def test_the_last_column_stays_in_view_at_1440(browser, table):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.set_content(f"<style>{_CSS}</style><div style='width:1164px'>{to_xml(_drawn()[table]())}</div>")
        out = page.evaluate("""() => [...document.querySelectorAll('.table-scroll-wrap')].map(w => {
            const right = w.getBoundingClientRect().right, t = w.querySelector('table');
            const last = [...t.querySelectorAll('tr')].map(r => r.lastElementChild.getBoundingClientRect().right);
            return {right, scroll: t.scrollWidth - w.clientWidth, past: Math.max(...last) - right};
        })""")
    finally:
        page.close()
    assert out, f"{table} drew no table"
    for box in out:
        assert box["right"] <= 1440 and box["scroll"] <= 0 and box["past"] <= 0, f"{table}: {box}"
