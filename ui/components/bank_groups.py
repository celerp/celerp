# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The bank page as markup: a bank's statement lines grouped by counterparty, the
panels a line opens (cloned from templates by bank-page.js, no server trip), the bulk
toolbar, the statements strip, the statement preview and the drop zone. Rendering only:
the routes in ui/routes/bank.py fetch the data and call these."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

from fasthtml.common import *

from celerp.accounting_roles import account_label
from celerp.output.doc_print import money_step
from ui.components.attrs import hx_vals
from ui.components.encode import EVENT, THIS, call, safe_url
from ui.components.funds import UNRESTRICTED, fund_options
from ui.components.shell import flash
from ui.components.table import EMPTY, PARTY_SEARCH_URL, display_enum, searchable_select
from ui.i18n import t

VIEWS = ("open", "suggested", "questions", "posted", "all")
_STATUS_KEYS = {"unmatched": "bank.status_open", "posted": "bank.status_posted", "matched": "bank.status_matched",
                "skipped": "bank.status_no_entry"}
# The header figures of a statement that the preview lets the user correct.
PREVIEW_FIELDS = (("opening_balance", "bank.field_opening"), ("closing_balance", "bank.field_closing"),
                  ("period_start", "bank.field_start"), ("period_end", "bank.field_end"))
# What a column of a tabular statement can hold, as the statement parser names it.
MAP_FIELDS = ("date", "description", "amount", "debit", "credit", "balance", "reference", "ignore")


def bank_url(bank_id: str, *tail: str) -> str:
    return f"/accounting/reconcile/bank/{quote(str(bank_id), safe='')}{''.join('/' + part for part in tail)}"


def new_key() -> str:
    """An idempotency key rendered with a form, so a retry of that form replays."""
    return uuid.uuid4().hex


def money(value, currency: str | None) -> str:
    """A figure as plain digits to the currency's minor unit, the currency named in the header."""
    if value is None or value == "":
        return EMPTY
    try:
        return format(Decimal(str(value)).quantize(Decimal(money_step(currency))), ",f")
    except InvalidOperation:
        return str(value)


def _key_input() -> FT:
    return Input(type="hidden", name="idempotency_key", value=new_key())


def _context_inputs(view: str, q: str) -> tuple:
    return Input(type="hidden", name="view", value=view), Input(type="hidden", name="q", value=q)


# ── The page around the groups ──────────────────────────────────────────────

def relabel_notice(bank: dict, back: str) -> FT | None:
    """The account is labelled in another currency than the books' while every entry on
    it carries a books-currency amount only, so none of its amounts can be read. Says so
    and offers the correction: setting the account to the books' currency changes no
    amount, and setting it back in the account's settings undoes it."""
    base = bank.get("relabel_to")
    if not base:
        return None
    return Div(
        Span(t("recon.relabel_notice", currency=bank.get("currency", ""), base=base)),
        Form(
            Input(type="hidden", name="next", value=back),
            Button(t("recon.relabel_button", base=base), type="submit", cls="btn btn--primary btn--sm"),
            method="post", action=f"/accounting/reconcile/relabel/{bank.get('id', '')}",
        ),
        cls="flash flash--warning recon-relabel",
    )



def page_flashes(params) -> tuple:
    """What a page was sent back with: ``msg`` is a refusal, ``done`` an action that went
    through."""
    msg, done = params.get("msg", "").strip(), params.get("done", "").strip()
    return (flash(msg) if msg else None, flash(done, "success") if done else None)


def rate_input(bank: dict, cls: str = "", value: str = "") -> FT | None:
    """The exchange rate an entry on a bank kept in another currency than the books is
    booked at; a bank in the books currency is never asked."""
    if not bank.get("books_currency") or bank.get("currency") == bank["books_currency"]:
        return None
    label = t("recon.rate_label", currency=bank["currency"], base=bank["books_currency"])
    return Input(type="number", name="rate", step="any", min="0", value=value, placeholder=label,
                 aria_label=label, cls=f"form-input {cls}".strip())


