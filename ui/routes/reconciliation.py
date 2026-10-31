# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Bank reconciliation workspace UI routes."""

from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import quote, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from celerp.accounting_roles import account_label
from celerp.importers import tabular
from celerp.output.doc_print import money_step
from celerp.services.business_time import business_today
import ui.api_client as api
from ui.components.icons import import_icon
from ui.api_client import APIError
from ui.components.attrs import hx_vals
from ui.components.shell import base_shell, flash, page_header, page_title
from ui.components.table import (
    EMPTY, PARTY_PRELOAD, PARTY_SEARCH_URL, add_new_option, display_enum, fmt_money,
    party_options, searchable_select, status_cards,
)
from ui.config import get_token as _token
from ui.i18n import t
from ui.routes.reports import _get_company


_MAX_CSV_BYTES = 5 * 1024 * 1024
_CANONICAL_FIELDS = ["date", "description", "amount", "debit", "credit", "balance", "reference", "ignore"]


# ── Status helpers ────────────────────────────────────────────────────────────

_STATUS_CLS = {
    "matched": "recon-row--matched",
    "created": "recon-row--matched",
    "suggested": "recon-row--suggested",
    "unmatched": "recon-row--unmatched",
    "skipped": "recon-row--skipped",
}

def _row_status_badge(status: str) -> FT:
    # Raw status stays canonical (badge class, comparisons); only the visible label
    # is resolved per request language via the enum display layer (R3).
    return Span(display_enum(status, "recon_status"), cls=f"badge badge--recon-{status}")


# ── Select-to-match interaction ───────────────────────────────────────────────
# Click a bank statement line to select it, then click a book entry to reconcile
# the two on the spot, the way Xero handles a manual match. No popup, no reload.
_RECON_MATCH_JS = """
window.reconSelectLine = function(row, ev) {
  if (ev && ev.target.closest('button, a, input, select, textarea, label, form, .row-actions')) return;
  var ws = row.closest('.recon-workspace');
  if (!ws) return;
  var wasActive = row.classList.contains('recon-row--active');
  ws.querySelectorAll('.recon-row--active').forEach(function(r){ r.classList.remove('recon-row--active'); });
  if (!wasActive) row.classList.add('recon-row--active');
};
window.reconMatchEntry = function(entry) {
  var ws = entry.closest('.recon-workspace');
  if (!ws) return;
  var active = ws.querySelector('.recon-row--active');
  if (!active) {
    celerpToast(ws.dataset.selectFirst, 'info');
    return;
  }
  htmx.ajax('POST', '/accounting/reconcile/' + ws.dataset.sessionId + '/lines/'
      + active.id.replace('stmt-line-', '') + '/match-confirm', {
    values: {je_id: entry.id.replace('book-entry-', '')},
    target: '#recon-workspace',
    swap: 'outerHTML',
  });
};
window.reconFilterBook = function(input) {
  var q = input.value.trim().toLowerCase();
  input.closest('.recon-panel').querySelectorAll('.recon-book-entry').forEach(function(row) {
    row.style.display = !q || row.textContent.toLowerCase().indexOf(q) !== -1 ? '' : 'none';
  });
};
if (!window._reconScrollHook) {
  window._reconScrollHook = true;
  var _reconScroll = {};
  document.body.addEventListener('htmx:beforeSwap', function(ev) {
    var ws = document.getElementById('recon-workspace');
    if (!ws || !ws.contains(ev.detail.target) && ev.detail.target !== ws) return;
    _reconScroll = {};
    ws.querySelectorAll('.recon-panel-body').forEach(function(b, i) { _reconScroll[i] = b.scrollTop; });
  });
  document.body.addEventListener('htmx:afterSettle', function() {
    var ws = document.getElementById('recon-workspace');
    if (!ws) return;
    ws.querySelectorAll('.recon-panel-body').forEach(function(b, i) {
      if (_reconScroll[i]) b.scrollTop = _reconScroll[i];
    });
    _reconScroll = {};
  });
}
"""


# ── Statement line row ────────────────────────────────────────────────────────

