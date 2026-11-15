# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The bank page (/accounting/reconcile/bank/{bank_id}), the bank list it is reached
from, and the client's Questions page. Every action calls the bank statement API and
re-renders what it changed: one line, the preview, or the page body."""

from __future__ import annotations

import json
from pathlib import PurePath
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components import bank_groups as BG
from ui.components.shell import base_shell, flash, no_access_banner, page_header, page_title
from ui.components.table import EMPTY
from ui.config import get_token as _token
from ui.i18n import t

_CONTENT_TYPES = {".ofx": "application/x-ofx", ".qfx": "application/vnd.intu.qfx", ".csv": "text/csv"}
_QUESTION_STATUSES = ("open", "answered", "all")


def _login() -> Response:
    return RedirectResponse("/login", status_code=302)


def _fragment_error(e: APIError) -> FT:
    return flash(str(e.detail))


async def _no_access(request: Request, e: APIError, title: str) -> FT:
    return await base_shell(page_header(title), no_access_banner(e), title=page_title("nav.reconcile"),
                            nav_active="accounting", request=request)


def _view(value: str | None) -> str:
    return value if value in BG.VIEWS else "open"


async def _answer_options(token: str, bank_id: str) -> list[tuple[str, str]]:
    chart = (await api.get_chart(token)).get("items", [])
    banks = (await api.get_bank_accounts(token)).get("items", [])
    return BG.answer_options(chart, banks, bank_id)


async def _body(token: str, bank: dict, view: str, q: str, *, outcome: str = "", bulk_outcome: str | None = None,
                undo_ids: list[str] | None = None, table: dict | None = None) -> FT:
    """The page under its header: actions, counts, statements, views, search, the bulk
    toolbar, the groups, the panel templates and the drop for the next statement."""
    bank_id, cur = bank["id"], bank.get("currency")
    table = table or await api.bank_groups(token, bank_id, view=view, q=q)
    return Div(
        flash(outcome, "info") if outcome else None,
        Div(*BG.page_actions(bank_id, view, q), cls="bank-actions-bar screen-only"),
        BG.summary_line(table, cur),
        BG.counted_entries(table, bank_id, cur, view, q),
        BG.statements_strip(table["statements"], bank, view, q),
        BG.view_links(bank_id, view, q),
        BG.search_form(bank_id, view, q),
        BG.bulk_toolbar(bank_id, cur, view, q, bulk_outcome, undo_ids),
        BG.group_table(table, bank_id, cur),
        *BG.panel_templates(bank, await _answer_options(token, bank_id)),
        Details(Summary(t("bank.drop_another")), BG.drop_zone(bank_id), cls="screen-only"),
        id="bank-body", data_bank=bank_id)


async def _fresh_line(token: str, bank: dict, line_id: str, notice: FT | None = None) -> FT | None:
    table = await api.bank_groups(token, bank["id"], view="all")
    for g in table["groups"]:
        for line in g["lines"]:
            if line["id"] == line_id:
                return BG.line_row(line, bank["id"], bank.get("currency"), group_key=g["key"], notice=notice)
    return None


def _line_response(row: FT | None, line_id: str) -> Response:
    """The line re-rendered in its place, wherever the request was sent from."""
    from ui.components.encode import html_response
    return html_response(row if row is not None else Div(), headers={
        "HX-Retarget": f"#bank-line-{line_id}", "HX-Reswap": "outerHTML"})


def setup_routes(app):

    @app.get("/accounting/reconcile/start")
    async def bank_list(request: Request):
        """Every bank with its balance and its page; with no bank, how to add one."""
        token = _token(request)
        if not token:
            return _login()
        try:
            banks = [b for b in (await api.get_bank_accounts(token)).get("items", []) if b.get("is_active", True)]
        except APIError as e:
            if e.status == 401:
                return _login()
            if e.status == 403:
                return await _no_access(request, e, t("bank.list_title"))
            banks = []
        rows = [Div(A(f"{b['bank_name']} {b.get('account_number') or ''}".strip(), href=BG.bank_url(b["id"]),
                      cls="bank-list__name"),
                    Span(b.get("currency") or EMPTY, cls="text-muted"),
                    Span(BG.money(b.get("balance"), b.get("currency")), cls="bank-cell--money"),
                    A(t("recon.history"), href=f"/accounting/reconcile/history/{quote(b['id'], safe='')}",
                      cls="btn btn--ghost btn--sm"),
                    cls="bank-list__row") for b in banks]
        add = A(t("acct.add_bank_account"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary")
        return await base_shell(
            page_header(t("bank.list_title"), add),
            P(t("bank.list_intro") if banks else t("acct.no_bank_accounts_configured"), cls="text-muted"),
            Div(*rows, cls="bank-list settings-card") if rows else None,
            title=page_title("bank.list_title"), nav_active="accounting", request=request)

    @app.get("/accounting/reconcile/bank/{bank_id}")
    async def bank_page(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return _login()
        view, q = _view(request.query_params.get("view")), request.query_params.get("q", "").strip()
        job = request.query_params.get("preview", "").strip()
        try:
            bank = await api.get_bank_account(token, bank_id)
            if job:
                body = Div(BG.preview_loading(bank_id, job), id="bank-body")
            else:
                table = await api.bank_groups(token, bank_id, view=view, q=q)
                if not table["statements"] and not request.query_params.get("start"):
                    body = Div(BG.drop_zone(bank_id), id="bank-body")
                else:
                    body = await _body(token, bank, view, q, table=table)
        except APIError as e:
            if e.status == 401:
                return _login()
            if e.status == 403:
                return await _no_access(request, e, t("nav.reconcile"))
            return RedirectResponse("/accounting/reconcile/start", status_code=302)
        return await base_shell(
            page_header(t("bank.page_title", bank=bank.get("bank_name", "")),
                        A(t("btn.back"), href="/accounting/reconcile/start", cls="btn btn--secondary"),
                        summary=t("bank.page_intro")),
            *BG.page_flashes(request.query_params),
            BG.relabel_notice(bank, BG.bank_url(bank_id)),
            body,
            Script(src="/static/bank-page.js"),
            title=page_title("nav.reconcile"), nav_active="accounting", request=request)

    @app.post("/accounting/reconcile/bank/{bank_id}/drop")
    async def drop(request: Request, bank_id: str):
        """Upload the dropped files and start reading them; the preview follows the job."""
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        files = []
        for f in form.getlist("files"):
            if getattr(f, "filename", ""):
                suffix = PurePath(f.filename).suffix.lower()
                files.append((f.filename, await f.read(),
                              _CONTENT_TYPES.get(suffix) or f.content_type or "application/octet-stream"))
        back = BG.bank_url(bank_id)
        if not files:
            return RedirectResponse(back + "?msg=" + quote(t("bank.err_no_file"), safe=""), status_code=303)
        try:
            ids = (await api.bank_files(token, files))["file_ids"]
            job = await api.read_statement(token, {"file_ids": ids, "bank_id": bank_id,
                                                   "idempotency_key": str(form.get("idempotency_key") or BG.new_key())})
        except APIError as e:
            if e.status == 401:
                return _login()
            return RedirectResponse(back + "?msg=" + quote(str(e.detail), safe=""), status_code=303)
        return RedirectResponse(back + "?preview=" + quote(job["job_id"], safe=""), status_code=303)

    @app.get("/accounting/reconcile/bank/{bank_id}/preview/{job_id}")
    async def preview(request: Request, bank_id: str, job_id: str):
        token = _token(request)
        if not token:
            return _login()
        try:
            return BG.statement_preview(bank_id, job_id, await api.statement_preview(token, job_id))
        except APIError as e:
            if e.status == 409:
                return BG.preview_loading(bank_id, job_id)
            return _fragment_error(e)

    @app.get("/accounting/reconcile/bank/{bank_id}/preview/{job_id}/cell")
    async def preview_cell(request: Request, bank_id: str, job_id: str):
        p = request.query_params
        field = p.get("field", "")
        if field not in dict(BG.PREVIEW_FIELDS):
            return Response(status_code=400)
        return BG.header_cell(bank_id, job_id, int(p.get("idx", "0") or 0), field, p.get("value") or None,
                              p.get("state") or BG.preview_state([], None))

    def _state(form) -> tuple[list[dict], dict | None]:
        state = json.loads(str(form.get("state") or "{}"))
        return state.get("headers") or [], state.get("column_map")

    @app.post("/accounting/reconcile/bank/{bank_id}/preview/{job_id}")
    async def preview_edit(request: Request, bank_id: str, job_id: str):
        """Re-check the statements with one header figure changed, or a tabular file with
        its columns named; nothing is written."""
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        headers, column_map = _state(form)
        field, idx = str(form.get("field", "")), int(str(form.get("idx", "0")) or 0)
        if field in dict(BG.PREVIEW_FIELDS) and 0 <= idx < len(headers):
            headers[idx][field] = str(form.get("value", "")).strip() or None
        named = {str(v): k[4:] for k, v in form.multi_items() if k.startswith("map_") and v != "ignore"}
        if named:
            column_map = named
        try:
            found = await api.statement_preview(token, job_id, [{"header": h} for h in headers], column_map)
        except APIError as e:
            return _fragment_error(e)
        return BG.statement_preview(bank_id, job_id, found)

    @app.post("/accounting/reconcile/bank/{bank_id}/preview/{job_id}/import")
    async def preview_import(request: Request, bank_id: str, job_id: str):
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        headers, column_map = _state(form)
        statements = [{"header": h} for h in headers]
        try:
            await api.import_statement(token, {"job_id": job_id, "bank_id": bank_id, "statements": statements,
                                               "column_map": column_map,
                                               "idempotency_key": str(form.get("idempotency_key"))})
        except APIError as e:
            return Div(_fragment_error(e), BG.statement_preview(
                bank_id, job_id, await api.statement_preview(token, job_id, statements, column_map)))
        return Response(status_code=204, headers={"HX-Redirect": BG.bank_url(bank_id) + "?view=all"})

    async def _page_action(request: Request, bank_id: str, action: str) -> FT | Response:
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        view, q = _view(str(form.get("view", ""))), str(form.get("q", "")).strip()
        bank = await api.get_bank_account(token, bank_id)
        try:
            out = await api.bank_action(token, bank_id, action, {"idempotency_key": str(form.get("idempotency_key"))})
        except APIError as e:
            return await _body(token, bank, view, q, outcome=str(e.detail))
        if action == "suggest":
            outcome = out["summary"]
        else:
            stopped = out.get("stopped_at") or {}
            outcome = " ".join(filter(None, [t("bank.completed", n=len(out.get("completed") or [])),
                                             stopped.get("reason")]))
        return await _body(token, bank, view, q, outcome=outcome)

    @app.post("/accounting/reconcile/bank/{bank_id}/suggest")
    async def suggest(request: Request, bank_id: str):
        return await _page_action(request, bank_id, "suggest")

    @app.post("/accounting/reconcile/bank/{bank_id}/complete")
    async def complete(request: Request, bank_id: str):
        return await _page_action(request, bank_id, "complete")

    @app.post("/accounting/reconcile/bank/{bank_id}/post-ticked")
    async def post_ticked(request: Request, bank_id: str):
        """Post the ticked lines with their groups' suggestions; the outcome and its Undo
        sit in the bulk toolbar."""
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        view, q = _view(str(form.get("view", ""))), str(form.get("q", "")).strip()
        bank = await api.get_bank_account(token, bank_id)
        keys, line_ids = [str(k) for k in form.getlist("group_key")], [str(i) for i in form.getlist("line_id")]
        if not keys:
            return await _body(token, bank, view, q, bulk_outcome=t("bank.nothing_ticked"))
        try:
            out = await api.bank_action(token, bank_id, "resolve", {
                "accept": {"group_keys": keys, "line_ids": line_ids or None},
                "idempotency_key": str(form.get("idempotency_key"))})
        except APIError as e:
            return await _body(token, bank, view, q, bulk_outcome=str(e.detail))
        done = [r["line_id"] for r in out["results"] if r["ok"]]
        refused = [r for r in out["results"] if not r["ok"]]
        text = " ".join([t("bank.posted_outcome", n=len(done)),
                         *([t("bank.posted_refused", n=len(refused), reason=refused[0]["reason"])] if refused else [])])
        return await _body(token, bank, view, q, bulk_outcome=text, undo_ids=done)

    @app.post("/accounting/reconcile/bank/{bank_id}/unmatch")
    async def unmatch(request: Request, bank_id: str):
        """Undo posted lines: one line in its place, or the bulk toolbar's lines with the body."""
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        ids = [str(i) for i in form.getlist("line_id")]
        bank = await api.get_bank_account(token, bank_id)
        whole = "view" in form
        try:
            await api.bank_action(token, bank_id, "unmatch", {"line_ids": ids,
                                                              "idempotency_key": str(form.get("idempotency_key"))})
        except APIError as e:
            if whole:
                return await _body(token, bank, _view(str(form.get("view"))), str(form.get("q", "")),
                                   bulk_outcome=str(e.detail))
            return _line_response(await _fresh_line(token, bank, ids[0], flash(str(e.detail))), ids[0])
        if whole:
            return await _body(token, bank, _view(str(form.get("view"))), str(form.get("q", "")),
                               bulk_outcome=t("bank.undone", n=len(ids)))
        return _line_response(await _fresh_line(token, bank, ids[0]), ids[0])

    @app.post("/accounting/reconcile/bank/{bank_id}/statement/{session_id}/{action}")
    async def statement_action(request: Request, bank_id: str, session_id: str, action: str):
        """Reopen a completed statement, write off an open one's difference within
        tolerance, remove it while none of its lines is posted, or let go of a book entry it
        counts with no line; the page body re-renders with what happened."""
        token = _token(request)
        if not token:
            return _login()
        if action not in ("reopen", "write-off", "remove", "undo-entry"):
            return Response(status_code=404)
        form = await request.form()
        view, q = _view(str(form.get("view", ""))), str(form.get("q", "")).strip()
        key = str(form.get("idempotency_key"))
        bank = await api.get_bank_account(token, bank_id)
        try:
            if action == "reopen":
                await api.reopen_reconciliation(token, session_id)
            elif action == "remove":
                await api.delete_statement(token, session_id, key)
            elif action == "undo-entry":
                await api.unmatch_recon_entries(token, session_id, [str(form.get("je_id", ""))])
            else:
                data: dict = {}
                raw = str(form.get("rate", "")).strip()
                if raw:
                    try:
                        data["rate"] = float(raw)
                    except ValueError:
                        return await _body(token, bank, view, q, outcome=t("acct.err_bank_rate_invalid", raw=raw))
                await api.write_off_recon(token, session_id, data)
        except APIError as e:
            if e.status == 401:
                return _login()
            return await _body(token, bank, view, q, outcome=str(e.detail))
        done = {"reopen": "bank.reopened", "remove": "bank.statement_removed", "write-off": "bank.written_off",
                "undo-entry": "bank.entry_undone"}
        return await _body(token, bank, view, q, outcome=t(done[action]))

    @app.post("/accounting/reconcile/bank/{bank_id}/resolve-line")
    async def resolve_line(request: Request, bank_id: str):
        """Answer or split one line. A refusal re-renders the form with its reason and what
        was typed; success replaces the whole line."""
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        line_id, kind = str(form.get("line_id", "")), str(form.get("form", "answer"))
        bank = await api.get_bank_account(token, bank_id)
        options = await _answer_options(token, bank_id)
        rate = str(form.get("rate", "")).strip()
        if kind == "split":
            parts = [(str(c).strip(), str(a).strip()) for c, a in zip(form.getlist("account_code"),
                                                                       form.getlist("amount"))]
            parts = [(c, a) for c, a in parts if c or a]

            def again(error: str) -> FT:
                return BG.split_form(bank, options, line_id=line_id, parts=parts, rate=rate, error=error)
            if not parts or not all(c for c, _a in parts):
                return again(t("reconciliation.split.account_required"))
            decision = {"kind": "post", "splits": [{"account_code": c, "amount": a or 0} for c, a in parts]}
        else:
            answer, contact = str(form.get("answer", "")), str(form.get("contact", "")).strip()

            def again(error: str) -> FT:
                return BG.answer_form(bank, options, line_id=line_id,
                                      values={"answer": answer, "contact": contact, "rate": rate}, error=error)
            head, _, rest = answer.partition(":")
            if head == "post" and rest:
                decision = {"kind": "post", "account_code": rest}
            elif head == "transfer" and rest:
                decision = {"kind": "transfer", "target_bank_id": rest}
            elif answer in ("cash", "suspense", "no_entry"):
                decision = {"kind": answer}
            else:
                return again(t("bank.err_choose_answer"))
            if contact:
                decision["contact"] = contact
        if rate:
            try:
                decision["rate"] = float(rate)
            except ValueError:
                return again(t("acct.err_bank_rate_invalid", raw=rate))
        try:
            out = await api.bank_action(token, bank_id, "resolve", {
                "decisions": [{"line_ids": [line_id], **decision}],
                "idempotency_key": str(form.get("idempotency_key"))})
        except APIError as e:
            return again(str(e.detail))
        result = out["results"][0]
        if not result["ok"]:
            return again(result["reason"])
        return _line_response(await _fresh_line(token, bank, line_id), line_id)

    @app.post("/accounting/reconcile/bank/{bank_id}/ask")
    async def ask(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        line_id = str(form.get("line_id", ""))
        bank = await api.get_bank_account(token, bank_id)
        try:
            out = await api.bank_action(token, bank_id, "ask", {
                "line_ids": [line_id], "note": str(form.get("note", "")),
                "idempotency_key": str(form.get("idempotency_key"))})
            notice = flash(out["outcome"], "success")
        except APIError as e:
            notice = flash(str(e.detail))
        return _line_response(await _fresh_line(token, bank, line_id, notice), line_id)

    # ── The client's page ───────────────────────────────────────────────────

    def _question_card(q: dict, notice: FT | None = None) -> FT:
        thumbs = [A(Img(src=a["url"], alt="", cls="receipt-thumb"), href=a["url"], target="_blank", rel="noopener")
                  for a in q.get("attachments") or [] if a.get("url")]
        settled = q.get("status") != "unmatched"
        return Div(
            Div(Span(q["line_date"]), Span(q.get("bank_name") or EMPTY, cls="text-muted"),
                Span(q.get("description") or EMPTY, cls="bank-cell--text"),
                Span(f"{BG.money(q['amount'], q.get('currency'))} {q.get('currency') or ''}", cls="bank-cell--money"),
                Span(*thumbs), cls="bank-row bank-row--question"),
            P(q["note"], cls="bank-question__note") if q.get("note") else None,
            notice,
            Div(Span(t("bank.what_was_it"), cls="form-label"), Span(q.get("answer") or EMPTY),
                Span(t("bank.contact_label"), cls="form-label"), Span(q.get("contact_text") or EMPTY),
                cls="print-only"),
            None if settled else Form(
                Input(type="hidden", name="idempotency_key", value=BG.new_key()),
                Label(t("bank.what_was_it"), cls="form-label"),
                Textarea(q.get("answer") or "", name="answer", rows="2", cls="form-input", required=True),
                Label(t("bank.contact_label"), cls="form-label"),
                Input(name="contact_text", value=q.get("contact_text") or "", cls="form-input"),
                Label(t("bank.receipt"), cls="form-label"),
                Input(type="file", name="files", multiple=True, accept="image/*,.pdf", cls="form-input"),
                Div(Button(t("btn.save"), type="submit", cls="btn btn--primary"), cls="form-actions"),
                hx_post=f"/accounting/questions/{quote(q['line_id'], safe='')}/answer",
                hx_encoding="multipart/form-data", hx_target="closest [data-question]", hx_swap="outerHTML",
                cls="screen-only"),
            cls="bank-question settings-card", data_question=q["line_id"])

    @app.get("/accounting/questions")
    async def questions_page(request: Request):
        token = _token(request)
        if not token:
            return _login()
        status = request.query_params.get("status", "open")
        status = status if status in _QUESTION_STATUSES else "open"
        try:
            found = await api.bank_questions(token, status)
        except APIError as e:
            if e.status == 401:
                return _login()
            return await base_shell(page_header(t("nav.bank_questions")), no_access_banner(e),
                                    title=page_title("nav.bank_questions"), request=request)
        tabs = Nav(*[A(t(f"bank.questions_{s}"), href=f"/accounting/questions?status={s}",
                       cls="tab" + (" tab--active" if s == status else "")) for s in _QUESTION_STATUSES],
                   cls="tabs screen-only")
        items = found["items"]
        return await base_shell(
            page_header(t("nav.bank_questions"), summary=t("bank.questions_intro")),
            tabs,
            Div(*[_question_card(q) for q in items], cls="bank-questions") if items
            else P(t("bank.questions_empty"), cls="empty-state"),
            title=page_title("nav.bank_questions"), nav_active="bank_questions", request=request)

    @app.post("/accounting/questions/{line_id}/answer")
    async def answer_question(request: Request, line_id: str):
        token = _token(request)
        if not token:
            return _login()
        form = await request.form()
        files = [(f.filename, await f.read(), f.content_type or "application/octet-stream")
                 for f in form.getlist("files") if getattr(f, "filename", "")]
        try:
            ids = (await api.bank_files(token, files))["file_ids"] if files else []
            await api.answer_bank_question(token, line_id, {
                "answer": str(form.get("answer", "")).strip(),
                "contact_text": str(form.get("contact_text", "")).strip() or None,
                "file_ids": ids, "idempotency_key": str(form.get("idempotency_key"))})
            notice = flash(t("bank.answer_saved"), "success")
        except APIError as e:
            notice = flash(str(e.detail))
        found = await api.bank_questions(token, "all")
        q = next((i for i in found["items"] if i["line_id"] == line_id), None)
        return _question_card(q, notice) if q else notice