def _statement_actions(bank: dict, statement: dict, view: str, q: str) -> tuple:
    """Reopen on a completed statement; on an open one, write off a difference within
    tolerance and remove the statement (the API refuses once a line of it is posted)."""
    def action(label: str, path: str, confirm: str, *extra) -> FT:
        return Form(_key_input(), *_context_inputs(view, q), *extra,
                    Button(label, type="submit", cls="btn btn--ghost btn--sm"),
                    hx_post=bank_url(bank["id"], "statement", quote(str(statement["id"]), safe=""), path),
                    hx_target="#bank-body", hx_swap="outerHTML", hx_confirm=confirm, cls="inline-form")
    if statement["status"] == "completed":
        return (action(t("btn.reopen"), "reopen", t("recon.reopen_confirm")),)
    return (action(t("btn.write_off_diff"), "write-off", t("bank.write_off_confirm"),
                   rate_input(bank, "bank-strip__rate")),
            action(t("bank.remove_statement"), "remove", t("bank.remove_statement_confirm")))


def statements_strip(statements: list[dict], bank: dict, view: str = "open", q: str = "") -> FT | None:
    if not statements:
        return None
    currency = bank.get("currency")
    return Div(
        *[Div(Span(f"{s.get('period_start') or EMPTY} - {s['statement_date']}", cls="bank-strip__period"),
              Span(money(s["closing_balance"], currency), cls="bank-strip__closing"),
              Span(display_enum(s["status"], "recon_session_status"), cls=f"badge badge--recon-{s['status']}"),
              Span(*_statement_actions(bank, s, view, q), cls="bank-strip__actions screen-only"),
              P(t("recon.needs_review"), cls="flash flash--warning") if s.get("needs_review") else None,
              cls="bank-strip__item", data_statement=s["id"])
          for s in statements],
        cls="bank-strip", aria_label=t("bank.statements_heading"))


def counted_entries(table: dict, bank_id: str, currency: str | None, view: str, q: str) -> FT | None:
    """The book entries the open statement counts with no line holding them, each with its
    Undo (an entry with no amount in the bank's currency shows "--")."""
    diff = table.get("difference") or {}
    entries = diff.get("entries") or []
    if not entries:
        return None
    undo = bank_url(bank_id, "statement", quote(str(diff["statement_id"]), safe=""), "undo-entry")
    return Div(
        P(t("bank.counted_entries"), cls="text-muted"),
        *[Div(Span((e.get("ts") or EMPTY)[:10], cls="bank-entry__date"), Span(e.get("memo") or EMPTY),
              Span(EMPTY if e["amount"] is None else money(e["amount"], currency), cls="cell--number"),
              Form(_key_input(), *_context_inputs(view, q), Input(type="hidden", name="je_id", value=e["je_id"]),
                   Button(t("btn.undo"), type="submit", cls="btn btn--ghost btn--sm"),
                   hx_post=undo, hx_target="#bank-body", hx_swap="outerHTML", cls="inline-form screen-only"),
              cls="bank-entry", id=f"bank-entry-{e['je_id']}")
          for e in entries],
        cls="bank-entries")


def summary_line(table: dict, currency: str) -> FT:
    s = table["summary"]
    diff = table.get("difference") or {}
    parts = [P(t("bank.summary", lines=s["lines"], open=s["open"], questions=s.get("questions", 0),
                 posted=s["posted"]), cls="bank-summary")]
    if diff.get("amount") is not None:
        parts.append(P(t("bank.difference", date=diff.get("statement_date") or EMPTY,
                         amount=money(diff["amount"], currency)), cls="bank-difference"))
    parts += [P(n, cls="text-muted") for n in diff.get("notes") or []]
    if table.get("suspense_count"):
        parts.append(P(t("bank.in_suspense", n=table["suspense_count"]), cls="text-muted"))
    return Div(*parts)


def view_links(bank_id: str, view: str, q: str) -> FT:
    return Nav(*[A(t(f"bank.view_{v}"), href=bank_url(bank_id) + "?" + urlencode({"view": v, **({"q": q} if q else {})}),
                   cls="tab" + (" tab--active" if v == view else ""),
                   aria_current="page" if v == view else None) for v in VIEWS], cls="tabs bank-views")


def search_form(bank_id: str, view: str, q: str) -> FT:
    return Form(Input(type="hidden", name="view", value=view),
                Input(type="search", name="q", value=q, placeholder=t("bank.search_placeholder"),
                      cls="form-input", aria_label=t("bank.search_placeholder")),
                action=bank_url(bank_id), method="get", cls="bank-search")