def _stmt_line_row(line: dict, session_id: str, currency: str, *, read_only: bool = False,
                   reason: str | None = None) -> FT:
    """One statement line. A *read_only* line (of a completed reconciliation) has no
    actions; *reason* is the key of why a bulk confirm left the line unmatched."""
    lid = line["id"]
    status = line.get("status", "unmatched")
    row_cls = "recon-row " + _STATUS_CLS.get(status, "")
    amount = float(line.get("amount", 0))
    amt_cls = "cell--number " + ("recon-amount--credit" if amount >= 0 else "recon-amount--debit")

    selectable = not read_only and status in ("unmatched", "suggested")
    actions = []
    if selectable:
        actions += [
            A(t("acct.create"),
                hx_get=f"/accounting/reconcile/{session_id}/lines/{lid}/create-form",
                hx_target=f"#recon-expand-{lid}",
                hx_swap="innerHTML",
                cls="btn btn--xs btn--secondary",
            ),
            A(t("inv.split"),
                hx_get=f"/accounting/reconcile/{session_id}/lines/{lid}/split-form",
                hx_target=f"#recon-expand-{lid}",
                hx_swap="innerHTML",
                cls="btn btn--xs btn--secondary",
            ),
        ]
        if status == "suggested":
            actions.append(
                Button(t("btn.confirm"),
                    hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/match-confirm",
                    # Bind the entry the user is looking at, so a suggestion that
                    # changes between render and click cannot match silently.
                    hx_vals=hx_vals({"je_id": str(line.get("matched_je_id") or "")}),
                    hx_target="#recon-workspace",
                    hx_swap="outerHTML",
                    cls="btn btn--xs btn--primary",
                )
            )
        actions.append(
            Button(t("btn.skip"),
                hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/skip",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--outline",
            )
        )
    elif not read_only and status in ("matched", "created"):
        actions.append(
            Button(t("btn.undo"),
                hx_post=f"/accounting/reconcile/{session_id}/lines/{lid}/unmatch",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                cls="btn btn--xs btn--outline",
            )
        )

    return Div(
        Div(
            Div(
                Span(line.get("line_date", "--"), cls="recon-date"),
                Span(line.get("description", "--"), cls="recon-desc"),
                Span(line.get("reference") or "", cls="recon-ref text-muted"),
                cls="recon-row-left",
            ),
            Div(
                Span(fmt_money(amount, currency), cls=amt_cls),
                _row_status_badge(status),
                Div(*actions, cls="row-actions"),
                cls="recon-row-right",
            ),
            cls="recon-row-main",
        ),
        P(t(reason), cls="flash flash--warning") if reason else None,
        Div(id=f"recon-expand-{lid}", cls="recon-row-expand"),
        id=f"stmt-line-{lid}",
        cls=row_cls + (" recon-row--selectable" if selectable else ""),
        onclick=("reconSelectLine(this, event)" if selectable else None),
    )


# ── Exchange rate (bank kept in another currency than the books) ─────────────

def _rate_field(bank: dict) -> FT | None:
    """The rate an entry booked on *bank* converts at into the books' currency, asked
    only when the account is kept in another currency than the books'."""
    if bank["currency"] == bank["books_currency"]:
        return None
    return Div(
        Label(t("recon.rate_label", currency=bank["currency"], base=bank["books_currency"]), cls="form-label"),
        Input(type="number", name="rate", step="any", min="0", required=True, cls="form-input max-w-sm"),
        cls="form-field",
    )


def _form_rate(form, data: dict) -> str | None:
    """Put the form's rate into *data*; the refusal to show when it is not a number."""
    raw = str(form.get("rate", "")).strip()
    if not raw:
        return None
    try:
        data["rate"] = float(raw)
    except ValueError:
        return t("acct.err_bank_rate_invalid", raw=raw)
    return None


# ── Create expense form (inline partial) ─────────────────────────────────────

def _create_form(session_id: str, line_id: str, line: dict, chart: list[dict], bank: dict,
                 contacts: list[dict] | None = None) -> FT:
    currency = bank["currency"]
    amount = abs(float(line.get("amount", 0)))
    contact_opts = party_options(contacts or [])
    account_options = [
        Option(account_label(a), value=a["code"])
        for a in chart
        if a.get("account_type") in ("expense", "cogs", "asset", "liability")
    ]
    _acct_opt, _acct_js = add_new_option(t("recon.add_new_account"), "/settings/accounting?tab=chart")
    return Div(
        H4(t("page.create_journal_entry")),
        Form(
            Div(
                Label(t("th.account"), cls="form-label"),
                Select(*account_options, _acct_opt, name="account_code",
                       cls="form-input cell-input--select", onchange=_acct_js),
                cls="form-field",
            ),
            Div(
                Label(t("acct.je_line_party"), cls="form-label"),
                searchable_select("contact", contact_opts, placeholder=t("acct.je_line_party"),
                                  cls_extra="form-input", search_url=PARTY_SEARCH_URL),
                cls="form-field",
            ),
            Div(
                Label(t("th.memo"), cls="form-label"),
                Input(type="text", name="memo", value=line.get("description", ""),
                      cls="form-input"),
                cls="form-field",
            ),
            Div(
                Label(t("label.amount"), cls="form-label"),
                Input(type="number", name="amount", value=str(amount),
                      step=money_step(currency), cls="form-input"),
                cls="form-field",
            ),
            _rate_field(bank),
            Div(
                Button(t("btn.create_match"), type="submit", cls="btn btn--primary btn--sm"),
                Button(t("btn.cancel"), type="button",
                       onclick=f"document.getElementById('recon-expand-{line_id}').innerHTML=''",
                       cls="btn btn--outline btn--sm ml-sm"),
                cls="form-actions",
            ),
            hx_post=f"/accounting/reconcile/{session_id}/lines/{line_id}/create-confirm",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
        ),
        cls="recon-inline-form",
    )


# ── Split form (inline partial) ───────────────────────────────────────────────

