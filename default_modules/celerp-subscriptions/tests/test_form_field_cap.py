# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""No subscriptions form posts one field per row.

The form parser stops at 1000 fields, so a form whose fields grow with its rows fails once a
page lists enough of them, with nothing done. A bulk table's selection therefore posts as one
field (the ticked rows as a JSON list), as Mark as read posts its keys. The guard reads every
form the subscriptions UI builds from the code, renders each with 3 and with 1001 rows, and
requires the same fields at both sizes: a new form fails until it is rendered here, and a form
whose fields grow with its rows fails outright.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup

import ui.routes.subscriptions as subs_ui

_ROOT = pathlib.Path(subs_ui.__file__).parent


def _form_builders() -> set[str]:
    """Every function of the subscriptions UI routes that builds a form (Form, or _bulk_form
    over a bulk table)."""
    found = set()
    for path in _ROOT.glob("subscriptions*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(c, ast.Call) and getattr(c.func, "id", "") in ("Form", "_bulk_form")
                    for c in ast.walk(fn)) and fn.name != "_bulk_form":
                found.add(fn.name)
    return found


def _period(k: int) -> dict:
    return {"period_index": k, "service_start": "2026-01-01", "service_end": "2026-01-07", "bill_date": "2026-01-01"}


def _subs(n: int) -> dict[str, dict]:
    return {f"doc:SUB-{i}": {"id": f"doc:SUB-{i}", "status": "active", "total": 10, "currency": "USD"} for i in range(n)}


def _due(n: int) -> dict:
    return {"items": [{"entity_id": eid, "periods": [_period(0)]} for eid in _subs(n)]}


def _catch_up(n: int) -> list[dict]:
    """*n* held periods: agreements of 11 periods each, as a server off for weeks leaves them."""
    eids = list(_subs(-(-n // 11)))
    return [{"entity_id": eid, "periods": [_period(k) for k in range(11)][:n - 11 * i]} for i, eid in enumerate(eids)]


# Each form builder, drawn with n rows.
RENDERERS = {
    "_due_panel": lambda n: subs_ui._due_panel(_due(n), _subs(n)),
    "_catch_up_card": lambda n: subs_ui._catch_up_card(_catch_up(n), _subs(-(-n // 11))),
    # One form per period row: its own fields never depend on how many rows the table has.
    "_period_actions": lambda n: subs_ui._period_actions("doc:SUB-0", {"period_index": n, "state": "open"}),
}


def _posted(form) -> list[tuple[str, str]]:
    """What a browser posts for *form*: its named controls (ticked boxes only), then the hx-vals
    on the form and its controls, and the chosen rows of a bulk form as its script sets them."""
    fields = [(c["name"], c.get("value", "")) for c in form.find_all(["input", "select", "textarea"])
              if c.get("name") and (c.get("type") != "checkbox" or c.has_attr("checked"))]
    for el in [form, *form.find_all(attrs={"hx-vals": True})]:
        if el.get("hx-vals"):
            fields.extend((k, str(v)) for k, v in json.loads(el["hx-vals"]).items())
    if form.get("data-chosen-rows"):
        assert any(subs_ui._CHOSEN_ROWS_JS.strip() in js.get_text() for js in form.find_all("script"))
        fields.append((form["data-chosen-rows"], json.dumps(
            [c["value"] for c in form.select(".bulk-select") if c.has_attr("checked")])))
    return fields


def _forms(html: str) -> list:
    return BeautifulSoup(html, "html.parser").find_all("form")


def _render(name: str, n: int) -> list:
    from fasthtml.common import to_xml
    return _forms(to_xml(RENDERERS[name](n)))


def test_every_form_the_subscriptions_ui_builds_is_rendered_by_the_guard():
    assert _form_builders() == set(RENDERERS)


@pytest.mark.parametrize("name", sorted(RENDERERS))
def test_a_form_posts_the_same_fields_at_1001_rows_as_at_3(name):
    small, large = _render(name, 3), _render(name, 1001)
    assert small and len(small) == len(large)
    for a, b in zip(small, large):
        assert [k for k, _ in _posted(a)] == [k for k, _ in _posted(b)], name
        assert len(_posted(b)) < 10


def test_no_subscriptions_route_reads_a_field_posted_once_per_row():
    for path in _ROOT.glob("subscriptions*.py"):
        assert not re.search(r"\.getlist\(", path.read_text()), path.name


async def test_select_all_on_a_catch_up_page_of_1001_periods_bills_all_of_them(owner_ui, monkeypatch):
    """Select all, then Bill, on a page of 1001 held periods, posted as the browser posts it: every
    period goes to the API in batches of 50, and none is refused for the size of the form."""
    import ui.api_client as api
    items = _catch_up(1001)

    async def held(token):
        return {"items": items}

    async def agreements(token, direction=None):
        return _subs(len(items))
    batches = []

    async def run(token, batch):
        batches.append(len(batch))
        return {"results": [{**i, "ok": True, "skipped": True, **_period(i["period_index"])} for i in batch]}
    monkeypatch.setattr(api, "subscriptions_catch_up", held)
    monkeypatch.setattr(subs_ui, "_agreements", agreements)
    monkeypatch.setattr(api, "run_subscriptions_catch_up", run)
    form = _forms((await owner_ui.get("/subscriptions/catch-up")).text)[0]
    data = _posted(form)
    assert len(json.loads(dict(data)["item"])) == 1001
    r = await owner_ui.post("/subscriptions/catch-up/run", content=urlencode(data),
                            headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert sum(batches) == 1001 and max(batches) == 50


async def test_generate_all_due_on_1001_agreements_sends_every_ticked_one(owner_ui, monkeypatch):
    import ui.api_client as api

    async def due(token):
        return _due(1001)

    async def agreements(token, direction=None):
        return _subs(1001)
    sent = []

    async def generate(token, items):
        sent.extend(items)
        return {"billed": 0, "refused": 0, "results": []}
    monkeypatch.setattr(api, "subscriptions_due", due)
    monkeypatch.setattr(subs_ui, "_agreements", agreements)
    monkeypatch.setattr(api, "generate_all_due", generate)
    form = _forms((await owner_ui.get("/subscriptions/due")).text)[0]
    data = _posted(form)
    r = await owner_ui.post("/subscriptions/generate-due", content=urlencode(data),
                            headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert len(sent) == 1001 and sent[0]["entity_id"] == "doc:SUB-0"


async def test_retry_on_a_refused_row_posts_its_period_as_the_one_item_field(owner_ui, monkeypatch):
    """Retry sits outside the form, so it carries its period itself, in the field's one shape."""
    from fasthtml.common import to_xml
    import ui.api_client as api
    refused = {"entity_id": "doc:SUB-0", "period_index": 3, "ok": False, "error": "paused"}
    button = BeautifulSoup(to_xml(subs_ui._outcome_table([refused], _subs(1), "k-retry")), "html.parser").find(
        attrs={"hx-post": "/subscriptions/generate-due"})
    sent = []

    async def generate(token, items):
        sent.extend(items)
        return {"billed": 0, "refused": 0, "results": []}
    monkeypatch.setattr(api, "generate_all_due", generate)
    r = await owner_ui.post("/subscriptions/generate-due", data=json.loads(button["hx-vals"]))
    assert r.status_code == 200 and [(i["entity_id"], i["period_index"]) for i in sent] == [("doc:SUB-0", 3)]