def page_actions(bank_id: str, view: str, q: str) -> tuple:
    """Suggest and Complete: each posts once and re-renders the page body."""
    def action(label: str, path: str, confirm: str | None = None) -> FT:
        return Form(_key_input(), *_context_inputs(view, q),
                    Button(label, type="submit", cls="btn btn--secondary"),
                    hx_post=bank_url(bank_id, path), hx_target="#bank-body", hx_swap="outerHTML",
                    hx_confirm=confirm, cls="inline-form")
    return (action(t("bank.suggest"), "suggest"), action(t("bank.complete"), "complete", t("bank.complete_confirm")))


def bulk_toolbar(bank_id: str, currency: str, view: str, q: str, outcome: FT | None = None,
                 undo_ids: list[str] | None = None) -> FT:
    """The selection's count and total on its Post button (bank-page.js keeps the hidden
    group and line inputs and the label in step with the ticks). An outcome of the last
    post, with its Undo, sits in the same bar."""
    dp = len(money_step(currency).partition(".")[2])
    post = Form(_key_input(), *_context_inputs(view, q),
                Div(cls="bulk-toolbar__ticked"),
                Button(t("bank.post_ticked", n=0, total=money(0, currency)), type="submit",
                       cls="btn btn--primary bulk-toolbar__post", data_label=t("bank.post_ticked"),
                       data_label_one=t("bank.post_ticked_one"), data_dp=str(dp)),
                hx_post=bank_url(bank_id, "post-ticked"), hx_target="#bank-body", hx_swap="outerHTML",
                hx_confirm=t("bank.post_ticked_confirm"), cls="inline-form")
    undo = None
    if undo_ids:
        undo = Form(_key_input(), *_context_inputs(view, q),
                    *[Input(type="hidden", name="line_id", value=i) for i in undo_ids],
                    Button(t("btn.undo"), type="submit", cls="btn btn--ghost"),
                    hx_post=bank_url(bank_id, "unmatch"), hx_target="#bank-body", hx_swap="outerHTML",
                    hx_confirm=t("bank.undo_all_confirm", n=len(undo_ids)), cls="inline-form")
    attrs = {"data_testid": "outcome"} if outcome is not None else {}
    return Div(post, Span(outcome, cls="bulk-toolbar__outcome") if outcome is not None else None, undo,
               cls="bulk-toolbar", data_bank=bank_id, **attrs)


# ── Groups and lines ────────────────────────────────────────────────────────

def _reason(suggestion: dict | None) -> str:
    s = suggestion or {}
    return s.get("reason") or (s.get("account_code") or EMPTY)


def _thumbs(line: dict) -> list:
    return [A(Img(src=safe_url(a["url"]), alt="", cls="receipt-thumb", loading="lazy"), href=safe_url(a["url"]), target="_blank",
              rel="noopener") for a in line.get("attachments") or [] if a.get("url")]


# Each grid's columns: the heading key (None for a column with no heading) and its alignment.
# The heading row and, on a phone where the cells stack, each cell's own label read from it.
_GROUP_HEAD = ((None, ""), ("bank.th_group", ""), ("bank.th_count", "bank-cell--num"), ("th.total", "bank-cell--money"),
               ("bank.th_suggestion", ""), (None, ""))
_LINE_HEAD = ((None, ""), ("th.date", ""), ("th.description", ""), ("bank.th_counterparty", ""),
              ("th.balance", "bank-cell--money"), ("label.amount", "bank-cell--money"), (None, ""),
              ("bank.th_suggestion", ""), ("th.status", ""), ("bank.th_answer", ""), (None, ""))
_PREVIEW_HEAD = (("th.date", ""), ("th.description", ""), ("label.amount", "bank-cell--money"),
                 ("th.balance", "bank-cell--money"), (None, ""))


def _head(heads: tuple, grid: str) -> FT:
    return Div(*[Span(t(k) if k else "", cls=c) for k, c in heads], cls=f"bank-row {grid} bank-row--head",
               aria_hidden="true")


def _row(*cells: FT, heads: tuple, cls: str) -> FT:
    """A grid row whose cells carry their column's heading, shown beside the value when the row stacks."""
    for cell, (key, _) in zip(cells, heads):
        if key:
            cell.set(data_label=t(key))
    return Div(*cells, cls=cls)


def _posted_to(line: dict) -> str:
    """Where an answered line's money went, by account; ``--`` when its answer made no entry."""
    accounts = [a["code"] if a["name"] == a["code"] else f'{a["code"]} {a["name"]}' for a in line.get("posted_to") or []]
    return t("bank.posted_to", accounts=", ".join(accounts)) if accounts else EMPTY