def _split_form(session_id: str, line_id: str, line: dict, chart: list[dict], bank: dict,
                contacts: list[dict] | None = None) -> FT:
    currency = bank["currency"]
    amount = abs(float(line.get("amount", 0)))
    contact_opts = party_options(contacts or [])
    account_options = [
        Option(account_label(a), value=a["code"])
        for a in chart
        if a.get("account_type") in ("expense", "cogs", "asset", "liability", "revenue")
    ]
    _acct_opt, _acct_js = add_new_option(t("recon.add_new_account"), "/settings/accounting?tab=chart")
    return Div(
        H4(t("page.split_transaction")),
        Form(
            Div(
                P(f"{t('doc.total')} {fmt_money(float(line.get('amount', 0)), currency)}", cls="text-muted"),
                id="split-entries",
                *[
                    Div(
                        Select(*account_options, _acct_opt, name="account_code_0",
                               cls="form-input cell-input--select w-auto", onchange=_acct_js),
                        Input(type="number", name="amount_0", placeholder=t("label.amount"),
                              step=money_step(currency), cls="form-input max-w-sm"),
                        searchable_select("contact_0", contact_opts,
                                          placeholder=t("acct.je_line_party"),
                                          cls_extra="form-input", search_url=PARTY_SEARCH_URL),
                        Input(type="text", name="memo_0", placeholder=t("th.memo"), cls="form-input"),
                        cls="split-entry-row flex-row gap-sm mb-sm",
                    )
                ],
            ),
            _rate_field(bank),
            Div(
                Button(t("btn.split_match"), type="submit", cls="btn btn--primary btn--sm"),
                Button(t("btn.cancel"), type="button",
                       onclick=f"document.getElementById('recon-expand-{line_id}').innerHTML=''",
                       cls="btn btn--outline btn--sm ml-sm"),
                cls="form-actions",
            ),
            hx_post=f"/accounting/reconcile/{session_id}/lines/{line_id}/split-confirm",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
        ),
        cls="recon-inline-form",
    )


# ── Progress bar ──────────────────────────────────────────────────────────────

def _recon_progress(total: int, matched: int, created: int, skipped: int) -> FT:
    if total == 0:
        pct = 0
    else:
        pct = int((matched + created + skipped) / total * 100)
    return Div(
        Div(
            Span(t("recon.progress_resolved", done=matched + created, total=total),
                 cls="recon-progress-label"),
            Span(t("recon.progress_skipped", n=skipped), cls="recon-progress-label text-muted"),
        ),
        Div(
            Div(cls="recon-progress-fill", style=f"width:{pct}%;"),
            cls="recon-progress",
        ),
    )


# ── Main workspace ────────────────────────────────────────────────────────────

