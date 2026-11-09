# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every route a document or list page saves a field through, refused for real and saved for real
(tests/test_saved_mark_routes.py keeps this table equal to the routes the pages save to).

An action clicked straight from the field waits for that field's save. A refusal, whatever shape
the route answers it in, sends nothing: the reason is said in a toast and the stored value stays.
A save that lands sends the action once, after it. Refusals come from the API itself, by a value
it refuses or by a change made to the record behind the page, never from a faked answer."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import pytest

from .conftest import answer_confirms
from .test_line_action_guards import _finalized_audit
from .test_list_mutation_serialization import _quotation
from .test_save_first_self_saving_fields import _NOT_SAVED, _type_qty, _write_off_list

pytestmark = pytest.mark.browser

SAVED_MARK = "x-celerp-saved"


@dataclass(frozen=True)
class Scenario:
    open: Callable  # (page, ui_server, api) -> ctx: the record made and its page open, ctx["stored"] read
    edit: Callable  # (page, ctx, value) -> the value now in the field, its save on its way or due
    act: Callable  # (page) -> the action clicked straight from the field
    sent: tuple[str, str]  # the action's request: method, URL ending
    stored: Callable  # (api, ctx) -> the field's stored value, None once the record is gone
    saved: str  # a value the API stores
    refused: str | None = None  # a value the API refuses; None: `refuse` makes the save refused
    refuse: Callable | None = None  # (api, ctx): a change behind the page the save is then refused for
    reason: str | Callable = _NOT_SAVED  # the toast, or (page) -> it
    kept: bool = True  # the stored value is still readable once the action ran


def _invoice(api, status: str = "draft") -> str:
    r = api.post("/docs", json={"doc_type": "invoice", "status": "draft", "issue_date": "2026-11-01",
                                "due_date": "2026-12-01", "line_items": [
                                    {"name": "W", "description": "Before", "quantity": 1, "unit_price": 100.0,
                                     "line_total": 100.0}]})
    assert r.status_code in (200, 201), r.text
    path = f"/docs/{r.json()['id']}"
    if status != "draft":
        assert api.post(f"{path}/finalize").status_code in (200, 201)
    return path


