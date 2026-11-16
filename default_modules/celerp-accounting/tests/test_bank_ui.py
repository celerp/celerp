# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The bank page, the bank list and the client's Questions page as rendered HTML: the
drop zone a bank with no statement opens on, the line row, and what Escape does on
every bank input. API answers are mocked; the API is covered in test_bank_statements.py."""

from __future__ import annotations

import json
import re
from urllib.parse import quote
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

pytestmark = pytest.mark.asyncio

_BANK = {"id": "b1", "bank_name": "Main Bank", "account_number": "****1234", "currency": "USD", "bank_type": "checking",
         "chart_account_code": "1111", "balance": 0.0, "opening_balance": 0.0, "is_active": True}
_LINE = {"id": "l1", "statement_id": "s1", "line_date": "2026-01-05", "amount": -200.0, "description": "WITHDRAWAL",
         "reference": None, "raw_balance": 800.0, "counterparty_account": "4471", "status": "unmatched",
         "matched_je_ids": [], "client_query": None, "attachments": [],
         "suggestion": {"kind": "post", "account_code": "5100", "source": "history", "reason": "Posted to 5100 twice",
                        "contact_id": None}}
_TABLE = {"groups": [{"key": "SPARSE:WITHDRAWAL:out", "direction": "out", "count": 1, "total": -200.0, "ticked": False,
                      "suggestion": _LINE["suggestion"], "lines": [_LINE]}],
          "summary": {"lines": 1, "open": 1, "posted": 0}, "statements": [
              {"id": "s1", "period_start": "2026-01-01", "statement_date": "2026-01-31", "opening_balance": 1000.0,
               "closing_balance": 800.0, "status": "in_progress"}],
          "suspense_count": 0, "difference": {"amount": 0.0, "notes": []}}
_EMPTY = {"groups": [], "summary": {"lines": 0, "open": 0, "posted": 0}, "statements": [], "suspense_count": 0,
          "difference": {"amount": 0.0, "notes": []}}


_COMPANY = {"id": "c1", "name": "TestCo", "currency": "USD", "settings": {"currency": "USD"}}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _api(table):
    return [patch("ui.api_client.get_bank_account", AsyncMock(return_value=_BANK)),
            patch("ui.api_client.get_bank_accounts", AsyncMock(return_value={"items": [_BANK]})),
            patch("ui.api_client.bank_groups", AsyncMock(return_value=table)),
            patch("ui.api_client.get_chart", AsyncMock(return_value={"items": [
                {"code": "5100", "name": "Office expenses", "account_type": "expense", "is_active": True}]})),
            patch("ui.api_client.get_company", AsyncMock(return_value=_COMPANY)),
            patch("ui.routes.reports._get_company", AsyncMock(return_value=_COMPANY))]


async def _get(ui_client, url, table, role="owner"):
    patches = _api(table)
    for p in patches:
        p.start()
    try:
        ui_client.cookies.set("celerp_token", make_test_token(role=role))
        return await ui_client.get(url)
    finally:
        for p in patches:
            p.stop()


async def test_bank_page_empty_state_is_drop_zone(ui_client):
    r = await _get(ui_client, "/accounting/reconcile/bank/b1", _EMPTY)
    assert r.status_code == 200, r.text
    html = r.text
    assert 'type="file"' in html and "Start without a file" in html
    assert 'action="/accounting/reconcile/bank/b1/drop"' in html or 'hx-post="/accounting/reconcile/bank/b1/drop"' in html
    r = await _get(ui_client, "/accounting/reconcile/start", _EMPTY)
    assert r.status_code == 200 and "Main Bank" in r.text


async def test_bank_line_row_shows_the_line_in_full(ui_client):
    r = await _get(ui_client, "/accounting/reconcile/bank/b1?view=questions", _TABLE)
    html = r.text
    for text in ("2026-01-05", "WITHDRAWAL", "-200.00", "4471", "800.00", "Posted to 5100 twice"):
        assert text in html, text
    assert 'name="q"' in html and "view=questions" in html


def _tag(html: str, pattern: str) -> str:
    m = re.search(pattern, html, re.S)
    assert m, pattern
    return m.group(0)


async def test_bank_inputs_leave_on_escape(ui_client):
    """G13: each input of the bank page behaves on Escape as the bank UI table says and
    writes nothing."""
    html = (await _get(ui_client, "/accounting/reconcile/bank/b1", _TABLE)).text
    # Ask the client note panel and the split form close on Escape without sending.
    for panel in (r"<[^>]+data-panel=\"ask\"[^>]*>", r"<form[^>]+data-form=\"split\"[^>]*>"):
        tag = _tag(html, panel)
        assert "celerpRemoveClosest" in tag and "event" in tag, panel
    # The search box and plain selects blur (the page loads the shared Escape script).
    assert "ESC_BLURS" in html or "blur()" in html
    # The Answer and contact pickers are searchable selects, which restore their choice on Escape.
    assert html.count("searchable-select") >= 2
    # The bulk toolbar keeps the selection on Escape.
    assert "bulk-toolbar" in html
    # Nothing on the bank page posts on a key press: every hx-trigger is a click, submit or
    # change (the shell's global search above it reads as you type and writes nothing).
    for trig in re.findall(r'hx-trigger="([^"]+)"', html[html.index('id="bank-body"'):]):
        assert "keyup" not in trig and "keydown" not in trig, trig
    page = (await _get(ui_client, "/accounting/reconcile/bank/b1?preview=j1", _TABLE)).text
    assert 'hx-get="/accounting/reconcile/bank/b1/preview/j1"' in page
    found = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "statements": [{
        "file_id": "f1", "header": {"opening_balance": "1000.00", "closing_balance": "800.00",
                                    "period_start": "2026-01-01", "period_end": "2026-01-31", "currency": "USD"},
        "lines": [_LINE], "tie": {"ok": True}, "blocking": [], "notices": []}]}
    with patch("ui.api_client.statement_preview", AsyncMock(return_value=found)):
        preview = (await _get(ui_client, "/accounting/reconcile/bank/b1/preview/j1", _TABLE)).text
    cells = re.findall(r"<[^>]+class=\"[^\"]*editable-cell[^\"]*\"[^>]*>", preview)
    assert len(cells) == 4
    for cell in cells:
        assert "celerpCellKey" in cell, cell


async def test_an_import_refusal_shows_on_the_preview_and_keeps_what_was_entered(ui_client):
    """A refused import re-renders the preview with the reason above it and the header
    figures the user typed, so nothing has to be entered again."""
    from ui.api_client import APIError
    edited = {"opening_balance": "1000.00", "closing_balance": "1234.00", "period_start": "2026-01-01",
              "period_end": "2026-01-31"}
    found = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "statements": [{
        "file_id": "f1", "header": {**edited, "currency": "USD"}, "lines": [_LINE], "tie": {"ok": False},
        "blocking": [], "notices": []}]}
    preview = AsyncMock(return_value=found)
    patches = [patch("ui.api_client.import_statement", AsyncMock(side_effect=APIError(
                   409, "A statement already covers 2026-01-01 to 2026-01-31 on this bank."))),
               patch("ui.api_client.statement_preview", preview)]
    for p in patches:
        p.start()
    try:
        ui_client.cookies.set("celerp_token", make_test_token(role="owner"))
        r = await ui_client.post("/accounting/reconcile/bank/b1/preview/j1/import", data={
            "state": json.dumps({"headers": [edited], "column_map": None}), "idempotency_key": "k1"})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    assert "A statement already covers 2026-01-01 to 2026-01-31 on this bank." in r.text
    assert preview.await_args.args[2] == [{"header": edited}]
    assert 'value="1234.00"' in r.text and 'data-testid="statement-preview"' in r.text


# ── Dropping a statement and its preview ─────────────────────────────────────

_CSV = ("st.csv", b"Date,Description,Amount\n2026-01-05,FEE,-5.00\n", "text/csv")
_DROP = "/accounting/reconcile/bank/b1/drop"


def _signed_in(ui_client, role: str = "owner") -> None:
    ui_client.cookies.set("celerp_token", make_test_token(role=role))


async def _drop(ui_client, kept: AsyncMock, read: AsyncMock | None = None, **send):
    with patch("ui.api_client.bank_files", kept), \
         patch("ui.api_client.read_statement", read or AsyncMock(return_value={"job_id": "j1"})):
        return await ui_client.post(_DROP, **send)


async def test_a_dropped_statement_is_read_and_the_page_follows_the_reading(ui_client):
    files, read = AsyncMock(return_value={"file_ids": ["f1"]}), AsyncMock(return_value={"job_id": "j1"})
    _signed_in(ui_client)
    r = await _drop(ui_client, files, read, files={"files": _CSV}, data={"idempotency_key": "k1"})
    assert r.status_code == 303 and r.headers["location"] == "/accounting/reconcile/bank/b1?preview=j1"
    assert files.await_args.args[1] == [("st.csv", _CSV[1], "text/csv")]
    assert read.await_args.args[1] == {"file_ids": ["f1"], "bank_id": "b1", "idempotency_key": "k1"}


async def test_a_drop_with_no_file_goes_back_to_the_bank_page_saying_so(ui_client):
    from ui.i18n import t
    files = AsyncMock()
    _signed_in(ui_client)
    r = await _drop(ui_client, files, data={})
    assert r.status_code == 303
    assert r.headers["location"] == "/accounting/reconcile/bank/b1?msg=" + quote(t("bank.err_no_file"), safe="")
    files.assert_not_awaited()
    page = await _get(ui_client, r.headers["location"], _TABLE)
    assert t("bank.err_no_file") in page.text


@pytest.mark.parametrize("signed_in", [False, True])
async def test_a_drop_without_a_session_goes_to_sign_in(ui_client, signed_in):
    """Signed out, nothing is sent; a session the API no longer accepts also signs in again."""
    from ui.api_client import APIError
    files = AsyncMock(side_effect=APIError(401, "expired"))
    if signed_in:
        _signed_in(ui_client)
    r = await _drop(ui_client, files, files={"files": _CSV})
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert files.await_count == (1 if signed_in else 0)


@pytest.mark.parametrize("refusal", ["st.csv is larger than the limit", "Statement already completed"])
async def test_a_refused_drop_goes_back_to_the_bank_page_with_the_refusal(ui_client, refusal):
    """A file the API will not keep (too large, wrong type) or a reading it refuses reads on
    the bank page, and no reading starts."""
    from ui.api_client import APIError
    read = AsyncMock()
    _signed_in(ui_client)
    r = await _drop(ui_client, AsyncMock(side_effect=APIError(400, refusal)), read, files={"files": _CSV})
    assert r.status_code == 303
    assert r.headers["location"] == "/accounting/reconcile/bank/b1?msg=" + quote(refusal, safe="")
    read.assert_not_awaited()


_UNMAPPED = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "needs_mapping": True,
             "headers": ["Col A", "Col B", "Col C"], "column_map": None,
             "statements": [{"file_id": "f1", "header": {}, "lines": [], "tie": {"ok": False}, "blocking": [],
                             "notices": []}]}


async def test_a_file_whose_columns_are_not_recognised_asks_for_them(ui_client):
    """Each column gets a picker, the preview's state travels with the form and Escape
    leaves for the bank page."""
    with patch("ui.api_client.statement_preview", AsyncMock(return_value=_UNMAPPED)):
        html = (await _get(ui_client, "/accounting/reconcile/bank/b1/preview/j1", _TABLE)).text
    form = _tag(html, r"<form[^>]+data-testid=\"column-mapper\"[^>]*>")
    assert 'hx-post="/accounting/reconcile/bank/b1/preview/j1"' in form
    assert "celerpGoOnEscape" in form and "/accounting/reconcile/bank/b1" in form
    for column in _UNMAPPED["headers"]:
        assert f'name="map_{column}"' in html
    assert 'name="state"' in html


async def test_named_columns_read_the_file_again_by_them(ui_client):
    """Columns left on ignore are not sent; the file is read again with the named columns
    and nothing is written."""
    preview = AsyncMock(return_value={**_UNMAPPED, "needs_mapping": False})
    _signed_in(ui_client)
    state = json.dumps({"headers": [{"opening_balance": "0.00"}], "column_map": None})
    with patch("ui.api_client.statement_preview", preview), patch("ui.api_client.import_statement") as imp:
        r = await ui_client.post("/accounting/reconcile/bank/b1/preview/j1", data={
            "map_Col A": "date", "map_Col B": "description", "map_Col C": "amount", "map_Col D": "ignore",
            "state": state})
    assert r.status_code == 200, r.text
    assert preview.await_args.args[1:] == ("j1", [{"header": {"opening_balance": "0.00"}}],
                                           {"date": "Col A", "description": "Col B", "amount": "Col C"})
    imp.assert_not_called()


@pytest.mark.parametrize("path", ["/accounting/reconcile/bank/b1/preview/j1",
                                  "/accounting/reconcile/bank/b1/preview/j1/import"])
@pytest.mark.parametrize("state", ["!!!not-json!!!", "[1, 2]", '{"headers": "x"}', '{"column_map": [1]}'])
async def test_a_preview_state_it_did_not_render_is_refused_and_nothing_is_sent(ui_client, path, state):
    preview, imp = AsyncMock(), AsyncMock()
    _signed_in(ui_client)
    with patch("ui.api_client.statement_preview", preview), patch("ui.api_client.import_statement", imp):
        r = await ui_client.post(path, data={"state": state, "idempotency_key": "k1"})
    assert r.status_code == 400
    preview.assert_not_awaited()
    imp.assert_not_awaited()


async def test_a_header_figure_edit_with_an_unreadable_position_is_refused(ui_client):
    preview = AsyncMock()
    _signed_in(ui_client)
    with patch("ui.api_client.statement_preview", preview):
        r = await ui_client.post("/accounting/reconcile/bank/b1/preview/j1", data={
            "state": json.dumps({"headers": [{}], "column_map": None}), "field": "closing_balance",
            "idx": "first", "value": "10"})
    assert r.status_code == 400
    preview.assert_not_awaited()


async def test_a_bank_page_that_cannot_open_goes_to_the_bank_list_saying_why(ui_client):
    from ui.api_client import APIError
    _signed_in(ui_client)
    with patch("ui.api_client.get_bank_account", AsyncMock(side_effect=APIError(404, "Bank account not found"))):
        r = await ui_client.get("/accounting/reconcile/bank/b9")
    assert r.status_code == 302
    assert r.headers["location"] == "/accounting/reconcile/start?msg=" + quote("Bank account not found", safe="")
    page = await _get(ui_client, r.headers["location"], _TABLE)
    assert "Bank account not found" in page.text
    assert 'href="/settings/accounting?tab=bank-accounts"' in page.text


async def test_complete_reads_on_the_bank_page_what_completed_and_where_it_stopped(ui_client):
    from ui.i18n import t
    done = AsyncMock(return_value={"completed": [{"id": "s1"}], "stopped_at": {"reason": "Statement s2 does not tie"}})
    _signed_in(ui_client)
    patches = _api(_TABLE)
    for p in patches:
        p.start()
    try:
        with patch("ui.api_client.bank_action", done):
            r = await ui_client.post("/accounting/reconcile/bank/b1/complete", data={"idempotency_key": "k1"})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200, r.text
    assert done.await_args.args[1:] == ("b1", "complete", {"idempotency_key": "k1"})
    assert t("bank.completed", n=1) in r.text and "Statement s2 does not tie" in r.text
    assert 'id="bank-body"' in r.text


# ── Settings: rules on every bank ───────────────────────────────────────────

async def test_a_rule_can_be_added_for_every_bank(ui_client):
    """The rules tab offers Every bank first, names each rule's bank, and a rule saved with
    Every bank goes to the API with no bank, which the API keeps as a rule on every bank."""
    from ui.i18n import t
    every = {"id": "r1", "bank_account_id": None, "match_pattern": "FEE", "match_type": "contains",
             "target_account_code": "5100", "is_active": True, "times_applied": 0}
    one = {**every, "id": "r2", "bank_account_id": "b1", "match_pattern": "RENT"}
    create = AsyncMock(return_value=every)
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.create_recon_rule", create), \
         patch("ui.api_client.get_recon_rules", AsyncMock(return_value={"items": [every, one]})), \
         patch("ui.api_client.get_bank_accounts", AsyncMock(return_value={"items": [_BANK]})):
        r = await ui_client.post("/settings/accounting/rules", data={
            "bank_account_id": "", "match_pattern": "FEE", "match_type": "contains", "target_account_code": "5100"})
    assert r.status_code == 200 and "error-banner" not in r.text, r.text
    assert create.await_args.args[1]["bank_account_id"] is None
    select = re.search(r'<select[^>]*name="bank_account_id".*?</select>', r.text, re.S).group(0)
    first = re.search(r"<option[^>]*>([^<]*)</option>", select)
    assert first.group(1) == t("acct.rule_every_bank") and 'value=""' in first.group(0)
    rows = re.findall(r"<tr>.*?</tr>", r.text, re.S)
    assert any("FEE" in row and t("acct.rule_every_bank") in row for row in rows)
    assert any("RENT" in row and "Main Bank" in row for row in rows)


async def test_a_rule_without_a_pattern_or_account_is_refused_naming_both(ui_client):
    from ui.i18n import t
    create = AsyncMock()
    ui_client.cookies.update({"celerp_token": make_test_token(role="owner")})
    with patch("ui.api_client.create_recon_rule", create):
        r = await ui_client.post("/settings/accounting/rules", data={"bank_account_id": "", "match_pattern": "",
                                                                     "target_account_code": "5100"})
    assert t("acct.rule_pattern_and_account_code_are_required") in r.text and not create.await_count


async def _post_ticked(ui_client, data, answer):
    action = AsyncMock(return_value=answer)
    _signed_in(ui_client)
    patches = _api(_TABLE)
    for p in patches:
        p.start()
    try:
        with patch("ui.api_client.bank_action", action):
            r = await ui_client.post("/accounting/reconcile/bank/b1/post-ticked", data=data)
    finally:
        for p in patches:
            p.stop()
    return r, action


async def test_post_ticked_sends_each_line_with_the_suggestion_it_showed_and_says_which_were_matched(ui_client):
    from ui.i18n import t
    r, action = await _post_ticked(ui_client, {"group_key": ["SPARSE:WITHDRAWAL:out"], "line_id": ["l1", "l2"],
                                               "version": ["v1", "v2"], "idempotency_key": "k1"},
                                   {"results": [{"line_id": "l1", "ok": True},
                                                {"line_id": "l2", "ok": True, "matched_instead": True}]})
    assert r.status_code == 200, r.text
    assert action.await_args.args[1:] == ("b1", "resolve", {
        "accept": {"group_keys": ["SPARSE:WITHDRAWAL:out"], "line_ids": ["l1", "l2"],
                   "versions": {"l1": "v1", "l2": "v2"}}, "idempotency_key": "k1"})
    assert t("bank.posted_outcome", n=2) in r.text and t("bank.matched_instead", n=1) in r.text


@pytest.mark.parametrize("data", [{"group_key": ["g"]}, {"line_id": ["l1"], "version": ["v1"]}, {}])
async def test_post_ticked_with_nothing_ticked_sends_nothing_and_says_so(ui_client, data):
    from ui.i18n import t
    r, action = await _post_ticked(ui_client, {**data, "idempotency_key": "k1"}, {"results": []})
    assert r.status_code == 200, r.text
    assert not action.await_count and t("bank.nothing_ticked") in r.text


async def test_a_refused_line_answer_shows_its_reason_on_the_form(ui_client):
    from celerp.accounting_roles import catalog_refusal
    from ui.i18n import refusal_text

    reason = catalog_refusal("acct.err_transfer_same_bank")
    answer = {"results": [{"line_id": "l1", "ok": False, "je_ids": [], "reason": reason, "matched_instead": False}]}
    _signed_in(ui_client)
    patches = _api(_TABLE)
    for p in patches:
        p.start()
    try:
        with patch("ui.api_client.bank_action", AsyncMock(return_value=answer)):
            r = await ui_client.post("/accounting/reconcile/bank/b1/resolve-line", data={
                "line_id": "l1", "form": "answer", "answer": "post:5100", "idempotency_key": "k1"})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 200 and "form-error" in r.text, r.text[:400]
    assert refusal_text(reason).split("'")[-1] in r.text, r.text[:600]  # the text after the apostrophe HTML escapes