def _workspace_view(
    session_id: str,
    recon: dict,
    bank: dict,
    lines: list[dict],
    book_entries: list[dict],
    currency: str,
    notice: str | None = None,
    notice_kind: str = "error",
    skipped_reasons: dict[str, str] | None = None,
) -> FT:
    # A completed reconciliation is the closed record of a bank statement, so it shows
    # no edit controls (GDR 2e, owner decision): its one way to change, Reopen, sits on
    # the banner, and the server still refuses and explains any change to it.
    read_only = recon.get("status") == "completed"
    skipped_reasons = skipped_reasons or {}
    total = len(lines)
    matched = sum(1 for l in lines if l["status"] in ("matched", "created"))
    suggested = sum(1 for l in lines if l["status"] == "suggested")
    skipped = sum(1 for l in lines if l["status"] == "skipped")
    unmatched = total - matched - suggested - skipped

    stmt_bal = float(recon.get("statement_balance", 0))
    # The server rounds the difference at the account currency and says whether the
    # session balances; the screen judges on that flag alone, exactly as completion does.
    # A difference of None is unknown: an entry has no amount in the account's currency.
    difference = recon.get("difference")
    balanced = bool(recon.get("balanced"))
    tol = float(recon.get("tolerance", 1.0))
    writable_off = not balanced and difference is not None and abs(float(difference)) <= tol
    diff_cls = "recon-diff--ok" if balanced else ("recon-diff--warn" if writable_off else "recon-diff--bad")

    # Build sets for unmatched book entries
    matched_je_ids = {l["matched_je_id"] for l in lines if l.get("matched_je_id")}
    # Entries the session counts with no statement line holding them (matched through
    # the API): shown as matched, each with its own Undo.
    held = set(recon.get("reconciled_je_ids") or []) - matched_je_ids
    unmatched_entries = sorted(
        (e for e in book_entries if e["je_id"] not in matched_je_ids),
        key=lambda e: e.get("ts") or "", reverse=True,
    )

    bank_name = bank.get("bank_name", "")
    acc_num = bank.get("account_number", "")

    header = Div(
        Div(
            H2(t("recon.title", bank=bank_name, account=acc_num), cls="recon-title"),
            Span(t("recon.statement_date", date=recon.get("statement_date", "--")), cls="text-muted"),
            A(t("recon.history"), href=f"/accounting/reconcile/history/{bank.get('id', '')}",
              cls="btn btn--secondary btn--sm"),
        ),
        Div(
            Span(t("recon.statement_balance", amount=fmt_money(stmt_bal, currency)), cls="val-chip"),
            Span(t("recon.difference", amount=fmt_money(difference, currency)), cls=f"val-chip {diff_cls}"),
        ),
        cls="recon-header",
    )

    toolbar = Div(
        Button(
            t("recon.auto_match_all"),
            hx_post=f"/accounting/reconcile/{session_id}/auto-match",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
            cls="btn btn--secondary btn--sm",
        ),
        Button(
            t("recon.confirm_suggestions", n=suggested),
            hx_post=f"/accounting/reconcile/{session_id}/bulk-confirm",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
            cls="btn btn--secondary btn--sm",
            disabled="" if suggested == 0 else None,
        ),
        *(
            [Button(t("btn.complete"),
                hx_post=f"/accounting/reconcile/{session_id}/complete",
                hx_target="body",
                hx_swap="outerHTML",
                cls="btn btn--primary btn--sm",
            )]
            if balanced else
            [Form(
                _rate_field(bank),
                Button(t("btn.write_off_diff"), type="submit", cls="btn btn--outline btn--sm"),
                hx_post=f"/accounting/reconcile/{session_id}/write-off",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                cls="flex-row gap-sm",
            )]
            if writable_off else []
        ),
        cls="recon-toolbar",
    ) if not read_only else None

    bank_panel = Div(
        H3(t("page.bank_statement"), cls="recon-panel-title"),
        _recon_progress(total, matched, created=sum(1 for l in lines if l["status"] == "created"),
                        skipped=skipped),
        Div(
            *[_stmt_line_row(l, session_id, currency, read_only=read_only,
                             reason=skipped_reasons.get(l["id"])) for l in lines],
            id="stmt-lines-panel",
            cls="recon-panel-body",
        ) if lines else Div(
            P(t("acct.no_statement_lines_imported_yet"), cls="empty-state"),
            None if read_only else Form(
                Div(
                    Label(t("label.upload_csv"), cls="form-label"),
                    Input(type="file", name="csv_file", accept=".csv", cls="form-input", required=True),
                    cls="form-field",
                ),
                Button(import_icon(), t("doc.import_csv"), type="submit", cls="btn btn--primary btn--sm"),
                hx_post=f"/accounting/reconcile/{session_id}/import",
                hx_target="#recon-workspace",
                hx_swap="outerHTML",
                hx_encoding="multipart/form-data",
            ),
        ),
        cls="recon-panel recon-panel--bank",
    )

    book_panel = Div(
        H3(t("page.book_entries"), cls="recon-panel-title"),
        None if read_only else P(t("recon.book_match_hint"), cls="recon-panel-hint text-muted"),
        Input(type="search", placeholder=t("label.search"),
              oninput="reconFilterBook(this)",
              cls="form-input recon-book-filter") if unmatched_entries else None,
        Div(
            *[
                Div(
                    Div(
                        Span(e.get("ts", EMPTY)[:10], cls="recon-date"),
                        Span(e.get("memo", EMPTY), cls="recon-desc"),
                        cls="recon-row-left",
                    ),
                    Div(
                        Span(fmt_money(e.get("amount", 0), currency), cls="cell--number"),
                        cls="recon-row-right",
                    ),
                    Button(t("btn.undo"),
                           hx_post=f"/accounting/reconcile/{session_id}/entries/{e['je_id']}/unmatch",
                           hx_target="#recon-workspace", hx_swap="outerHTML",
                           cls="btn btn--xs btn--outline") if e["je_id"] in held and not read_only else None,
                    cls="recon-row recon-row--matched recon-book-entry" if e["je_id"] in held
                    else "recon-row recon-row--unmatched recon-book-entry",
                    id=f"book-entry-{e['je_id']}",
                    onclick=None if read_only or e["je_id"] in held else "reconMatchEntry(this)",
                )
                for e in unmatched_entries
            ],
            cls="recon-panel-body",
        ) if unmatched_entries else Div(
            P(t("acct.all_book_entries_matched"), cls="empty-state"),
            cls="recon-panel-body",
        ),
        cls="recon-panel recon-panel--book",
    )

    completed = Div(
        Span(t("recon.completed_banner", date=(recon.get("completed_at") or "")[:10] or EMPTY)),
        Button(
            t("btn.reopen"),
            hx_post=f"/accounting/reconcile/{session_id}/reopen",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
            hx_confirm=t("recon.reopen_confirm"),
            cls="btn btn--secondary btn--sm",
        ),
        cls="flash flash--success recon-completed",
    ) if read_only else None

    return Div(
        completed,
        _relabel_notice(bank, f"/accounting/reconcile/{session_id}"),
        P(t("recon.needs_review"), cls="flash flash--warning") if recon.get("needs_review") else None,
        header,
        toolbar,
        Div(
            Span(t("recon.stat_unmatched", n=unmatched), cls="recon-stat"),
            Span(t("recon.stat_suggested", n=suggested), cls="recon-stat recon-stat--suggested"),
            Span(t("recon.stat_matched", n=matched), cls="recon-stat recon-stat--matched"),
            Span(t("recon.stat_skipped", n=skipped), cls="recon-stat recon-stat--skipped"),
            cls="recon-stats-bar",
        ),
        Div(bank_panel, book_panel, cls="recon-panels"),
        Script(_RECON_MATCH_JS),
        Script(f"celerpToast({json.dumps(notice)}, {json.dumps(notice_kind)});") if notice else None,
        id="recon-workspace",
        cls="recon-workspace",
        **{"data-session-id": session_id,
           "data-select-first": t("recon.select_line_first")},
    )


