# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → Accounting: Bank Accounts."""

from __future__ import annotations

import json

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.components.currency import currency_options
from ui.components.icons import import_icon
from ui.api_client import APIError
from ui.components.shell import base_shell, bug_report_url, flash, page_header, page_title
from celerp.accounting_roles import account_label
from celerp.services.lot_origin import OLDER_STOCK_PATH
from ui.components.table import (EMPTY, add_new_option, bulk_toolbar, searchable_select, display_enum, fmt_money,
                                 server_pager)

from ui.components.posting_accounts import account_picker
from ui.components.operation_key import operation_key_attrs, operation_key_input, required_operation_key
from ui.routes.accounting_import import ACCOUNT_TYPES, asset_register_target

# The cash flow sections an account may be pinned to. The accounting API owns this
# list (celerp_accounting.routes.CASH_FLOW_CATEGORIES) and validates against it; the
# two run in separate processes, so a test asserts they still match.
CASH_FLOW_CATEGORIES = ("operating", "investing", "financing")
# The longest account code the API accepts (celerp_accounting.chart_rules.ACCOUNT_CODE_MAX),
# mirrored the same way and asserted by the same kind of test.
_ACCOUNT_CODE_MAX = 32
from ui.routes.settings import _token, _check_permission
from ui.routes.settings_general import _section_breadcrumb
from ui.i18n import refusal_text, role_label, t


# Raw bank-account type values. Canonical everywhere (API, comparisons); the
# human label is resolved at render via display_enum(domain="bank_type").
_BANK_TYPES = ("checking", "savings", "credit_card")