def line_row(line: dict, bank_id: str, currency: str, *, group_key: str = "", ticked: bool = False,
             notice: FT | None = None) -> FT:
    """One statement line in full, its buttons, and the slot its panels open in."""
    lid, status = line["id"], line["status"]
    is_open = status == "unmatched"
    q = line.get("client_query") or {}
    asked = bool(q.get("asked")) and not q.get("withdrawn")
    if is_open:
        actions = [Button(t("bank.answer_separately"), type="button", cls="btn btn--ghost btn--sm",
                          data_open="answer"),
                   Button(t("bank.split"), type="button", cls="btn btn--ghost btn--sm", data_open="split"),
                   Button(t("bank.remind") if asked else t("bank.ask_client"), type="button",
                          cls="btn btn--ghost btn--sm", data_open="ask")]
    else:
        actions = [Button(t("btn.undo"), type="button", cls="btn btn--ghost btn--sm",
                          hx_post=bank_url(bank_id, "unmatch"), hx_vals=hx_vals({"line_id": lid, "idempotency_key": new_key()}),
                          hx_target="closest [data-line]", hx_swap="outerHTML", hx_confirm=t("bank.undo_confirm"))]
    tick = Input(type="checkbox", data_role="line", data_group=group_key, data_amount=str(line["amount"]),
                 value=lid, checked=ticked, aria_label=line.get("description") or lid) if is_open else Span()
    return Div(
        _row(tick,
            Span(line["line_date"], cls="bank-cell bank-cell--date"),
            Span(line.get("description") or EMPTY, cls="bank-cell bank-cell--text"),
            Span(line.get("counterparty_account") or EMPTY, cls="bank-cell"),
            Span(money(line.get("raw_balance"), currency), cls="bank-cell bank-cell--money"),
            Span(money(line["amount"], currency), cls="bank-cell bank-cell--money"),
            Span(*_thumbs(line), cls="bank-cell bank-cell--thumbs"),
            Span(_reason(line.get("suggestion")) if is_open else _posted_to(line), cls="bank-cell bank-cell--text"),
            Span(Span(t(_STATUS_KEYS.get(status, "bank.status_open")), cls=f"badge badge--recon-{status}"),
                 cls="bank-cell"),
            Span(q.get("answer") or EMPTY, cls="bank-cell bank-cell--text"),
            Span(*actions, cls="bank-cell bank-actions screen-only"),
            cls="bank-row bank-row--line", heads=_LINE_HEAD),
        Div(notice, cls="bank-panel-slot"),
        cls="bank-line", id=f"bank-line-{lid}", data_line=lid, data_amount=str(line["amount"]))