def _relabel_notice(bank: dict, back: str) -> FT | None:
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


# ── Reconciliation history ───────────────────────────────────────────────────

# Session statuses in the order the history filter cards show them, with their colour.
_SESSION_STATUSES = {"open": "yellow", "completed": "green"}


def _history_table(items: list[dict], currency: str) -> FT:
    """An account's reconciliations, newest statement first (the API's order)."""
    headers = (Th(t("label.statement_date")), Th(t("label.statement_closing_balance"), cls="cell--number"),
               Th(t("th.status")), Th(t("th.completed")))

    def _rows(i: dict) -> tuple[FT, ...]:
        row = Tr(
            Td(A(i["statement_date"], href=f"/accounting/reconcile/{i['id']}")),
            Td(fmt_money(i["statement_balance"], currency), cls="cell--number"),
            Td(display_enum(i["status"], "recon_session_status")),
            Td((i.get("completed_at") or "")[:10] or EMPTY),
        )
        if not i.get("needs_review"):
            return (row,)
        # The note spans the row below so it wraps instead of widening a column.
        return row, Tr(Td(P(t("recon.needs_review"), cls="flash flash--warning"),
                          colspan=str(len(headers)), cls="cell--note"))
    return Table(
        Thead(Tr(*headers)),
        Tbody(*[r for i in items for r in _rows(i)]),
        cls="data-table",
    )


def _book_entries(recon: dict) -> list[dict]:
    """The book entries the workspace lists: those not yet counted, plus those the session
    counts by their own match (not the opening balance), so each can be undone."""
    held = set(recon.get("reconciled_je_ids") or [])
    return recon.get("unreconciled_entries", []) + [e for e in recon.get("reconciled_entries", []) if e["je_id"] in held]


async def _fresh_workspace(token: str, session_id: str, notice: str | None = None, **view):
    """Re-render the whole workspace from fresh API state.

    Every reconciliation mutation returns this, so the book panel, stats bar,
    difference chip, and toolbar can never drift out of step with the lines.
    *view* passes the notice kind and skipped-line reasons on to the view.
    """
    try:
        recon = await api.get_reconciliation(token, session_id)
        lines = (await api.get_statement_lines(token, session_id)).get("items", [])
    except APIError as e:
        if e.status == 401:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        return P(str(e.detail), cls="error-banner")
    bank = recon["bank_account"]
    book_entries = _book_entries(recon)
    return _workspace_view(session_id, recon, bank, lines, book_entries, bank["currency"],
                           notice=notice, **view)


# ── CSV import helpers ────────────────────────────────────────────────────────

def _page_flashes(request: Request) -> tuple:
    """What the page was sent back with: ``msg`` is a refusal, ``done`` an action that
    went through."""
    msg = request.query_params.get("msg", "").strip()
    done = request.query_params.get("done", "").strip()
    return (flash(msg) if msg else None, flash(done, "success") if done else None)


def _workspace_redirect(session_id: str, msg: str = "") -> Response:
    """204 + HX-Redirect back to the workspace so htmx navigates the browser
    instead of swapping a full page into #recon-workspace. The message is
    carried in the URL and rendered by the workspace as a flash banner."""
    url = f"/accounting/reconcile/{session_id}"
    if msg:
        url += "?msg=" + quote(msg, safe="")
    return Response("", status_code=204, headers={"HX-Redirect": url})


def _mapper_fragment(
    session_id: str,
    headers: list[str],
    csv_b64: str,
    filename: str,
    selections: dict[str, str] | None = None,
    error: str = "",
) -> FT:
    """Inline column-mapping step, rendered into #recon-workspace. Carries the
    uploaded CSV forward as a base64 hidden field so confirm needs no server-side
    stash. Defaults each select to the canonical field matching the header name."""
    selections = selections or {}

    def _default(h: str) -> str:
        low = h.strip().lower()
        return low if low in _CANONICAL_FIELDS else "ignore"

    rows = [
        Tr(
            Td(h),
            Td(Select(
                *[Option(f, value=f, selected=(f == selections.get(h, _default(h))))
                  for f in _CANONICAL_FIELDS],
                name=f"map_{h}",
                cls="form-input cell-input--select",
            )),
        )
        for h in headers
    ]

    return Div(
        H3(t("recon.map_csv_columns"), cls="recon-panel-title"),
        P(t("acct.we_couldnt_autodetect_your_csv_columns_please_map"), cls="text-muted"),
        P(error, cls="error-banner") if error else None,
        Form(
            Table(
                Thead(Tr(Th(t("th.csv_column")), Th(t("th.maps_to")))),
                Tbody(*rows),
                cls="data-table",
            ),
            Input(type="hidden", name="csv_b64", value=csv_b64),
            Input(type="hidden", name="csv_filename", value=filename),
            Div(
                Button(t("btn.confirm_mapping"), type="submit", cls="btn btn--primary"),
                A(t("btn.cancel"), href=f"/accounting/reconcile/{session_id}",
                  cls="btn btn--secondary ml-sm"),
                cls="mt-md",
            ),
            onkeydown=f"if(event.key==='Escape'){{window.location='/accounting/reconcile/{session_id}'}}",
            hx_post=f"/accounting/reconcile/{session_id}/confirm-import",
            hx_target="#recon-workspace",
            hx_swap="outerHTML",
        ),
        id="recon-workspace",
        cls="recon-workspace settings-card",
    )