def _accounting_settings_tabs(active: str) -> FT:
    tabs = [
        ("bank-accounts", t("settings_accounting.tab_bank_accounts")),
        ("chart", t("settings_accounting.chart_of_accounts")),
        ("rules", t("page.reconciliation_rules")),
        ("period-lock", t("page.period_lock")),
        ("posting-accounts", t("posting.tab")),
        ("older-stock", t("older_stock.tab")),
    ]
    return Div(
        *[
            A(label, href=f"/settings/accounting?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _bank_account_row(b: dict) -> FT:
    balance = b.get("balance", 0.0)
    bal_cls = "balance--positive" if balance >= 0 else "balance--negative"
    currency = b.get("currency", "")
    return Div(
        Div(
            Span(b.get("bank_name", ""), cls="account-name"),
            Span(f"{display_enum(b.get('bank_type', ''), 'bank_type')} · {b.get('account_number', '')} · {b.get('chart_account_code', '')}", cls="bank-name-label"),
            cls="bank-info",
        ),
        Div(
            Span(f"{currency} {balance:,.2f}", cls=f"balance {bal_cls}"),
            # The balance is the ledger's. Say so when an opening balance was
            # entered here that no journal entry carries, rather than letting the
            # figure look wrong for no stated reason.
            *([P(t("acct.opening_balance_unbacked"), cls="flash flash--warning")]
              if b.get("opening_unbacked") else []),
            Div(
                A(t("btn.edit"), href=f"/settings/accounting/bank-accounts/{b['id']}/edit",
                  cls="btn btn--secondary btn--xs"),
                A(t("acct.reconcile"), href=f"/accounting/reconcile/start?bank_id={b['id']}",
                  cls="btn btn--primary btn--xs"),
                A(t("recon.history"), href=f"/accounting/reconcile/history/{b['id']}",
                  cls="btn btn--secondary btn--xs"),
                Button(
                    t("inv.archive") if b.get("is_active") else t("btn.restore"),
                    hx_patch=f"/settings/accounting/bank-accounts/{b['id']}/toggle",
                    hx_target="#bank-accounts-list",
                    hx_swap="outerHTML",
                    cls="btn btn--secondary btn--xs",
                ),
                cls="row-actions flex-row gap-sm",
            ),
            cls="bank-balance-col flex-end gap-xs",
        ),
        cls="bank-account-card",
    )


def _bank_accounts_tab(banks: list[dict]) -> FT:
    active = [b for b in banks if b.get("is_active")]
    inactive = [b for b in banks if not b.get("is_active")]
    rows = [_bank_account_row(b) for b in active]
    archived = [_bank_account_row(b) for b in inactive] if inactive else []
    return Div(
        Div(
            A(t("acct.add_bank_account"), href="/settings/accounting/bank-accounts/new",
              cls="btn btn--primary"),
            cls="page-actions mb-md",
        ),
        Div(*rows, id="bank-accounts-list") if rows else Div(
            P(t("acct.no_bank_accounts_yet_add_one_to_start_tracking_cas"), cls="empty-state"),
            id="bank-accounts-list",
        ),
        *(
            [Details(
                Summary(t("acct.archived_accounts"), cls="text-muted mt-md"),
                Div(*archived),
            )] if archived else []
        ),
        cls="settings-card",
    )


def _period_lock_tab(lock_data: dict) -> FT:
    lock_date = lock_data.get("lock_date") or ""
    set_at = lock_data.get("lock_date_set_at") or ""
    firm_lock = lock_data.get("firm_lock")
    return Div(
        H3(t("page.period_lock"), cls="section-title"),
        P(
            t("settings_accounting.period_lock_help"),
            cls="text-muted mb-md",
        ),
        Form(
            Div(
                Label(t("label.lock_through_date")),
                Input(type="date", name="lock_date", value=lock_date,
                      cls="form-input max-w-md"),
                cls="form-field",
            ),
            Div(
                Button(t("btn.update_lock_date"), type="submit", cls="btn btn--primary"),
                *(
                    [Button(t("btn.unlock_remove_lock"), type="submit", name="unlock", value="1",
                            cls="btn btn--outline ml-sm")]
                    if lock_date else []
                ),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/period-lock",
            hx_target="#period-lock-content",
            hx_swap="outerHTML",
        ),
        *(
            [P(t("settings_accounting.currently_locked_through",
                 lock_date=lock_date,
                 updated=(set_at[:10] if set_at else t("settings_accounting.unknown"))),
               cls="text-muted mt-md")]
            if lock_date else []
        ),
        *(
            [P(t("settings_accounting.firm_lock", firm=firm_lock["firm"], date=firm_lock["date"]),
               cls="text-muted mt-sm")]
            if firm_lock else []
        ),
        Hr(cls="section-divider mt-lg mb-lg"),
        H3(t("page.close_fiscal_year"), cls="section-title"),
        P(
            t("settings_accounting.close_year_help"),
            cls="text-muted mb-md",
        ),
        Form(
            Div(
                Label(t("label.fiscal_year_end_date")),
                Input(type="date", name="fiscal_year_end",
                      cls="form-input max-w-md"),
                cls="form-field",
            ),
            Div(
                Button(t("btn.close_year"), type="submit", cls="btn btn--danger",
                       hx_confirm=t("settings_accounting.close_year_confirm")),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/close-year",
            hx_target="#period-lock-content",
            hx_swap="outerHTML",
        ),
        id="period-lock-content",
        cls="settings-card",
    )


def _rules_tab(rules: list[dict], banks: list[dict]) -> FT:
    bank_options = [Option(f"{b['bank_name']} {b.get('account_number', '')}", value=b["id"]) for b in banks]
    _bank_opt, _bank_js = add_new_option(t("settings_accounting.add_new_bank_account_option"), "/settings/accounting?tab=bank-accounts")
    _MATCH_TYPES = ("contains", "exact", "starts_with")

    rows = []
    for r in rules:
        rows.append(Tr(
            Td(r.get("match_pattern", EMPTY)),
            Td(display_enum(r.get("match_type"), "match_type") if r.get("match_type") else EMPTY),
            Td(r.get("target_account_code", EMPTY)),
            Td(r.get("default_memo") or EMPTY),
            Td(Span(t("th.active") if r.get("is_active") else t("settings.inactive"),
                    cls="badge badge--active" if r.get("is_active") else "badge badge--inactive")),
            Td(str(r.get("times_applied", 0))),
            Td(
                Button(t("btn.delete"),
                       hx_delete=f"/settings/accounting/rules/{r['id']}",
                       hx_target="#rules-list",
                       hx_swap="outerHTML",
                       hx_confirm=t("settings_accounting.delete_rule_confirm"),
                       cls="btn btn--xs btn--outline"),
            ),
        ))

    return Div(
        H3(t("page.reconciliation_rules"), cls="section-title"),
        P(t("acct.rules_autocategorise_matching_bank_statement_lines"),
          cls="text-muted mb-md"),
        Div(
            Table(
                Thead(Tr(Th(t("th.pattern")), Th(t("th.match_type")), Th(t("th.account")), Th(t("th.default_memo")),
                         Th(t("th.status")), Th(t("th.applied")), Th(""))),
                Tbody(*rows) if rows else Tbody(Tr(Td(t("acct.no_rules_yet"), colspan="7", cls="empty-state"))),
                cls="data-table",
            ),
            id="rules-list",
        ),
        Hr(cls="section-divider mt-lg mb-lg"),
        H3(t("page.add_rule"), cls="section-title"),
        Form(
            Div(
                Label(t("label.bank_account"), cls="form-label"),
                Select(*bank_options, _bank_opt, name="bank_account_id",
                       cls="form-input cell-input--select", onchange=_bank_js),
                cls="form-field",
            ),
            Div(
                Label(t("label.match_pattern"), cls="form-label"),
                Input(type="text", name="match_pattern", placeholder=t("settings_accounting.ph_match_pattern"),
                      cls="form-input", required=True),
                cls="form-field",
            ),
            Div(
                Label(t("th.match_type"), cls="form-label"),
                Select(*[Option(display_enum(v, "match_type"), value=v) for v in _MATCH_TYPES],
                       name="match_type", cls="form-input cell-input--select"),
                cls="form-field",
            ),
            Div(
                Label(t("label.target_account_code"), cls="form-label"),
                Input(type="text", name="target_account_code", placeholder=t("settings_accounting.ph_account_code"),
                      cls="form-input", required=True),
                cls="form-field",
            ),
            Div(
                Label(t("label.default_memo_optional"), cls="form-label"),
                Input(type="text", name="default_memo", cls="form-input"),
                cls="form-field",
            ),
            Div(
                Button(t("page.add_rule"), type="submit", cls="btn btn--primary"),
                cls="form-actions mt-md",
            ),
            hx_post="/settings/accounting/rules",
            hx_target="#rules-list",
            hx_swap="outerHTML",
        ),
        cls="settings-card",
    )


def _cash_flow_display_cell(code: str, cash_flow_category: str | None) -> FT:
    """Read-only cash flow section cell. Click fires HTMX GET to fetch the select
    editor in place, matching the click-to-edit cells used elsewhere (e.g. the
    document numbering table in settings_sales.py)."""
    display = (display_enum(cash_flow_category, "cash_flow") if cash_flow_category else "") or EMPTY
    return Td(
        Div(
            display,
            hx_get=f"/settings/accounting/chart/{code}/cash-flow/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title=t("settings.click_to_edit"), cls="editable-cell",
        ),
    )


def _cash_flow_edit_cell(a: dict) -> FT:
    """Table cell in edit mode: a select of the derived option plus the three
    cash flow sections. Fires HTMX PATCH on change, swaps itself back on save."""
    code = a.get("code", "")
    current = a.get("cash_flow_category") or ""
    restore_url = f"/settings/accounting/chart/{code}/cash-flow/display"
    esc_js = (
        f"if(event.key==='Escape'){{htmx.ajax('GET','{restore_url}',"
        f"{{target:this.closest('td'),swap:'outerHTML'}});event.preventDefault();}}"
    )
    return Td(
        Select(
            Option(t("acct.cash_flow_derived"), value="", selected=not current),
            *[Option(display_enum(x, "cash_flow"), value=x, selected=(x == current)) for x in CASH_FLOW_CATEGORIES],
            name="value",
            hx_patch=f"/settings/accounting/chart/{code}/cash-flow",
            hx_target="closest td", hx_swap="outerHTML", hx_trigger="change",
            cls="cell-input cell-input--select", autofocus=True,
            onkeydown=esc_js,
        ),
        cls="cell cell--editing",
    )


_POSTING_BADGE = {"ready": "active", "unused": "inactive"}


def _posting_display_cell(key: str, account: dict | None, error: str | None = None) -> FT:
    """The account a role posts to; a click swaps in the picker (click-to-edit)."""
    return Td(
        Span(account_label(account) or EMPTY),
        P(error, cls="cell-error") if error else None,
        hx_get=f"/settings/accounting/posting-accounts/{key}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        title=t("settings.click_to_edit"), cls="cell cell--text cell--clickable",
    )


def _posting_edit_cell(key: str, candidates: list[dict], code: str | None, label: str) -> FT:
    """The picker in place of the account. A change saves and swaps in the updated
    row; Escape puts the display cell back without saving."""
    restore_url = f"/settings/accounting/posting-accounts/{key}/display"
    esc_js = (
        f"if(event.key==='Escape'){{htmx.ajax('GET','{restore_url}',"
        f"{{target:this.closest('td'),swap:'outerHTML'}});event.preventDefault();}}"
    )
    return Td(
        Div(
            account_picker("value", candidates, value=code or "", aria_label=label,
                           hx_patch=f"/settings/accounting/posting-accounts/{key}",
                           hx_target="closest tr", hx_swap="outerHTML", hx_trigger="change", autofocus=True),
            cls="cell-input-wrap", onkeydown=esc_js,
        ),
        cls="cell cell--editing",
    )


def _posting_role_row(row: dict, error: str | None = None) -> FT:
    status = row["status"]
    return Tr(
        Td(role_label(row["role"], row["label"])),
        _posting_display_cell(row["role"], row, error),
        Td(Span(t(f"posting.status_{status}"), cls=f"badge badge--{_POSTING_BADGE.get(status, 'overdue')}"),
           P(refusal_text(row["problem"]), cls="text-muted") if row.get("problem") else None),
        Td(", ".join(row.get("earlier") or []) or EMPTY),
        id=f"posting-{row['role']}",
    )


def _posting_row(data: dict, key: str, error: str | None = None) -> FT | None:
    row = next((r for r in data.get("roles", []) if r["role"] == key), None)
    return _posting_role_row(row, error) if row else None


def _posting_accounts_tab(data: dict) -> FT:
    return Div(
        H3(t("posting.tab"), cls="section-title"),
        P(t("posting.panel_hint"), cls="text-muted mb-md"),
        Div(Table(
            Thead(Tr(Th(t("posting.col_role")), Th(t("posting.col_account")), Th(t("th.status")),
                     Th(t("posting.col_earlier")))),
            Tbody(*[_posting_role_row(r) for r in data.get("roles", [])]),
            cls="data-table posting-accounts",
        ), cls="table-scroll-wrap"),
        cls="settings-card",
    )


_OLDER_STOCK_URL = "/settings/accounting/older-stock"
# The lists GET /accounting/older-stock pages, each from its own ``<name>_offset``.
_OLDER_STOCK_PAGES = ("lots", "placements", "moves", "corrections")
_OLDER_STOCK_MAX_PAGE = 500
# Above this many accounts a choice of account is searchable (GDR 2i).
_SEARCHABLE_AFTER = 10


def _older_stock_filters(filters: dict, accounts: list[str]) -> FT:
    """The account and SKU/name filters, passed in the URL (GDR 2m)."""
    options = [("", t("older_stock.all_accounts"))] + [(a, a) for a in accounts]
    account = filters.get("account", "")
    picker = (searchable_select("account", options, value=account, aria_label=t("older_stock.col_account"))
              if len(options) > _SEARCHABLE_AFTER else
              Select(*[Option(label, value=v, selected=v == account) for v, label in options],
                     name="account", cls="form-input", aria_label=t("older_stock.col_account")))
    return Form(
        Input(type="hidden", name="tab", value="older-stock"),
        picker,
        Input(type="search", name="q", value=filters.get("q", ""), placeholder=t("older_stock.search"),
              cls="form-input", onkeydown="if(event.key==='Escape'){this.value='';this.blur();}"),
        Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
        method="get", action="/settings/accounting", cls="filter-bar",
    )


def _older_lot_row(lot: dict, currency: str) -> FT:
    return Tr(
        Td(Input(type="checkbox", cls="bulk-select", name="selected", value=lot["item_id"]), cls="col-checkbox"),
        Td(lot["sku"] or EMPTY),
        Td(lot["name"] or EMPTY),
        Td(lot["account"] or EMPTY),
        Td(fmt_money(lot["value"], currency), cls="cell--number"),
        cls="data-row",
    )


def _move_target(view: dict, **attrs) -> FT:
    """The account picker for a move: the accounts the move accepts (``targets``)."""
    return searchable_select("code", [(c["code"], account_label(c)) for c in view.get("targets", [])],
                             placeholder=t("posting.choose"), aria_label=t("older_stock.move_to"), **attrs)


def _move_all_form(view: dict, filters: dict) -> FT | None:
    """Move every lot matching the filters, on every page, as one move: the button states
    how many and their value, and asks before moving. The selection's fingerprint rides
    along so the server refuses if the lots matching differ by then."""
    matching = view.get("matching") or {}
    n = int(matching.get("count") or 0)
    if not n:
        return None
    value = fmt_money(matching.get("value") or 0, view.get("currency"))
    return Form(
        _move_target(view),
        Input(type="hidden", name="selection", value=matching.get("selection") or ""),
        operation_key_input(),
        Button(t("older_stock.move_all", n=n, value=value), type="submit", cls="btn btn--sm btn--secondary"),
        id="older-stock-move-all", cls="filter-bar",
        hx_confirm=t("older_stock.move_all_confirm", n=n, value=value),
        hx_post=_older_stock_url(filters, "/move-all"), hx_target="#older-stock-card", hx_swap="outerHTML",
    )


def _older_stock_url(filters: dict, path: str = "", **changes) -> str:
    """An Older stock URL keeping the filters and pages shown, with ``changes`` applied:
    the tab itself, or an action at ``path`` whose answer re-renders the same view."""
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in {**filters, **changes}.items() if v not in ("", None)})
    base = f"{_OLDER_STOCK_URL}{path}" if path else OLDER_STOCK_PATH
    return base + ("&" if "?" in base else "?") + query if query else base


def _money(value, currency: str) -> FT | str:
    return fmt_money(value, currency) if value else EMPTY


def _correction_url(filters: dict, kind: str) -> str:
    return _older_stock_url(filters, "/corrections", kind=kind)


def _correction(pid: str, kind: str, label: str, filters: dict, confirm: str | None = None) -> FT:
    return Button(label, type="button", cls="btn btn--xs btn--secondary",
                  hx_post=_correction_url(filters, kind), hx_vals=json.dumps({"selected": pid}),
                  hx_target="#older-stock-card", hx_swap="outerHTML", hx_confirm=confirm)


def _retained(view: dict) -> str:
    """The account a write-off posts to, as users read it (the role while it is unmapped)."""
    return account_label(view.get("retained")) or role_label("retained_earnings", "Retained earnings")


def _uncertain_text(found: dict, currency: str | None) -> str:
    """Lines on an account that may or may not have been stock: the account, their
    amount and the entries holding them (lot_origin.uncertain_evidence)."""
    return t("older_stock.open.uncertain", account=found["account"], amount=fmt_money(found["amount"], currency),
             entries=", ".join(found["entries"]))


def _placement_row(p: dict, currency: str, filters: dict, retained: str) -> FT:
    """A placement: its classification, what it transferred between accounts, recognized as
    opening stock and left unresolved, the journal entries it posted, and the correction
    each difference allows (lot_origin.correct_placements). A surplus stays open as a plain
    line with one explicit write-off, and a shortfall is booked the same way, each
    confirmed naming the account it posts to and undone in one click. Lines that may or
    may not have been stock are shown as evidence to review, with nothing to book."""
    surplus = fmt_money(p["unresolved"], currency) if p["unresolved"] > 0 else None
    actions = [Div(_uncertain_text(u, currency), cls="text-muted") for u in p.get("uncertain") or []] \
        + ([Div(t("older_stock.surplus", amount=surplus)),
            _correction(p["pid"], "recognize", t("older_stock.write_off"), filters,
                        t("older_stock.write_off_confirm", amount=surplus, account=retained))] if surplus else []) \
        + ([_correction(p["pid"], "recognize", t("older_stock.book_difference"), filters,
                        t("older_stock.book_difference_confirm", amount=fmt_money(-p["unresolved"], currency),
                          account=retained))]
           if p["unresolved"] < 0 else []) \
        + ([_correction(p["pid"], "unresolve", t("older_stock.leave_unresolved"), filters)]
           if p["recognized"] > 0 else []) \
        + ([_correction(p["pid"], "unresolve", t("older_stock.undo_write_off"), filters)]
           if p["recognized"] < 0 else [])
    return Tr(
        Td(Input(type="checkbox", cls="bulk-select", name="selected", value=p["pid"]), cls="col-checkbox"),
        Td(p["day"], cls="cell--center"),
        Td(str(p["count"]), cls="cell--number"),
        Td(p["account"] or EMPTY),
        Td(display_enum(p["classification"], "older_stock_classification") if p.get("classification") else EMPTY),
        Td(_money(p["transferred"], currency), cls="cell--number"),
        Td(_money(p["recognized"], currency), cls="cell--number"),
        Td(_money(p["unresolved"], currency), cls="cell--number"),
        Td(*([Div(j, cls="cell--mono") for j in p["journal"]] or [EMPTY])),
        Td(*(actions or [EMPTY]), cls="cell--actions"),
        cls="data-row",
    )


def _summary_row(s: dict, currency: str) -> FT:
    return Tr(
        Td(s["account"]),
        Td(str(s["lots"]), cls="cell--number"),
        Td(_money(s["value"], currency), cls="cell--number"),
        Td(_money(s["transferred"], currency), cls="cell--number"),
        Td(_money(s["recognized"], currency), cls="cell--number"),
        Td(_money(s["unresolved"], currency), cls="cell--number"),
    )


def _correction_row(c: dict, currency: str) -> FT:
    return Tr(
        Td(c["day"], cls="cell--center"),
        Td(t(f"older_stock.kind.{c['kind']}")),
        Td(c.get("account") or EMPTY),
        Td(fmt_money(c["amount"], currency), cls="cell--number"),
        Td(c["journal"], cls="cell--mono"),
        Td(c.get("actor_name") or EMPTY),
    )


def _move_row(m: dict, currency: str, filters: dict) -> FT:
    """A move, undoable until a later move takes one of its lots elsewhere."""
    if m.get("undo_of"):
        action = Span(t("older_stock.undo_of"), cls="text-muted")
    elif m["undone"]:
        action = Span(t("older_stock.undone"), cls="text-muted")
    else:
        action = Button(t("btn.undo"), type="button", cls="btn btn--xs btn--secondary",
                        hx_post=_older_stock_url(filters, f"/moves/{m['move_id']}/undo"),
                        hx_target="#older-stock-card", hx_swap="outerHTML")
    return Tr(
        Td(m["day"], cls="cell--center"),
        Td(str(m["count"]), cls="cell--number"),
        Td(m["code"] or EMPTY),
        Td(fmt_money(m["value"], currency), cls="cell--number"),
        Td(action, cls="cell--actions"),
    )


_RECONCILIATION_BANNER = {"action": "error-banner", "review": "info-banner", "reconciled": "success-banner"}


def _reconciliation_summary(rec: dict | None, currency: str | None) -> FT | None:
    """Where the older stock stands with the books (lot_origin._reconciliation): the state,
    why stock waits with a link to the setting to fix, or each open difference with the
    way to review it in the placements below."""
    if not rec:
        return None
    state = rec["state"]
    reasons = [Li(refusal_text({"message": "", "message_key": r.get("body", ""), "params": r.get("params") or {}}),
                  " ", A(t("older_stock.state.fix"), href=r["action_url"])
                  if r.get("action_url") not in (None, OLDER_STOCK_PATH) else None)
               for r in rec.get("reasons") or []]
    opened = [Li(_uncertain_text(o, currency) if o["kind"] == "uncertain" else
                 t(f"older_stock.open.{o['kind']}", account=o["account"], amount=fmt_money(o["amount"], currency),
                   **{k: fmt_money(o[k], currency) for k in ("books", "stock") if k in o}))
              for o in rec.get("open") or []]
    return Div(
        H4(t(f"older_stock.state.{state}")),
        P(t(f"older_stock.state.{state}_hint", n=rec.get("waiting") or 0)),
        Ul(*reasons, *opened) if reasons or opened else None,
        A(t("older_stock.state.review_placements"), href="#older-stock-placements", cls="btn btn--sm btn--secondary")
        if state == "review" else None,
        cls=f"{_RECONCILIATION_BANNER[state]} mb-md", id="older-stock-reconciliation",
    )


def _placement_failure(failed: dict | None) -> FT | None:
    """A standing failure to place older stock: what it means, Try again, and a bug
    report pre-filled with the kind of error only."""
    if not failed:
        return None
    report = bug_report_url(area="Accounting",
                            current=f"Older stock could not be placed ({failed.get('error') or 'unknown error'}).")
    return Div(
        H4(t("notice.older_stock_failed.title")),
        P(t("older_stock.failed")),
        Div(Button(t("btn.retry"), type="button", cls="btn btn--sm btn--primary",
                   hx_post=f"{_OLDER_STOCK_URL}/retry", hx_target="#older-stock-card", hx_swap="outerHTML"),
            A(t("nav.report_bug"), href=report, target="_blank", rel="noopener", cls="btn btn--sm btn--secondary"),
            cls="form-actions"),
        cls="error-banner mb-md",
    )


def _older_stock_card(view: dict, filters: dict, message: str = "", kind: str = "success") -> FT:
    """Settings > Accounting > Older stock: each inventory account with its lots and what
    placements transferred, recognized and left unresolved; the lots placed automatically
    with a bulk move to another inventory account; each placement with its classification,
    journal entries and correction, one at a time or in bulk; every move; and every
    correction with who made it. Newest first, each list paged on its own."""
    currency = view.get("currency")
    lots, placements, moves = view.get("lots", []), view.get("placements", []), view.get("moves", [])
    summary, corrections = view.get("summary", []), view.get("corrections", [])
    totals, limit = view.get("totals", {}), view.get("limit") or 50
    retained = _retained(view)
    target = _move_target(view, cls="bulk-field")

    def pager(name: str) -> FT | str:
        return server_pager(int(filters.get(f"{name}_offset") or 0), limit, totals.get(name, 0),
                            lambda o, n: _older_stock_url(filters, **{f"{name}_offset": o, "limit": n}))

    def table(table_id: str, head: list, rows: list, **attrs) -> FT:
        return Div(Table(Thead(Tr(*head)), Tbody(*rows), cls="data-table", id=table_id, **attrs),
                   cls="table-scroll-wrap")

    select_all = Th(Input(type="checkbox", cls="bulk-select-all", title=t("label.select_all")), cls="col-checkbox")
    number = {"cls": "cell--number"}
    if lots:
        lot_table = table("older-stock-table", [
            select_all, Th(t("th.sku")), Th(t("th.name")), Th(t("older_stock.col_account")),
            Th(t("older_stock.col_value"), **number)],
            [_older_lot_row(lot, currency) for lot in lots], **operation_key_attrs())
    else:
        lot_table = P(t("older_stock.no_match") if filters.get("account") or filters.get("q")
                      else t("older_stock.empty"), cls="hint", id="older-stock-table")
    return Div(
        flash(message, kind) if message else None,
        H3(t("older_stock.tab"), cls="section-title"),
        _reconciliation_summary(view.get("reconciliation"), currency),
        _placement_failure(view.get("failed")),
        P(t("older_stock.hint"), cls="text-muted mb-md"),
        H4(t("older_stock.summary")) if summary else None,
        table("older-stock-summary", [
            Th(t("older_stock.col_account")), Th(t("older_stock.col_lots"), **number),
            Th(t("older_stock.col_value"), **number), Th(t("older_stock.col_transferred"), **number),
            Th(t("older_stock.col_recognized"), **number), Th(t("older_stock.col_unresolved"), **number)],
            [_summary_row(s, currency) for s in summary]) if summary else None,
        _older_stock_filters(filters, view.get("accounts", [])),
        bulk_toolbar("older-stock-table", [
            {"value": "move", "label": t("older_stock.move"), "method": "post",
             "url": _older_stock_url(filters, "/move"), "target": "#older-stock-card", "fields": True}],
            fields=[target]),
        lot_table,
        pager("lots"),
        _move_all_form(view, filters),
        H4(t("older_stock.placements"), cls="mt-md"),
        bulk_toolbar("older-stock-placements", [
            {"value": "recognize", "label": t("older_stock.book_difference"), "method": "post",
             "url": _correction_url(filters, "recognize"), "target": "#older-stock-card",
             "confirm": t("older_stock.bulk_recognize_confirm", n="{n}", account=retained)},
            {"value": "unresolve", "label": t("older_stock.leave_unresolved"), "method": "post",
             "url": _correction_url(filters, "unresolve"), "target": "#older-stock-card"}]) if placements else None,
        table("older-stock-placements", [
            select_all, Th(t("th.date"), cls="cell--center"), Th(t("older_stock.col_lots"), **number),
            Th(t("older_stock.col_account")), Th(t("older_stock.col_classification")),
            Th(t("older_stock.col_transferred"), **number), Th(t("older_stock.col_recognized"), **number),
            Th(t("older_stock.col_unresolved"), **number), Th(t("older_stock.col_journal")), Th("")],
            [_placement_row(p, currency, filters, retained) for p in placements])
        if placements else P(t("older_stock.no_placements"), cls="hint"),
        pager("placements"),
        H4(t("older_stock.moves"), cls="mt-md"),
        table("older-stock-moves", [
            Th(t("th.date"), cls="cell--center"), Th(t("older_stock.col_lots"), **number),
            Th(t("older_stock.col_to")), Th(t("older_stock.col_value"), **number), Th("")],
            [_move_row(m, currency, filters) for m in moves]) if moves else P(t("older_stock.no_moves"), cls="hint"),
        pager("moves"),
        H4(t("older_stock.corrections"), cls="mt-md"),
        table("older-stock-corrections", [
            Th(t("th.date"), cls="cell--center"), Th(t("older_stock.col_correction")),
            Th(t("older_stock.col_account")), Th(t("older_stock.col_amount"), **number),
            Th(t("older_stock.col_journal")), Th(t("older_stock.col_by"))],
            [_correction_row(c, currency) for c in corrections])
        if corrections else P(t("older_stock.no_corrections"), cls="hint"),
        pager("corrections"),
        cls="settings-card", id="older-stock-card",
    )


def _older_stock_filters_of(request: Request) -> dict:
    """The filters and pages the Older stock tab shows, read from the URL (GDR 2m). A page
    value that is not a whole number in range is left out, so the first page shows."""
    params = request.query_params
    filters = {k: v for k in ("account", "q") if (v := params.get(k, "").strip())}
    for key in (*(f"{name}_offset" for name in _OLDER_STOCK_PAGES), "limit"):
        raw = params.get(key, "").strip()
        if raw.isdigit() and (key != "limit" or 1 <= int(raw) <= _OLDER_STOCK_MAX_PAGE):
            filters[key] = raw
    return filters


async def _older_stock_action(request: Request, call) -> FT:
    """Run an older-stock action and answer with the card again: the outcome on success,
    the API's reason on a refusal, read within the filters the page was showing."""
    token = _token(request)
    if not token:
        return P(t("error.session_expired"), cls="cell-error")
    filters = _older_stock_filters_of(request)
    try:
        view = await call(token)
        message, kind = view.pop("message", ""), view.pop("kind", "success")
        if any(filters.values()):
            view = await api.get_older_stock(token, filters)
    except APIError as e:
        message, kind = refusal_text(e.data or e.detail) or str(e.detail), "error"
        try:
            view = await api.get_older_stock(token, filters)
        except APIError as e2:
            return Div(P(refusal_text(e2.data or e2.detail) or str(e2.detail), cls="error-banner"),
                       cls="settings-card", id="older-stock-card")
    return _older_stock_card(view, filters, message, kind)


def _chart_table(chart: list[dict]) -> FT:
    def _row(a: dict) -> FT:
        code = a.get("code", "")
        return Tr(
            Td(code, cls="cell--mono"),
            Td(a.get("name", "")),
            Td(Span(display_enum(a.get("account_type", ""), "account_type"), cls=f"badge badge--{a.get('account_type', '')}")),
            Td(a.get("parent_code") or EMPTY),
            _cash_flow_display_cell(code, a.get("cash_flow_category")),
            Td(Span(t("th.active") if a.get("is_active", True) else t("settings.inactive"),
                    cls="badge badge--active" if a.get("is_active", True) else "badge badge--inactive")),
            Td(A(t("btn.edit"), href=f"/settings/accounting/chart/{code}/edit",
                 cls="btn btn--secondary btn--xs")),
            cls="data-row",
        )

    by_type: dict[str, list] = {}
    for a in chart:
        atype = a.get("account_type", "other")
        by_type.setdefault(atype, []).append(a)

    sections: list[FT] = []
    for atype in ACCOUNT_TYPES:
        accounts = by_type.get(atype, [])
        if not accounts:
            continue
        sections.append(Tr(Th(display_enum(atype, "account_type"), colspan="7", cls="section-header")))
        sections.extend(_row(a) for a in accounts)

    return Table(
        Thead(Tr(Th(t("th.code")), Th(t("th.name")), Th(t("th.doc_type")), Th(t("th.parent")),
                 Th(t("th.cash_flow_section")), Th(t("th.status")), Th(""))),
        Tbody(*sections),
        cls="data-table sticky-head",
    )


def _chart_tab(chart: list[dict]) -> FT:
    return Div(
        Div(
            A(t("acct.add_account"), href="/settings/accounting/chart/new", cls="btn btn--primary"),
            A(import_icon(), t("acct.import_chart_csv"), href="/accounting/import/chart", cls="btn btn--secondary"),
            A(import_icon(), t("acct.import_asset_register"), href="/import/asset-register", cls="btn btn--secondary")
            if asset_register_target() else None,
            Form(
                Button(t("btn.seed_default_chart"), type="submit", cls="btn btn--secondary"),
                hx_post="/settings/accounting/chart/seed",
                hx_target="#chart-content",
                hx_swap="outerHTML",
                style="display:inline",
            ) if not chart else None,
            cls="page-actions flex-row gap-sm mb-md",
        ),
        Div(_chart_table(chart), id="chart-content") if chart else Div(
            P(t("acct.no_accounts_yet_seed_the_default_chart_or_add_acco"), cls="empty-state"),
            id="chart-content",
        ),
        cls="settings-card",
    )


def _account_form(chart: list[dict], values: dict | None = None) -> FT:
    """Add/edit form for a chart account. Pass values to edit an existing one."""
    v = values or {}
    editing = bool(v)
    code = v.get("code", "")
    parent_opts = [("", EMPTY)] + [
        (a["code"], f"{a['code']} {a.get('name', '')}")
        for a in chart if a.get("code") and a.get("code") != code
    ]
    rows = [
        Tr(
            Td(t("th.code"), cls="detail-label"),
            Td(Span(code, cls="cell--mono")) if editing else
            Td(Input(type="text", name="code", placeholder=t("settings_accounting.ph_code"), maxlength="32",
                     cls="cell-input", required=True)),
        ),
        Tr(
            Td(t("th.name"), cls="detail-label"),
            Td(Input(type="text", name="name", value=v.get("name", ""),
                     placeholder=t("settings_accounting.ph_name"), cls="cell-input", required=True)),
        ),
        Tr(
            Td(t("th.doc_type"), cls="detail-label"),
            Td(Select(
                *[Option(display_enum(x, "account_type"), value=x, selected=(x == v.get("account_type", "asset")))
                  for x in ACCOUNT_TYPES],
                name="account_type", cls="cell-input cell-input--select",
            )),
        ),
        Tr(
            Td(t("th.parent"), cls="detail-label"),
            Td(searchable_select("parent_code", parent_opts, value=v.get("parent_code") or "")),
        ),
    ]
    rows.append(Tr(
        Td(t("th.cash_flow_section"), cls="detail-label"),
        Td(Select(
            Option(t("acct.cash_flow_derived"), value="",
                   selected=not v.get("cash_flow_category")),
            *[Option(display_enum(x, "cash_flow"), value=x, selected=(x == v.get("cash_flow_category")))
              for x in CASH_FLOW_CATEGORIES],
            name="cash_flow_category", cls="cell-input cell-input--select",
        )),
    ))
    if editing:
        rows.append(Tr(
            Td(t("th.status"), cls="detail-label"),
            Td(Select(
                Option(t("th.active"), value="true", selected=bool(v.get("is_active", True))),
                Option(t("settings.inactive"), value="false", selected=not v.get("is_active", True)),
                name="is_active", cls="cell-input cell-input--select",
            )),
        ))
    action = {"hx_patch": f"/settings/accounting/chart/{code}"} if editing else \
             {"hx_post": "/settings/accounting/chart/new"}
    return Form(
        Div(
            Table(*rows, cls="detail-table"),
            Div(
                Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                A(t("btn.cancel"), href="/settings/accounting?tab=chart",
                  cls="btn btn--secondary ml-sm"),
                cls="mt-md",
            ),
            Div(id="account-form-error"),
        ),
        onkeydown="if(event.key==='Escape'){window.location='/settings/accounting?tab=chart'}",
        hx_target="#account-form-error",
        hx_swap="innerHTML",
        **action,
    )


async def _account_error_page(request: Request, message: str) -> FT:
    return await base_shell(
        _section_breadcrumb(t("page.accounting")),
        page_header(
            t("settings_accounting.chart_of_accounts"),
            A(t("btn.back"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
        ),
        Div(P(message, cls="error-banner"), cls="settings-card"),
        title=page_title("settings_accounting.chart_of_accounts"),
        nav_active="settings-accounting",
        request=request,
    )


async def _validate_account(token: str, name: str, account_type: str,
                            parent_code: str, own_code: str) -> str | None:
    """Field-level validation shared by account create and patch. Returns an
    error message, or None when the fields are valid."""
    if not name:
        return t("settings_accounting.name_required")
    if account_type not in ACCOUNT_TYPES:
        return t("settings_accounting.account_type_invalid", types=", ".join(t(f"enum.account_type.{x}") for x in ACCOUNT_TYPES))
    if parent_code:
        if parent_code == own_code:
            return t("settings_accounting.account_own_parent")
        try:
            chart = (await api.get_chart(token)).get("items", [])
        except APIError as e:
            return str(e.detail)
        if not any(a.get("code") == parent_code for a in chart):
            return t("settings_accounting.parent_account_not_exist", code=parent_code)
    return None


def setup_routes(app):

    @app.get("/settings/accounting")
    async def settings_accounting_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_module_settings")):
            return r
        tab = request.query_params.get("tab", "bank-accounts")
        try:
            banks_data = await api.get_bank_accounts(token, include_inactive=True)
            banks = banks_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            banks = []

        if tab == "bank-accounts":
            content = _bank_accounts_tab(banks)
        elif tab == "chart":
            try:
                chart_data = await api.get_chart(token)
                content = _chart_tab(chart_data.get("items", []))
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                content = Div(P(str(e.detail), cls="error-banner"), cls="settings-card")
        elif tab == "rules":
            try:
                rules_data = await api.get_recon_rules(token)
                rules = rules_data.get("items", [])
            except Exception:
                rules = []
            content = _rules_tab(rules, banks)
        elif tab == "period-lock":
            try:
                lock_data = await api.get_period_lock(token)
            except Exception:
                lock_data = {}
            content = _period_lock_tab(lock_data)
        elif tab == "posting-accounts":
            try:
                content = _posting_accounts_tab(await api.get_posting_accounts(token))
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                content = Div(P(str(e.detail), cls="error-banner"), cls="settings-card")
        elif tab == "older-stock":
            filters = _older_stock_filters_of(request)
            try:
                content = _older_stock_card(await api.get_older_stock(token, filters), filters)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                content = Div(P(str(e.detail), cls="error-banner"), cls="settings-card")
        else:
            content = _bank_accounts_tab(banks)
            tab = "bank-accounts"

        msg = request.query_params.get("msg", "").strip()
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(t("settings_accounting.finance_settings")),
            flash(msg) if msg else None,
            _accounting_settings_tabs(tab),
            content,
            title=page_title("settings_accounting.finance_settings"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.get("/settings/accounting/bank-accounts/new")
    async def new_bank_account_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
            currency = company.get("currency", "USD")
        except APIError:
            currency = "USD"

        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("acct.add_bank_account"),
                A(t("btn.back"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
            ),
            Div(
                Form(
                    Div(
                        Table(
                            Tr(
                                Td(t("acct.bank_name"), cls="detail-label"),
                                Td(Input(type="text", name="bank_name", placeholder=t("settings_accounting.ph_bank_name"),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_number"), cls="detail-label"),
                                Td(Input(type="text", name="account_number", placeholder=t("settings_accounting.ph_account_number"),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_type"), cls="detail-label"),
                                Td(Select(
                                    *[Option(display_enum(v, "bank_type"), value=v) for v in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(
                                    searchable_select("currency", currency_options(), value=currency,
                                                      aria_label=t("th.currency")),
                                    P(t("acct.using_a_foreigncurrency_account_the_multicurrency"), cls="form-hint"),
                                ),
                            ),
                            Tr(
                                Td(t("acct.opening_balance"), cls="detail-label"),
                                Td(Input(type="number", name="opening_balance", value="0",
                                         step="0.01", cls="cell-input cell-input--number")),
                            ),
                            cls="detail-table",
                        ),
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                            A(t("btn.cancel"), href="/settings/accounting?tab=bank-accounts",
                              cls="btn btn--secondary ml-sm"),
                            cls="mt-md",
                        ),
                        Div(id="bank-form-error"),
                    ),
                    hx_post="/settings/accounting/bank-accounts/new",
                    hx_target="#bank-form-error",
                    hx_swap="innerHTML",
                ),
                cls="settings-card",
            ),
            title=page_title("acct.add_bank_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.post("/settings/accounting/bank-accounts/new")
    async def create_bank_account_submit(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        bank_name = str(form.get("bank_name", "")).strip()
        account_number = str(form.get("account_number", "")).strip()
        bank_type = str(form.get("bank_type", "checking")).strip()
        currency = str(form.get("currency", "USD")).strip().upper()
        opening_balance_raw = str(form.get("opening_balance", "0")).strip()
        if not bank_name or not account_number:
            return P(t("acct.bank_name_and_account_number_are_required"), cls="error-banner")
        try:
            opening_balance = float(opening_balance_raw)
        except ValueError:
            return P(t("acct.opening_balance_must_be_a_number"), cls="error-banner")
        try:
            await api.create_bank_account(token, {
                "bank_name": bank_name,
                "account_number": account_number,
                "bank_type": bank_type,
                "currency": currency,
                "opening_balance": opening_balance,
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=bank-accounts"})

    @app.get("/settings/accounting/bank-accounts/{bank_id}/edit")
    async def edit_bank_account_page(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            b = await api.get_bank_account(token, bank_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse("/settings/accounting?tab=bank-accounts", status_code=302)

        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("settings_accounting.edit_named", name=b.get("bank_name") or t("settings_accounting.bank_account")),
                A(t("btn.back"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
            ),
            Div(
                Form(
                    Div(
                        Table(
                            Tr(
                                Td(t("acct.bank_name"), cls="detail-label"),
                                Td(Input(type="text", name="bank_name", value=b.get("bank_name", ""),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_number"), cls="detail-label"),
                                Td(Input(type="text", name="account_number", value=b.get("account_number", ""),
                                         cls="cell-input", required=True)),
                            ),
                            Tr(
                                Td(t("acct.account_type"), cls="detail-label"),
                                Td(Select(
                                    *[Option(display_enum(v, "bank_type"), value=v, selected=(v == b.get("bank_type")))
                                      for v in _BANK_TYPES],
                                    name="bank_type", cls="cell-input cell-input--select",
                                )),
                            ),
                            Tr(
                                Td(t("th.currency"), cls="detail-label"),
                                Td(searchable_select("currency", currency_options(b.get("currency", "")),
                                                     value=b.get("currency", ""), aria_label=t("th.currency"))),
                            ),
                            cls="detail-table",
                        ),
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                            A(t("btn.cancel"), href="/settings/accounting?tab=bank-accounts",
                              cls="btn btn--secondary ml-sm"),
                            cls="mt-md",
                        ),
                        Div(id="bank-form-error"),
                    ),
                    hx_patch=f"/settings/accounting/bank-accounts/{bank_id}",
                    hx_target="#bank-form-error",
                    hx_swap="innerHTML",
                ),
                cls="settings-card",
            ),
            title=page_title("settings_accounting.edit_bank_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.patch("/settings/accounting/bank-accounts/{bank_id}")
    async def patch_bank_account_route(request: Request, bank_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        patch = {}
        for field in ("bank_name", "account_number", "bank_type", "currency"):
            v = str(form.get(field, "")).strip()
            if v:
                patch[field] = v
        if not patch:
            return P(t("acct.nothing_to_update"), cls="error-banner")
        try:
            await api.patch_bank_account(token, bank_id, patch)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=bank-accounts"})

    @app.patch("/settings/accounting/bank-accounts/{bank_id}/toggle")
    async def toggle_bank_account(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return Div(P(t("error.session_expired")), id="bank-accounts-list")
        refused = []
        try:
            b = await api.get_bank_account(token, bank_id)
            await api.patch_bank_account(token, bank_id, {"is_active": not b.get("is_active", True)})
        except APIError as e:
            refused = [P(str(e.detail), cls="error-banner")]
        try:
            banks = (await api.get_bank_accounts(token, include_inactive=True)).get("items", [])
        except APIError:
            banks = []
        active = [b for b in banks if b.get("is_active")]
        rows = [_bank_account_row(b) for b in active]
        return Div(*refused, *rows, id="bank-accounts-list") if rows else Div(
            *refused,
            P(t("acct.no_bank_accounts_yet_add_one_to_start_tracking_cas"), cls="empty-state"),
            id="bank-accounts-list",
        )

    @app.post("/settings/accounting/period-lock")
    async def post_period_lock(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        unlock = form.get("unlock")
        lock_date = None if unlock else (form.get("lock_date") or "").strip() or None
        try:
            result = await api.set_period_lock(token, lock_date)
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="period-lock-content")
        return _period_lock_tab(result)

    @app.post("/settings/accounting/close-year")
    async def post_close_year(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        fiscal_year_end = (form.get("fiscal_year_end") or "").strip()
        if not fiscal_year_end:
            return Div(P(t("acct.fiscal_year_end_date_is_required"), cls="error-banner"), id="period-lock-content")
        try:
            result = await api.close_fiscal_year(token, fiscal_year_end)
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="period-lock-content")
        lock_data = {"lock_date": result.get("lock_date")}
        content = _period_lock_tab(lock_data)
        # Prepend success message
        net = result.get("net_income", 0)
        msg = t("settings_accounting.fiscal_year_closed", date=fiscal_year_end, net=f"{net:,.2f}")
        return Div(P(msg, cls="success-banner mb-md"), content, id="period-lock-content")

    @app.post("/settings/accounting/rules")
    async def create_rule(request: Request):
        token = _token(request)
        if not token:
            return Div(P(t("error.session_expired")), id="rules-list")
        form = await request.form()
        data = {
            "bank_account_id": str(form.get("bank_account_id", "")).strip(),
            "match_pattern": str(form.get("match_pattern", "")).strip(),
            "match_type": str(form.get("match_type", "contains")).strip(),
            "target_account_code": str(form.get("target_account_code", "")).strip(),
            "default_memo": str(form.get("default_memo", "")).strip() or None,
        }
        if not data["bank_account_id"] or not data["match_pattern"] or not data["target_account_code"]:
            return Div(P(t("acct.bank_account_pattern_and_account_code_are_required"), cls="error-banner"), id="rules-list")
        try:
            await api.create_recon_rule(token, data)
            rules_data = await api.get_recon_rules(token)
            rules = rules_data.get("items", [])
            banks_data = await api.get_bank_accounts(token)
            banks = banks_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="rules-list")
        return _rules_tab(rules, banks)

    @app.delete("/settings/accounting/rules/{rule_id}")
    async def delete_rule(request: Request, rule_id: str):
        token = _token(request)
        if not token:
            return Div(P(t("error.session_expired")), id="rules-list")
        try:
            await api.delete_recon_rule(token, rule_id)
            rules_data = await api.get_recon_rules(token)
            rules = rules_data.get("items", [])
            banks_data = await api.get_bank_accounts(token)
            banks = banks_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="rules-list")
        return _rules_tab(rules, banks)

    @app.post("/settings/accounting/chart/seed")
    async def seed_chart_route(request: Request):
        token = _token(request)
        if not token:
            return Div(P(t("error.session_expired")), id="chart-content")
        try:
            await api.seed_chart(token)
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="chart-content")
        return Div(_chart_table(chart), id="chart-content")

    @app.get("/settings/accounting/chart/new")
    async def new_account_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _account_error_page(request, str(e.detail))
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("acct.add_account"),
                A(t("btn.back"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
            ),
            Div(_account_form(chart), cls="settings-card"),
            title=page_title("acct.add_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.post("/settings/accounting/chart/new")
    async def create_account_submit(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        code = str(form.get("code", "")).strip()
        name = str(form.get("name", "")).strip()
        account_type = str(form.get("account_type", "")).strip()
        parent_code = str(form.get("parent_code", "")).strip()
        if not code:
            return P(t("settings_accounting.code_required"), cls="error-banner")
        if len(code) > _ACCOUNT_CODE_MAX:
            return P(t("settings_accounting.code_too_long", max=_ACCOUNT_CODE_MAX), cls="error-banner")
        err = await _validate_account(token, name, account_type, parent_code, own_code=code)
        if err:
            return P(err, cls="error-banner")
        try:
            await api.create_account(token, {
                "code": code,
                "name": name,
                "account_type": account_type,
                "parent_code": parent_code or None,
                "cash_flow_category": str(form.get("cash_flow_category", "")).strip(),
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=chart"})

    @app.get("/settings/accounting/chart/{code}/edit")
    async def edit_account_page(request: Request, code: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return await _account_error_page(request, str(e.detail))
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return await _account_error_page(request, t("settings_accounting.account_not_found"))
        return await base_shell(
            _section_breadcrumb(t("page.accounting")),
            page_header(
                t("settings_accounting.edit_named", name=acct.get("name") or code),
                A(t("btn.back"), href="/settings/accounting?tab=chart", cls="btn btn--secondary"),
            ),
            Div(_account_form(chart, values=acct), cls="settings-card"),
            title=page_title("settings_accounting.edit_account"),
            nav_active="settings-accounting",
            request=request,
        )

    @app.patch("/settings/accounting/chart/{code}")
    async def patch_account_submit(request: Request, code: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="error-banner")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        account_type = str(form.get("account_type", "")).strip()
        parent_code = str(form.get("parent_code", "")).strip()
        err = await _validate_account(token, name, account_type, parent_code, own_code=code)
        if err:
            return P(err, cls="error-banner")
        try:
            await api.patch_account(token, code, {
                "name": name,
                "account_type": account_type,
                "parent_code": parent_code or None,
                "is_active": str(form.get("is_active", "true")).strip() == "true",
                "cash_flow_category": str(form.get("cash_flow_category", "")).strip(),
            })
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/accounting?tab=chart"})

    @app.get("/settings/accounting/posting-accounts/{key}/edit")
    async def posting_account_edit(request: Request, key: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            data = await api.get_posting_accounts(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        row = next((r for r in data.get("roles", []) if r["role"] == key), None)
        if row is None:
            return P(t("posting.unknown_role"), cls="cell-error")
        return _posting_edit_cell(key, row["candidates"], row.get("code"), role_label(key, row["label"]))

    @app.get("/settings/accounting/posting-accounts/{key}/display")
    async def posting_account_display(request: Request, key: str):
        """The display cell again (the Escape cancel handler)."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            data = await api.get_posting_accounts(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        current = next((r for r in data.get("roles", []) if r["role"] == key), None)
        if current is None:
            return P(t("posting.unknown_role"), cls="cell-error")
        return _posting_display_cell(key, current)

    @app.patch("/settings/accounting/posting-accounts/{key}")
    async def posting_account_patch(request: Request, key: str):
        """Save the chosen account and return the updated row in place. A refusal
        (an account the role cannot use) is shown on the row with the reason."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        form = await request.form()
        code = str(form.get("value", "")).strip()
        try:
            data = await api.set_posting_account(token, key, code)
            error = None
        except APIError as e:
            error = str(e.detail)
            try:
                data = await api.get_posting_accounts(token)
            except APIError as e2:
                return P(str(e2.detail), cls="cell-error")
        return _posting_row(data, key, error) or P(t("posting.unknown_role"), cls="cell-error")

    @app.post("/settings/accounting/older-stock/move")
    async def older_stock_move(request: Request):
        """Move the ticked lots to the chosen inventory account, once per page render."""
        form = await request.form()

        async def call(token):
            view = await api.move_older_stock(token, list(dict.fromkeys(form.getlist("selected"))),
                                              str(form.get("code", "")).strip(), required_operation_key(form))
            done = view.pop("result")
            return {**view, "message": t("older_stock.moved", n=done["moved"], code=done["code"])}

        return await _older_stock_action(request, call)

    @app.post("/settings/accounting/older-stock/move-all")
    async def older_stock_move_all(request: Request):
        """Move every lot matching the filters the page shows (in the URL) as one move."""
        form = await request.form()
        filters = _older_stock_filters_of(request)

        async def call(token):
            view = await api.move_all_older_stock(token, {k: filters[k] for k in ("account", "q") if k in filters},
                                                  str(form.get("selection", "")).strip(),
                                                  str(form.get("code", "")).strip(), required_operation_key(form))
            done = view.pop("result")
            return {**view, "message": t("older_stock.moved", n=done["moved"], code=done["code"])}

        return await _older_stock_action(request, call)

    @app.post("/settings/accounting/older-stock/moves/{move_id}/undo")
    async def older_stock_undo(request: Request, move_id: str):
        async def call(token):
            return {**await api.undo_older_stock_move(token, move_id), "message": t("older_stock.undo_done")}

        return await _older_stock_action(request, call)

    @app.post("/settings/accounting/older-stock/retry")
    async def older_stock_retry(request: Request):
        async def call(token):
            view = await api.retry_older_stock(token)
            if view.pop("result")["placed"]:
                return {**view, "message": t("older_stock.placed_msg")}
            return {**view, "message": t("older_stock.still_failed"), "kind": "error"}

        return await _older_stock_action(request, call)

    @app.post("/settings/accounting/older-stock/corrections")
    async def older_stock_correct(request: Request):
        """Correct how the ticked placements (one, from its row, or many, from the bulk
        toolbar) treated their difference; ``kind`` rides in the URL."""
        form = await request.form()

        async def call(token):
            view = await api.correct_older_stock(token, list(dict.fromkeys(form.getlist("selected"))),
                                                 request.query_params.get("kind", "").strip())
            view.pop("result", None)
            return {**view, "message": t("older_stock.corrected_msg")}

        return await _older_stock_action(request, call)

    @app.get("/settings/accounting/chart/{code}/cash-flow/edit")
    async def cash_flow_field_edit(request: Request, code: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return P(t("settings_accounting.account_not_found"), cls="cell-error")
        return _cash_flow_edit_cell(acct)

    @app.get("/settings/accounting/chart/{code}/cash-flow/display")
    async def cash_flow_field_display(request: Request, code: str):
        """Return the read-only display cell (used by the Escape cancel handler)."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            chart_data = await api.get_chart(token)
            chart = chart_data.get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        acct = next((a for a in chart if a.get("code") == code), None)
        if acct is None:
            return P(t("settings_accounting.account_not_found"), cls="cell-error")
        return _cash_flow_display_cell(code, acct.get("cash_flow_category"))

    @app.patch("/settings/accounting/chart/{code}/cash-flow")
    async def cash_flow_field_patch(request: Request, code: str):
        """Save just the cash flow override: a partial update through the same
        API endpoint patch_account_submit forwards to. An empty value clears the
        override back to the derived default, and an invalid one comes back as a
        422 whose message is shown in place rather than swallowed."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", "")).strip()
        try:
            acct = await api.patch_account(token, code, {"cash_flow_category": value})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _cash_flow_display_cell(code, acct.get("cash_flow_category"))