def group_block(group: dict, bank_id: str, currency: str) -> FT:
    lines = group["lines"]
    all_open = all(l["status"] == "unmatched" for l in lines)
    any_open = any(l["status"] == "unmatched" for l in lines)
    ticked = bool(group.get("ticked")) and any_open
    folded = group["count"] > 1 and all_open
    key = group["key"]
    name = key.split(":", 2)[-1] if key.startswith("SPARSE:") else key
    head = _row(
        Input(type="checkbox", data_role="group", data_group=key, checked=ticked, aria_label=name) if any_open
        else Span(),
        Span(name, cls="bank-cell bank-cell--text bank-group__name"),
        Span(str(group["count"]), cls="bank-cell bank-cell--num"),
        Span(money(group["total"], currency), cls="bank-cell bank-cell--money"),
        Span(_reason(group.get("suggestion")), cls="bank-cell bank-cell--text"),
        Span(Button(t("bank.show_lines"), type="button", cls="btn btn--ghost btn--sm", data_show_lines="1")
             if folded else None, cls="bank-cell screen-only"),
        cls="bank-row bank-row--group", heads=_GROUP_HEAD)
    return Div(head,
               Div(*[line_row(l, bank_id, currency, group_key=key, ticked=ticked) for l in lines],
                   cls="bank-lines", hidden=folded),
               cls="bank-group", data_group=key, id="bank-group-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:12])


def group_table(table: dict, bank_id: str, currency: str) -> FT:
    if not table["groups"]:
        return P(t("bank.view_empty"), cls="empty-state")
    return Div(_head(_GROUP_HEAD, "bank-row--group"), _head(_LINE_HEAD, "bank-row--line"), *[group_block(g, bank_id, currency) for g in table["groups"]],
               cls="bank-groups", data_currency=currency)


# ── Panels (templates cloned by bank-page.js) ───────────────────────────────

def answer_options(chart: list[dict], banks: list[dict], bank_id: str) -> list[tuple[str, str]]:
    """What a line can be answered with: an account to post to, another bank (a transfer),
    a cash withdrawal, Suspense or no entry."""
    own = next((b.get("chart_account_code") for b in banks if b["id"] == bank_id), None)
    opts = [(f"post:{a['code']}", account_label(a)) for a in chart
            if a.get("is_active", True) and a.get("code") and a["code"] != own]
    opts += [(f"transfer:{b['id']}", t("bank.answer_transfer", bank=b["bank_name"])) for b in banks
             if b["id"] != bank_id and b.get("is_active", True) and b.get("bank_type") != "cash"]
    return opts + [("cash", t("bank.answer_cash")), ("suspense", t("bank.answer_suspense")),
                   ("no_entry", t("bank.answer_no_entry"))]


def _fund_pick(funds: list[dict] | None, value: str = "") -> FT | None:
    """The Fund choice of an entry posted from a line, Unrestricted until a fund is picked;
    none when the company has no funds turned on."""
    if funds is None:
        return None
    return Div(searchable_select("fund", fund_options(funds), value=value or UNRESTRICTED, placeholder=EMPTY,
                                 aria_label=t("th.fund")), cls="searchable-select")


def _fund_hint(funds: list[dict] | None) -> FT | None:
    """Left at Unrestricted, money moved off a balance the funds share is split among the
    funds holding it, not put on one."""
    return P(t("recon.fund_hint"), cls="form-hint") if funds is not None else None


def answer_form(bank: dict, options: list[tuple[str, str]], funds: list[dict] | None = None, *, line_id: str = "",
                values: dict | None = None, error: str = "") -> FT:
    values = values or {}
    bank_id = bank["id"]
    return Form(
        Input(type="hidden", name="line_id", value=line_id), Input(type="hidden", name="form", value="answer"),
        _key_input(),
        P(error, cls="form-error") if error else None,
        Label(t("bank.answer_label"), cls="form-label"),
        Div(searchable_select("answer", options, value=values.get("answer", ""), aria_label=t("bank.answer_label")),
            cls="searchable-select"),
        Label(t("bank.contact_label"), cls="form-label"),
        Div(searchable_select("contact", [], value=values.get("contact", ""), search_url=PARTY_SEARCH_URL,
                              aria_label=t("bank.contact_label")), cls="searchable-select"),
        Label(t("th.fund"), cls="form-label") if funds is not None else None,
        _fund_pick(funds, values.get("fund", "")), _fund_hint(funds),
        rate_input(bank, value=values.get("rate", "")),
        Div(Button(t("btn.post"), type="submit", cls="btn btn--primary"),
            Button(t("btn.cancel"), type="button", cls="btn btn--ghost",
                   onclick=call("celerpRemoveClosest", THIS, "form")), cls="form-actions"),
        hx_post=bank_url(bank_id, "resolve-line"), hx_target="this", hx_swap="outerHTML",
        cls="bank-panel", data_form="answer", onkeydown=call("celerpRemoveClosest", THIS, "form", EVENT))


def split_form(bank: dict, options: list[tuple[str, str]], funds: list[dict] | None = None, *, line_id: str = "",
               parts: list[tuple[str, str, str, str]] | None = None, rate: str = "", error: str = "") -> FT:
    """The parts of one line, each an account, a positive amount in the bank's minor unit,
    an optional contact and, with funds turned on, its fund; blank rows are dropped."""
    bank_id, currency = bank["id"], bank.get("currency")
    accounts = [(v.split(":", 1)[1], label) for v, label in options if v.startswith("post:")]
    parts = list(parts or []) + [("", "", "", "")] * max(0, 3 - len(parts or []))
    return Form(
        Input(type="hidden", name="line_id", value=line_id), Input(type="hidden", name="form", value="split"),
        _key_input(),
        P(error, cls="form-error") if error else None,
        P(t("bank.split_hint"), cls="text-muted"),
        *[Div(Div(searchable_select("account_code", accounts, value=code, aria_label=t("label.account")),
                  cls="searchable-select"),
              Input(type="number", name="amount", value=amount, min="0", step=money_step(currency), cls="form-input",
                    aria_label=t("label.amount")),
              Div(searchable_select("contact", [], value=contact, search_url=PARTY_SEARCH_URL,
                                    aria_label=t("bank.contact_label")), cls="searchable-select"),
              _fund_pick(funds, fund),
              cls="bank-split__part") for code, amount, contact, fund in parts],
        _fund_hint(funds),
        rate_input(bank, value=rate),
        Div(Button(t("btn.post"), type="submit", cls="btn btn--primary"),
            Button(t("btn.cancel"), type="button", cls="btn btn--ghost",
                   onclick=call("celerpRemoveClosest", THIS, "form")), cls="form-actions"),
        hx_post=bank_url(bank_id, "resolve-line"), hx_target="this", hx_swap="outerHTML",
        cls="bank-panel", data_form="split", onkeydown=call("celerpRemoveClosest", THIS, "form", EVENT))


def ask_panel(bank_id: str) -> FT:
    return Div(
        Input(type="hidden", name="line_id", value=""), _key_input(),
        Label(t("bank.ask_note_label"), cls="form-label"),
        Textarea(name="note", rows="2", cls="form-input"),
        Div(Button(t("bank.ask_send"), type="button", cls="btn btn--primary",
                   hx_post=bank_url(bank_id, "ask"), hx_include="closest [data-panel=ask]",
                   hx_target="closest [data-line]", hx_swap="outerHTML"),
            Button(t("btn.cancel"), type="button", cls="btn btn--ghost",
                   onclick=call("celerpRemoveClosest", THIS, "[data-panel=ask]")), cls="form-actions"),
        cls="bank-panel", data_panel="ask", onkeydown=call("celerpRemoveClosest", THIS, "[data-panel=ask]", EVENT))


def panel_templates(bank: dict, options: list[tuple[str, str]], funds: list[dict] | None) -> tuple:
    return (Template(answer_form(bank, options, funds), id="bank-tpl-answer"),
            Template(split_form(bank, options, funds), id="bank-tpl-split"),
            Template(ask_panel(bank["id"]), id="bank-tpl-ask"))


# ── The drop zone and the statement preview ─────────────────────────────────

def drop_zone(bank_id: str) -> FT:
    return Div(
        H2(t("bank.drop_title")),
        P(t("bank.drop_hint"), cls="text-muted"),
        Form(Input(type="file", name="files", multiple=True, accept=".pdf,.csv,.ofx,.qfx,.xlsx",
                   cls="form-input", data_drop="1", aria_label=t("bank.drop_title")),
             _key_input(),
             action=bank_url(bank_id, "drop"), method="post", enctype="multipart/form-data", cls="bank-drop__form"),
        A(t("bank.start_without"), href=bank_url(bank_id) + "?view=all&start=1", cls="btn btn--ghost"),
        cls="bank-drop settings-card")


def preview_loading(bank_id: str, job_id: str) -> FT:
    return Div(P(t("bank.preview_reading"), cls="text-muted"), hx_get=bank_url(bank_id, "preview", job_id),
               hx_trigger="load delay:1s", hx_swap="outerHTML", data_testid="statement-loading")


def preview_state(headers: list[dict], column_map: dict | None) -> str:
    """What the preview carries between re-checks and into Import: each statement's header
    figures and the columns a tabular file is read by."""
    return json.dumps({"headers": headers, "column_map": column_map})


def header_cell(bank_id: str, job_id: str, idx: int, field: str, value, state: str) -> FT:
    """One header figure of the preview: an input that re-checks the statement on Enter
    and puts the figure back on Escape. The span holds no other input."""
    shown = "" if value is None else str(value)
    restore = bank_url(bank_id, "preview", job_id, "cell") + "?" + urlencode(
        {"idx": idx, "field": field, "value": shown, "state": state})
    return Span(
        Form(Input(name="value", value=shown, placeholder=EMPTY, cls="editable-cell form-input",
                   aria_label=t(dict(PREVIEW_FIELDS)[field]),
                   onkeydown=call("celerpCellKey", THIS, EVENT, {"url": restore, "target": "[data-field]"})),
             hx_post=bank_url(bank_id, "preview", job_id), hx_target="closest [data-testid=statement-preview]",
             hx_swap="outerHTML", hx_vals=hx_vals({"idx": idx, "field": field, "state": state}),
             cls="inline-form"),
        data_field=field, cls="bank-preview__cell")


def column_mapper(bank_id: str, job_id: str, columns: list[str], state: str) -> FT:
    """A tabular file whose columns were not recognised: name what each column holds, then
    the preview reads the file again. Escape leaves for the bank page."""
    def guess(column: str) -> str:
        low = column.strip().lower()
        return low if low in MAP_FIELDS else "ignore"
    return Form(
        H3(t("recon.map_csv_columns")),
        P(t("acct.we_couldnt_autodetect_your_csv_columns_please_map"), cls="text-muted"),
        Table(Thead(Tr(Th(t("th.csv_column")), Th(t("th.maps_to")))),
              Tbody(*[Tr(Td(c), Td(Select(*[Option(f, value=f, selected=f == guess(c)) for f in MAP_FIELDS],
                                          name=f"map_{c}", cls="form-input", aria_label=c)))
                      for c in columns]),
              cls="data-table"),
        Input(type="hidden", name="state", value=state),
        Div(Button(t("btn.confirm_mapping"), type="submit", cls="btn btn--primary"),
            A(t("btn.cancel"), href=bank_url(bank_id), cls="btn btn--secondary"), cls="form-actions"),
        hx_post=bank_url(bank_id, "preview", job_id), hx_target="closest [data-testid=statement-preview]",
        hx_swap="outerHTML", onkeydown=call("celerpGoOnEscape", EVENT, bank_url(bank_id)),
        cls="bank-preview__mapper", data_testid="column-mapper")


def statement_preview(bank_id: str, job_id: str, preview: dict) -> FT:
    """The statements a reading job found, each with its header figures (click to edit),
    whether it ties, what blocks Import, and its lines. A tabular file whose columns were
    not recognised asks for them first. Import confirms first."""
    headers = [{f: st["header"].get(f) for f, _ in PREVIEW_FIELDS} for st in preview["statements"]]
    state = preview_state(headers, preview.get("column_map"))
    if preview.get("needs_mapping"):
        return Div(H2(t("bank.preview_title")), column_mapper(bank_id, job_id, preview.get("headers") or [], state),
                   cls="bank-preview settings-card", data_testid="statement-preview")
    blocks = []
    for idx, st in enumerate(preview["statements"]):
        cur = st["header"].get("currency")
        tie = st.get("tie") or {}
        blocks.append(Div(
            Div(*[Div(Span(t(label), cls="form-label"),
                      header_cell(bank_id, job_id, idx, f, st["header"].get(f), state), cls="form-field")
                  for f, label in PREVIEW_FIELDS], cls="bank-preview__header"),
            P(t("bank.preview_ties") if tie.get("ok") else t("bank.preview_not_tied"),
              cls="flash flash--success" if tie.get("ok") else "flash flash--warning"),
            *[P(b["text"], cls="flash flash--error") for b in st.get("blocking") or []],
            *[P(n, cls="text-muted") for n in st.get("notices") or []],
            Div(_head(_PREVIEW_HEAD, "bank-row--preview"),
                *[_row(Span(l.get("line_date") or EMPTY), Span(l.get("description") or EMPTY, cls="bank-cell--text"),
                       Span(money(l.get("amount"), cur), cls="bank-cell--money"),
                       Span(money(l.get("raw_balance"), cur), cls="bank-cell--money"),
                       Span(t("bank.preview_duplicate") if l.get("duplicate") else ""),
                       cls="bank-row bank-row--preview", heads=_PREVIEW_HEAD) for l in st["lines"]],
                cls="bank-preview__lines"),
            cls="bank-preview__statement"))
    failed = preview.get("failed_steps") or []
    importable = preview["statements"] and not any(st.get("blocking") for st in preview["statements"])
    return Div(
        H2(t("bank.preview_title")),
        P(t("bank.failed_steps", n=len(failed)), cls="flash flash--warning") if failed else None,
        *blocks,
        Form(Input(type="hidden", name="state", value=state), _key_input(),
             Button(t("btn.import"), type="submit", cls="btn btn--primary"),
             hx_post=bank_url(bank_id, "preview", job_id, "import"), hx_confirm=t("bank.import_confirm"),
             hx_target="closest [data-testid=statement-preview]", hx_swap="outerHTML") if importable else None,
        cls="bank-preview settings-card", data_testid="statement-preview")