# ── Setup routes ─────────────────────────────────────────────────────────────

def setup_routes(app):

    @app.get("/accounting/reconcile/start")
    async def reconcile_start(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        company = await _get_company(token)
        try:
            banks = (await api.get_bank_accounts(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            banks = []

        chosen = request.query_params.get("bank_id", "")
        # One minor unit of the finest currency among the accounts offered, so any of
        # their statement balances can be typed; the server judges at the chosen one.
        balance_step = min((money_step(b.get("currency")) for b in banks if b.get("is_active", True)),
                           key=float, default=money_step(company.get("currency")))
        bank_options = [
            Option(f"{b['bank_name']} {b.get('account_number', '')}", value=b["id"], selected=b["id"] == chosen)
            for b in banks if b.get("is_active", True)
        ]
        _bank_opt, _bank_js = add_new_option(t("recon.add_new_bank_account"), "/settings/accounting?tab=bank-accounts")

        if bank_options:
            bank_select_or_msg = Select(
                *bank_options, _bank_opt,
                name="bank_account_id",
                cls="form-input cell-input--select",
                required=True,
                onchange=_bank_js,
            )
        else:
            bank_select_or_msg = P(t("acct.no_bank_accounts_configured"),
                A(t("acct.add_bank_account"), href="/settings/accounting?tab=bank-accounts"),
            )

        form = Div(
            Form(
                Div(
                    Label(t("label.bank_account"), cls="form-label"),
                    bank_select_or_msg,
                    cls="form-field",
                ),
                Div(
                    Label(t("label.statement_date"), cls="form-label"),
                    Input(type="date", name="statement_date",
                          value=(request.query_params.get("statement_date")
                                 or business_today(company.get("timezone")).isoformat()),
                          cls="form-input", required=True),
                    cls="form-field",
                ),
                Div(
                    Label(t("label.statement_closing_balance"), cls="form-label"),
                    Input(type="number", name="statement_balance", step=balance_step,
                          value=request.query_params.get("statement_balance", ""),
                          placeholder="0", cls="form-input", required=True),
                    cls="form-field",
                ),
                Div(
                    Button(t("btn.start_reconciliation"), type="submit", cls="btn btn--primary"),
                    cls="form-actions",
                ),
                action="/accounting/reconcile/start",
                method="post",
            ),
            cls="settings-card",
        )

        msg = request.query_params.get("msg", "").strip()
        return await base_shell(
            page_header(t("btn.start_reconciliation"),
                        A(t("btn.back"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary")),
            flash(msg) if msg else None,
            form,
            title=page_title("btn.start_reconciliation"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/start")
    async def reconcile_start_submit(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        bank_id = str(form.get("bank_account_id", "")).strip()
        stmt_date = str(form.get("statement_date", "")).strip()
        stmt_bal_raw = str(form.get("statement_balance", "0")).strip()
        if not bank_id or not stmt_date or not stmt_bal_raw:
            return RedirectResponse("/accounting/reconcile/start", status_code=302)
        try:
            stmt_bal = float(stmt_bal_raw)
            recon = await api.start_reconciliation(token, {
                "bank_account_id": bank_id,
                "statement_date": stmt_date,
                "statement_balance": stmt_bal,
            })
            session_id = recon.get("id") or recon.get("session_id")
            return RedirectResponse(f"/accounting/reconcile/{session_id}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            entered = {"bank_id": bank_id, "statement_date": stmt_date, "statement_balance": stmt_bal_raw}
            return RedirectResponse(
                "/accounting/reconcile/start?" + urlencode({"msg": str(e.detail), **entered}), status_code=302)

    @app.get("/accounting/reconcile/history/{bank_id}")
    async def reconcile_history(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            bank = await api.get_bank_account(token, bank_id)
            items = (await api.get_bank_reconciliations(token, bank_id)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse(
                "/settings/accounting?tab=bank-accounts&msg=" + quote(str(e.detail), safe=""),
                status_code=302,
            )
        status = request.query_params.get("status", "")
        if status not in _SESSION_STATUSES:
            status = ""
        base = f"/accounting/reconcile/history/{bank_id}"
        reconcile = A(t("acct.reconcile"), href=f"/accounting/reconcile/start?bank_id={bank_id}",
                      cls="btn btn--primary")
        cards = status_cards(
            [{"label": display_enum(s, "recon_session_status"),
              "count": sum(1 for i in items if i["status"] == s), "total": None, "status": s,
              "color": color} for s, color in _SESSION_STATUSES.items()],
            base, status or None)
        shown = [i for i in items if not status or i["status"] == status]
        return await base_shell(
            *_page_flashes(request),
            page_header(f"{t('recon.history')}: {bank.get('bank_name', '')}",
                        A(t("btn.back"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
                        reconcile),
            P(t("recon.history_intro"), cls="text-muted"),
            _relabel_notice(bank, base),
            cards,
            _history_table(shown, bank["currency"]) if items else Div(
                P(t("recon.history_empty"), cls="empty-state"), reconcile, cls="settings-card"),
            title=page_title("recon.history"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/relabel/{bank_id}")
    async def relabel_bank(request: Request, bank_id: str):
        """Set an account to the books' currency (``_relabel_notice``), then return to the
        page the notice was on, saying what changed and how to change it back."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        back = str((await request.form()).get("next", ""))
        if not back.startswith("/accounting/reconcile/") or "//" in back or "\\" in back:
            back = f"/accounting/reconcile/history/{bank_id}"
        try:
            bank = await api.get_bank_account(token, bank_id)
            base = bank.get("relabel_to")
            if base:
                await api.patch_bank_account(token, bank_id, {"currency": base})
                done = t("recon.relabel_done", currency=bank["currency"], base=base)
                return RedirectResponse(back + "?done=" + quote(done, safe=""), status_code=303)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse(back + "?msg=" + quote(str(e.detail), safe=""), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.get("/accounting/reconcile/{session_id}")
    async def reconcile_workspace(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            recon = await api.get_reconciliation(token, session_id)
            lines_data = await api.get_statement_lines(token, session_id)
            lines = lines_data.get("items", [])
            bank = recon["bank_account"]
            book_entries = _book_entries(recon)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse(
                "/settings/accounting?tab=bank-accounts&msg=" + quote(str(e.detail), safe=""),
                status_code=302,
            )

        workspace = _workspace_view(session_id, recon, bank, lines, book_entries, bank["currency"])
        return await base_shell(
            *_page_flashes(request),
            workspace,
            title=page_title("recon.workspace_title"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/{session_id}/import")
    async def reconcile_import_csv(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        csv_file = form.get("csv_file")
        if not csv_file or not hasattr(csv_file, "read"):
            return _workspace_redirect(session_id, t("recon.err_choose_csv"))
        try:
            content = await tabular.read_upload_bytes(csv_file, limit=_MAX_CSV_BYTES)
        except tabular.TabularError:
            return _workspace_redirect(session_id, t("recon.err_csv_too_large"))
        filename = csv_file.filename or "upload.csv"
        try:
            result = await api.import_recon_csv(token, session_id, content, filename)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return _workspace_redirect(session_id, str(e.detail))
        if result.get("needs_mapping"):
            csv_b64 = base64.b64encode(content).decode()
            return _mapper_fragment(session_id, list(result.get("headers") or []), csv_b64, filename)
        if not int(result.get("rows_imported", 0) or 0):
            return _workspace_redirect(session_id, t("recon.err_no_lines"))
        return _workspace_redirect(session_id)

    @app.post("/accounting/reconcile/{session_id}/confirm-import")
    async def confirm_import(request: Request, session_id: str):
        """Import with the user's explicit column mapping from the inline mapper."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        filename = str(form.get("csv_filename", "")).strip() or "upload.csv"
        try:
            content = base64.b64decode(str(form.get("csv_b64", "")), validate=True)
        except (binascii.Error, ValueError):
            return _workspace_redirect(session_id, t("recon.err_csv_corrupted"))
        if len(content) > _MAX_CSV_BYTES:
            return _workspace_redirect(session_id, t("recon.err_csv_too_large"))
        try:
            fieldnames, _ = tabular.read_csv(content.decode("utf-8-sig", errors="replace"))
        except tabular.TabularError as e:
            return _workspace_redirect(session_id, str(e))
        selections = {k[4:]: str(v) for k, v in form.items() if k.startswith("map_")}
        column_map = {
            canonical: header
            for header, canonical in selections.items()
            if canonical != "ignore"
        }
        # Mirrors the backend csv_parser requirement: a date, a description, and
        # at least one amount-like column. Kept in lockstep, not imported, so the
        # UI never couples to backend internals.
        missing = [f for f in ("date", "description") if f not in column_map]
        if not any(f in column_map for f in ("amount", "debit", "credit")):
            missing.append("amount (or debit/credit)")
        if missing:
            csv_b64 = base64.b64encode(content).decode()
            return _mapper_fragment(
                session_id, fieldnames, csv_b64, filename, selections=selections,
                error=t("recon.err_map_required") + ", ".join(missing),
            )
        if any(h not in fieldnames for h in column_map.values()):
            return _workspace_redirect(session_id, t("recon.err_columns_changed"))
        try:
            result = await api.import_recon_csv(token, session_id, content, filename, column_map=column_map)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return _workspace_redirect(session_id, str(e.detail))
        if not int(result.get("rows_imported", 0) or 0):
            return _workspace_redirect(session_id, t("recon.err_no_lines"))
        return _workspace_redirect(session_id)

    @app.get("/accounting/reconcile/{session_id}/lines/{line_id}/create-form")
    async def create_form_partial(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            bank = (await api.get_reconciliation(token, session_id))["bank_account"]
            lines_data = await api.get_statement_lines(token, session_id)
            line = next((l for l in lines_data.get("items", []) if l["id"] == line_id), None)
            if not line:
                return P(t("acct.line_not_found"), cls="error-banner")
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
            contacts = (await api.list_contacts(token, {"limit": PARTY_PRELOAD})).get("items") or []
        except APIError:
            return P(t("acct.error_loading_data"), cls="error-banner")
        return _create_form(session_id, line_id, line, chart, bank, contacts)

    @app.get("/accounting/reconcile/{session_id}/lines/{line_id}/split-form")
    async def split_form_partial(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            bank = (await api.get_reconciliation(token, session_id))["bank_account"]
            lines_data = await api.get_statement_lines(token, session_id)
            line = next((l for l in lines_data.get("items", []) if l["id"] == line_id), None)
            if not line:
                return P(t("acct.line_not_found"), cls="error-banner")
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
            contacts = (await api.list_contacts(token, {"limit": PARTY_PRELOAD})).get("items") or []
        except APIError:
            return P(t("acct.error_loading_data"), cls="error-banner")
        return _split_form(session_id, line_id, line, chart, bank, contacts)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/match-confirm")
    async def match_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        je_id = str(form.get("je_id", "")).strip()
        if not je_id:
            return await _fresh_workspace(token, session_id, notice=t("acct.missing_jeid"))
        try:
            await api.match_recon_line(token, session_id, line_id, je_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/create-confirm")
    async def create_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        account_code = str(form.get("account_code", "")).strip()
        memo = str(form.get("memo", "")).strip()
        amount_raw = str(form.get("amount", "")).strip()
        if not account_code:
            return await _fresh_workspace(token, session_id,
                                          notice=t("settings_accounting.code_required"))
        data = {"account_code": account_code, "memo": memo}
        # No party chosen means no contact key, so an entry nobody named posts
        # exactly the request it posted before the picker existed.
        contact = str(form.get("contact", "")).strip()
        if contact:
            data["contact"] = contact
        if amount_raw:
            try:
                data["amount"] = float(amount_raw)
            except ValueError:
                pass
        if refused := _form_rate(form, data):
            return await _fresh_workspace(token, session_id, notice=refused)
        try:
            await api.create_recon_expense(token, session_id, line_id, data)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/split-confirm")
    async def split_confirm(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        # Parse split-N fields
        splits = []
        i = 0
        while f"account_code_{i}" in form:
            code = str(form.get(f"account_code_{i}", "")).strip()
            amt_raw = str(form.get(f"amount_{i}", "")).strip()
            memo = str(form.get(f"memo_{i}", "")).strip()
            contact = str(form.get(f"contact_{i}", "")).strip()
            if code and amt_raw:
                try:
                    split = {"account_code": code, "amount": float(amt_raw), "memo": memo}
                    if contact:
                        split["contact"] = contact
                    splits.append(split)
                except ValueError:
                    pass
            i += 1
        if not splits:
            return await _fresh_workspace(token, session_id,
                                          notice=t("acct.at_least_one_split_entry_required"))
        extra: dict = {}
        if refused := _form_rate(form, extra):
            return await _fresh_workspace(token, session_id, notice=refused)
        try:
            await api.split_recon_line(token, session_id, line_id, {"splits": splits, **extra})
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/skip")
    async def skip_line(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            await api.skip_recon_line(token, session_id, line_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/lines/{line_id}/unmatch")
    async def unmatch_line(request: Request, session_id: str, line_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            await api.unmatch_recon_line(token, session_id, line_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/entries/{je_id}/unmatch")
    async def unmatch_entry(request: Request, session_id: str, je_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            await api.unmatch_reconciliation(token, session_id, [je_id])
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/auto-match")
    async def trigger_auto_match(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            await api.auto_match_recon(token, session_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/bulk-confirm")
    async def trigger_bulk_confirm(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            result = await api.bulk_confirm_recon(token, session_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        skipped = {s["line_id"]: s["reason"] for s in result.get("skipped", [])}
        if not skipped:
            return await _fresh_workspace(token, session_id)
        key = "recon.bulk_confirm_skipped_one" if len(skipped) == 1 else "recon.bulk_confirm_skipped"
        return await _fresh_workspace(token, session_id, notice=t(key, count=len(skipped)),
                                      notice_kind="warning", skipped_reasons=skipped)

    @app.post("/accounting/reconcile/{session_id}/complete")
    async def complete_recon(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            result = await api.complete_reconciliation(token, session_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return P(str(e.detail), cls="error-banner")
        return await base_shell(
            page_header(t("recon.complete_heading")),
            Div(
                P(t("recon.completed_for", date=result.get("statement_date", "--")),
                  cls="success-banner"),
                A(t("btn._back_to_accounting"), href="/settings/accounting?tab=bank-accounts",
                  cls="btn btn--primary"),
                A(t("recon.view_completed"), href=f"/accounting/reconcile/{session_id}",
                  cls="btn btn--secondary"),
                cls="settings-card",
            ),
            title=page_title("recon.complete_title"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/{session_id}/reopen")
    async def reopen_recon(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        try:
            await api.reopen_reconciliation(token, session_id)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)

    @app.post("/accounting/reconcile/{session_id}/write-off")
    async def trigger_write_off(request: Request, session_id: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        data: dict = {}
        if refused := _form_rate(await request.form(), data):
            return await _fresh_workspace(token, session_id, notice=refused)
        try:
            await api.write_off_recon(token, session_id, data)
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            return await _fresh_workspace(token, session_id, notice=str(e.detail))
        return await _fresh_workspace(token, session_id)