def _goto(page, ui_server, api, path: str, stored: Callable, **ctx) -> dict:
    answer_confirms(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    ctx["path"] = path
    ctx["stored"] = stored(api, ctx)
    return ctx


def _get(api, path: str):
    r = api.get(path)
    return r.json() if r.status_code == 200 else None


def _doc(field: str):
    def stored(api, ctx):
        rec = _get(api, ctx["path"])
        return rec and (rec.get(field) or "")[:10 if field.endswith("_date") else None]
    return stored


def _fill(opener: str, field: str, dblclick: bool = False):
    def edit(page, ctx, value):
        if opener:
            cell = page.locator(opener).first
            cell.dblclick() if dblclick else cell.click()
        el = page.locator(field).first
        el.wait_for(timeout=5000)
        el.fill(value)
        return value
    return edit


def _click(selector: str):
    return lambda page: page.locator(selector).first.click()


_ISSUE = _click("button[hx-post$='/action/finalize']")
_ISSUED = ("POST", "/action/finalize")


def _line_description(api, ctx):
    lines = (_get(api, ctx["path"]) or {}).get("line_items")
    return lines[0].get("description") if lines else None


def _lines_removed(api, ctx):
    """Back to draft with no lines: the page still shows line 1, the route finds none to save."""
    assert api.post(f"{ctx['path']}/revert-to-draft", json={}).status_code in (200, 201)
    doc = api.get(ctx["path"]).json()
    r = api.patch(ctx["path"], json={"fields_changed": {"line_items": {"old": doc["line_items"], "new": []}},
                                     "expected_version": doc["version"]})
    assert r.status_code == 200, r.text
    ctx["stored"] = None


def _open_void(page, ui_server, api):
    ctx = _goto(page, ui_server, api, _invoice(api, "final"), _line_description)
    page.locator(".void-section summary", has_text="Void").first.click()
    page.locator("form[hx-post$='/action/void'] input[name='reason']").fill("Guard")
    return ctx


def _list_terms(api, ctx):
    rec = _get(api, ctx["path"])
    return rec and (rec.get("terms_text") or "")


def _finalize(api, ctx):
    assert api.post(f"{ctx['path']}/finalize").status_code in (200, 201)


def _counted(api, ctx):
    rec = _get(api, ctx["path"])
    return rec and next(li for li in rec["line_items"] if ctx["item"] in (li.get("item_id"), li.get("entity_id"))).get("counted_qty")


def _open_count(page, ui_server, api):
    path = f"/lists/{_finalized_audit(page, ui_server, api)}"
    answer_confirms(page)
    page.goto(f"{ui_server}{path}", wait_until="load")
    url = page.locator("[hx-get$='/counted/edit']").first.get_attribute("hx-get")
    ctx = {"path": path, "item": url.split("/line/")[1].split("/counted")[0]}
    ctx["stored"] = _counted(api, ctx)
    return ctx


def _edit_count(page, ctx, value):
    page.locator("[hx-get$='/counted/edit']").first.click()
    page.locator("input[name='counted_qty']").first.fill(value)
    return float(value)


def _write_off_qty(api, ctx):
    rec = _get(api, ctx["path"])
    return rec and float(rec["line_items"][0].get("qty_out") or 0)


def _open_write_off(page, ui_server, api):
    _write_off_list(page, ui_server, api, f"WO-GUARD-{time.time_ns()}")
    ctx = {"path": "/lists/" + page.url.split("/lists/")[1].split("?")[0]}
    ctx["stored"] = _write_off_qty(api, ctx)
    return ctx


def _edit_write_off(page, ctx, value):
    _type_qty(page, value)
    return float(value)


def _file_tag(api, ctx):
    rec = _get(api, ctx["path"])
    return rec and next((f.get("document_tag") or "" for f in rec.get("files") or [] if f["id"] == ctx["file"]), "")


def _open_files(page, ui_server, api):
    path = _invoice(api)
    r = api.post(f"{path}/files", files={"file": ("guard.txt", b"guard", "text/plain")})
    assert r.status_code in (200, 201), r.text
    ctx = _goto(page, ui_server, api, path, _file_tag, file=r.json()["id"])
    page.locator("details.doc-internal summary", has_text="Files").first.click()
    return ctx


def _edit_tag(page, ctx, value):
    select = page.locator("select.file-tag-select").first
    select.wait_for(timeout=5000)
    tag = select.locator("option").nth(1).get_attribute("value")
    select.select_option(tag)
    return tag


def _delete_file(api, ctx):
    assert api.delete(f"{ctx['path']}/files/{ctx['file']}").status_code in (200, 204)


def _share_expiry(api, ctx):
    rec = _get(api, f"{ctx['path']}/share/state")
    return rec and rec.get("expires_at")


def _open_share(page, ui_server, api):
    path = _invoice(api, "final")
    assert api.post(f"{path}/share", json={"expires_at": "2027-01-31"}).status_code in (200, 201)
    ctx = _goto(page, ui_server, api, path, _share_expiry)
    # The page offers Share only once the instance is linked to the relay; the panel is the route's own,
    # in the body its controls answer into.
    body = "share-body-" + path.rsplit("/", 1)[1].replace(":", "-")
    page.evaluate("([p, b]) => { const d = document.createElement('div'); d.id = b; document.body.append(d);"
                  " return htmx.ajax('GET', p + '/share', '#' + b); }", [path, body])
    page.locator(f"#{body} input[name='expires_at']").wait_for(timeout=5000)
    return ctx


_STOP = "stop"  # the agreement set to stop at the end of its period, which a draft refuses


def _anchor(api, ctx):
    return (_get(api, f"/docs/{ctx['eid']}") or {}).get("anchor_date")


def _open_agreement(page, ui_server, api):
    from .test_subscriptions_e2e import _draft
    eid = _draft(api, {"unit_price": 100.0, "line_total": 100.0, "taxes": []})
    return _goto(page, ui_server, api, f"/subscriptions/{eid}", _anchor, eid=eid)


def _edit_schedule(page, ctx, value):
    if value == _STOP:
        page.locator("select[hx-patch$='/schedule/cancel_at_period_end']").select_option("true")
    else:
        page.locator("input[hx-patch$='/schedule/anchor_date']").fill(value)
    return value


SCENARIOS: dict[tuple[str, str], Scenario] = {
    ("PATCH", "/docs/{entity_id}/field/{field}"): Scenario(
        open=lambda page, ui, api: _goto(page, ui, api, _invoice(api), _doc("due_date")),
        edit=_fill("[hx-get$='/field/due_date/edit']", "input[hx-patch$='/field/due_date']"),
        act=_ISSUE, sent=_ISSUED, stored=_doc("due_date"),
        saved="2027-03-15", refused="2026-10-01",
        reason="The due date cannot be earlier than the issue date."),
    ("PATCH", "/docs/{entity_id}/line/{li_index}/field/{field}"): Scenario(
        open=_open_void,
        edit=_fill("[hx-get$='/line/0/field/description/edit']", "input[hx-patch$='/line/0/field/description']", True),
        act=_click("form[hx-post$='/action/void'] button[type='submit']"), sent=("POST", "/action/void"),
        stored=_line_description, saved="After", refuse=_lines_removed,
        reason="Celerp couldn't find this line. It may have been deleted. Select Try again."),
    ("POST", "/docs/{entity_id}/field/{field}"): Scenario(
        open=lambda page, ui, api: _goto(page, ui, api, _invoice(api), _doc("terms_text")),
        edit=_fill("", "textarea[name='terms_text']"),
        act=_ISSUE, sent=_ISSUED, stored=_doc("terms_text"),
        saved="Net 30, saved", refuse=_finalize),
    ("POST", "/lists/{entity_id}/field/{field}"): Scenario(
        open=lambda page, ui, api: _goto(page, ui, api, f"/lists/{_quotation(api)}", _list_terms),
        edit=_fill("", "textarea[name='terms_text']"),
        act=_ISSUE, sent=_ISSUED, stored=_list_terms,
        saved="Valid 14 days", refuse=_finalize),
    ("PATCH", "/lists/{entity_id}/field/{field}"): Scenario(
        open=lambda page, ui, api: _goto(page, ui, api, f"/lists/{_quotation(api)}", _doc("issue_date")),
        edit=_fill("[hx-get$='/field/issue_date/edit']", ".editable-cell--editing input[type='date']"),
        act=_ISSUE, sent=_ISSUED, stored=_doc("issue_date"),
        saved="2026-10-15", refuse=_finalize,
        reason="This list is no longer a draft, so it can't be edited. Revert it to Draft first."),
    ("POST", "/lists/{entity_id}/line/{item_id}"): Scenario(
        open=_open_count, edit=_edit_count,
        act=_click("button[hx-post$='/action/adjust']"), sent=("POST", "/action/adjust"), stored=_counted,
        saved="1", refuse=lambda api, ctx: api.post(f"{ctx['path']}/revert-to-draft", json={}).raise_for_status(),
        reason="Finalize the stock count before entering counted quantities."),
    ("PATCH", "/lists/{entity_id}/writeoff-line/{line_id}/{field}"): Scenario(
        open=_open_write_off, edit=_edit_write_off,
        act=lambda page: page.get_by_role("button", name="Write off stock").click(),
        sent=("POST", "/action/write-off"), stored=_write_off_qty,
        saved="5", refused="9",
        reason=lambda page: page.locator("td.col-qtyout .cell-error").first.inner_text().strip()),
    ("POST", "/docs/{entity_id}/files/{file_id}/tag"): Scenario(
        open=_open_files, edit=_edit_tag, act=_ISSUE, sent=_ISSUED, stored=_file_tag,
        saved="", refuse=_delete_file),
    ("POST", "/docs/{entity_id}/share"): Scenario(
        open=_open_share, edit=_fill("", "input[name='expires_at']"),
        act=_click("button[hx-delete$='/share']"), sent=("DELETE", "/share"), stored=_share_expiry,
        saved="2027-06-30", refused="2020-01-01",
        reason="The expiry date has already passed. Choose a future date.", kept=False),
    ("PATCH", "/subscriptions/{entity_id}/schedule/{field}"): Scenario(
        open=_open_agreement, edit=_edit_schedule,
        act=lambda page: page.get_by_role("button", name="Activate", exact=True).click(),
        sent=("POST", "/activate"), stored=_anchor, saved="2027-04-15", refused=_STOP,
        reason="A draft has no current period yet."),
}


def _watch(page) -> list:
    """Every non-GET request of the page, and each answer that carries the saved mark, in order."""
    log: list = []
    page.on("request", lambda q: q.method != "GET" and log.append(("sent", q.method, q.url)))
    page.on("response", lambda r: r.request.method != "GET" and r.headers.get(SAVED_MARK)
            and log.append(("saved", r.request.method, r.url)))
    return log


def _actions(log: list, sent: tuple[str, str]) -> list[int]:
    return [i for i, (kind, method, url) in enumerate(log) if kind == "sent" and method == sent[0]
            and url.split("?")[0].endswith(sent[1])]


@pytest.mark.parametrize("key", list(SCENARIOS), ids=lambda k: f"{k[0]} {k[1]}")
def test_a_refused_field_save_stops_the_action_with_its_reason(page, ui_server, api, key):
    s = SCENARIOS[key]
    ctx = s.open(page, ui_server, api)
    if s.refuse:
        s.refuse(api, ctx)
    before = ctx["stored"]
    log = _watch(page)
    s.edit(page, ctx, s.refused if s.refused is not None else s.saved)
    s.act(page)
    toast = page.locator(".toast--error .toast__msg").first
    toast.wait_for(timeout=8000)
    page.wait_for_timeout(1500)  # the action would have been confirmed and sent by now
    reason = s.reason(page) if callable(s.reason) else s.reason
    assert reason and toast.inner_text().strip().startswith(reason), (toast.inner_text(), reason)
    assert _actions(log, s.sent) == [], log
    assert not any(kind == "saved" for kind, *_ in log), log
    assert s.stored(api, ctx) == before


@pytest.mark.parametrize("key", list(SCENARIOS), ids=lambda k: f"{k[0]} {k[1]}")
def test_a_stored_field_save_sends_the_action_once_after_it(page, ui_server, api, key):
    s = SCENARIOS[key]
    ctx = s.open(page, ui_server, api)
    log = _watch(page)
    value = s.edit(page, ctx, s.saved)
    s.act(page)
    deadline = time.time() + 10
    while time.time() < deadline and not _actions(log, s.sent):
        page.wait_for_timeout(100)
    page.wait_for_timeout(1000)
    sent = _actions(log, s.sent)
    marked = [i for i, (kind, *_) in enumerate(log) if kind == "saved"]
    assert len(sent) == 1 and marked and marked[0] < sent[0], log
    if s.kept:
        assert s.stored(api, ctx) == value


class _SwappedOut:
    """The document field route as it is, except that an answer without the saved mark becomes a bare
    empty 200: no toast, and the swap takes the field out of the page."""

    def __init__(self, route):
        self.route = route

    async def __call__(self, scope, receive, send):
        from starlette.responses import Response
        answer: list = []

        async def keep(message):
            answer.append(message)
        await self.route.app(scope, receive, keep)
        if any(k.lower() == SAVED_MARK.encode() for k, _ in answer[0]["headers"]):
            for message in answer:
                await send(message)
        else:
            await Response("", 200)(scope, receive, send)


@pytest.fixture()
def refusal_swaps_the_field_out():
    from starlette.routing import Route
    from ui.app import app as ui_app
    path = "/docs/{entity_id}/field/{field}"
    routes = list(ui_app.router.routes)
    real = next(r for r in routes if getattr(r, "path", "") == path and "PATCH" in (r.methods or ()))
    ui_app.router.routes.insert(0, Route(path, _SwappedOut(real), methods=["PATCH"]))
    try:
        yield
    finally:
        ui_app.router.routes[:] = routes


def test_a_refusal_that_takes_the_field_out_of_the_page_stops_the_action(
        page, ui_server, api, refusal_swaps_the_field_out):
    """How a save ended is read from its own request: a refusal whose answer removes the field, so
    nothing is left on the page to say so, still sends nothing."""
    s = SCENARIOS[("PATCH", "/docs/{entity_id}/field/{field}")]
    ctx = s.open(page, ui_server, api)
    log = _watch(page)
    s.edit(page, ctx, s.refused)
    s.act(page)
    toast = page.locator(".toast--error .toast__msg").first
    toast.wait_for(timeout=8000)
    page.wait_for_timeout(1500)  # the action would have been confirmed and sent by now
    assert toast.inner_text().strip().startswith(_NOT_SAVED), toast.inner_text()
    assert page.locator("input[hx-patch$='/field/due_date']").count() == 0
    assert _actions(log, s.sent) == [], log
    assert s.stored(api, ctx) == ctx["stored"]


def test_a_refused_save_from_a_control_that_is_not_a_form_field_stops_the_action(page, ui_server, api):
    """A field's save is told by its request, whatever element sent it: a refused save sent from the
    field's display cell, its answer swapped nowhere, still sends nothing."""
    s = SCENARIOS[("PATCH", "/docs/{entity_id}/field/{field}")]
    ctx = s.open(page, ui_server, api)
    log = _watch(page)
    page.evaluate("""([url, value]) => htmx.ajax('PATCH', url, {
        source: document.querySelector("[hx-get$='/field/due_date/edit']"), values: {value}, swap: 'none'})""",
                  [f"{ctx['path']}/field/due_date", s.refused])
    s.act(page)
    toast = page.locator(".toast--error .toast__msg").first
    toast.wait_for(timeout=8000)
    page.wait_for_timeout(1500)  # the action would have been confirmed and sent by now
    assert toast.inner_text().strip().startswith(s.reason), toast.inner_text()
    assert _actions(log, s.sent) == [], log
    assert s.stored(api, ctx) == ctx["stored"]
