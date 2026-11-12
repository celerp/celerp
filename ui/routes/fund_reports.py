# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fund reports: the fund balance sheet, fund activity and the fund reconciliation.

Each sits beside the other financial reports under /reports, with the same print view and
CSV export, and is listed there when the books use funds. Fund activity is also where a
movement between funds is undone: Reverse on a movement that still stands posts its
reversal (movements are never voided).
"""
from __future__ import annotations

from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from celerp.services.business_time import business_today
from celerp.services.peer_lines import UNRESTRICTED
from ui.api_client import APIError, error_message
from ui.components.encode import safe_url
from ui.components.funds import fund_waiting_banner
from ui.components.report_kit import (
    csv_amount, csv_response, date_params, fname_date, period_subtitle, plain_error_response, print_shell,
)
from ui.components.shell import page_header, page_title
from ui.components.table import fmt_money
from ui.config import get_role, get_token as _token
from ui.i18n import get_lang, t
from ui.routes.financial_reports import (
    REPORTS, _bars, _drill_ledger_href, _error_content, _page, _report_path, report_nav,
)
from ui.routes.reports import _date_filter_bar, _get_company, _parse_dates
from ui.routes.settings_funds import movement_title

_NUM = "cell--number"
_ACTIVITY_ID = "fund-activity"
_COLUMNS = ("opening", "received", "spent", "released", "transferred", "closing")
_MOVEMENT_KINDS = {"fund_transfer": "transfer", "fund_release": "release",
                   "fund_reallocation": "reallocate", "fund_opening": "opening"}


def _fund_names(funds: list[dict]) -> dict[str, str]:
    """Fund id -> name, the lines with no fund under Unrestricted."""
    return {UNRESTRICTED: t("label.unrestricted"), **{f["id"]: f["name"] for f in funds}}


def _named(names: dict[str, str], fund_id) -> str:
    return names.get(fund_id or UNRESTRICTED, fund_id)


def _table(head: list, rows: list) -> FT:
    return Table(Thead(Tr(*head)), Tbody(*rows), cls="data-table data-table--compact")


def _section(title: str, body) -> FT:
    return Div(H3(title, cls="report-section-title"), body, cls="report-section")


def _money_cells(values, currency) -> list:
    return [Td(fmt_money(v or 0, currency), cls=_NUM) for v in values]


def _movement_label(movement: dict) -> str:
    kind = _MOVEMENT_KINDS.get(movement.get("je_type"))
    return movement_title(kind) if kind else movement.get("je_type") or ""


# --- the fund balance sheet -------------------------------------------------------------

def balance_sheet_view(data: dict, currency: str | None) -> FT:
    """Each asset and liability account by fund, each fund's net assets under them, and the
    restricted net assets apart from what is available."""
    funds = data.get("funds") or []
    as_of = data.get("as_of") or ""
    rows = [Tr(Td(A(f"{row['code']} {row.get('name', '')}".strip(),
                    href=safe_url(_drill_ledger_href(row["code"], "fund-balance-sheet", "", as_of)), cls="drilldown-link")),
               *_money_cells([row["amounts"].get(f["id"]) for f in funds], currency),
               Td(fmt_money(row.get("total", 0), currency), cls=_NUM))
            for row in data.get("accounts") or ()]
    if not rows:
        return Div(P(t("acct.no_entries"), cls="empty-state"), cls="report-view")
    net = [f.get("net_assets", 0) for f in funds]
    rows.append(Tr(Td(Strong(t("fund_reports.net_assets"))),
                   *[Td(Strong(fmt_money(v, currency)), cls=_NUM) for v in [*net, sum(net)]],
                   cls="row--total"))
    return Div(
        _table([Th(t("th.account")), *[Th(f["name"], cls=_NUM) for f in funds], Th(t("th.total"), cls=_NUM)], rows),
        Div(Span(f"{t('fund_reports.restricted_net_assets')}: "
                 f"{fmt_money(data.get('restricted_net_assets', 0), currency)}", cls="val-chip"),
            Span(f"{t('fund_reports.available_net_assets')}: "
                 f"{fmt_money(data.get('available_net_assets', 0), currency)}", cls="val-chip"),
            cls="valuation-bar"),
        cls="report-view",
    )


def balance_sheet_csv(data: dict, currency: str | None) -> list[list]:
    funds = data.get("funds") or []
    rows: list[list] = [["Code", "Account", *[f["name"] for f in funds], "Total"]]
    rows += [[row["code"], row.get("name", ""), *[csv_amount(row["amounts"].get(f["id"], 0), currency) for f in funds],
              csv_amount(row.get("total", 0), currency)] for row in data.get("accounts") or ()]
    net = [f.get("net_assets", 0) for f in funds]
    rows += [["", "Net assets", *[csv_amount(v, currency) for v in [*net, sum(net)]]], [],
             ["", "Restricted net assets", csv_amount(data.get("restricted_net_assets", 0), currency)],
             ["", "Available net assets", csv_amount(data.get("available_net_assets", 0), currency)]]
    return rows


# --- fund activity ----------------------------------------------------------------------

def _reverse_form(movement: dict, period: tuple[str, str], today: str, kept: dict | None) -> FT:
    """Reverse on a movement that still stands: the date and reason of its reversal."""
    values = kept or {}
    d_from, d_to = period
    return Details(
        Summary(t("fund_reports.reverse")),
        Form(
            Input(type="hidden", name="from", value=d_from),
            Input(type="hidden", name="to", value=d_to),
            Label(t("th.date"), Input(type="date", name="date", value=values.get("date") or today, required=True,
                                      cls="form-input")),
            Label(t("label.reason"), Input(type="text", name="reason", value=values.get("reason", ""), required=True,
                                           cls="form-input")),
            Button(t("fund_reports.reverse"), type="submit", cls="btn btn--secondary btn--sm"),
            hx_post=f"/reports/funds/movements/{quote(movement['entity_id'], safe='')}/reverse",
            hx_target=f"#{_ACTIVITY_ID}", hx_swap="outerHTML", cls="flex-col gap-sm",
        ),
        open=kept is not None,
    )


def _movement_state(movement: dict, actions: dict | None) -> FT | str:
    """What became of a movement: its reversal, what it reverses, or Reverse while it stands
    (``actions`` is None on paper, where nothing can be done)."""
    if movement.get("reversed_by"):
        return t("fund_reports.reversed_by", entry=movement["reversed_by"])
    if movement.get("reverses"):
        return t("fund_reports.reverses", entry=movement["reverses"])
    if actions is None:
        return ""
    kept = actions["kept"]
    return _reverse_form(movement, actions["period"], actions["today"],
                         kept[1] if kept and kept[0] == movement["entity_id"] else None)


def _fund_changes(changes: list[dict]) -> FT:
    rows = [Tr(Td(c.get("date") or ""), Td(c.get("record_id") or ""), Td(c.get("reason") or ""),
               Td(", ".join(c.get("posted") or ())))
            for c in sorted(changes, key=lambda c: c.get("date") or "", reverse=True)]
    return _section(t("fund_reports.fund_changes"), _table(
        [Th(t("th.date")), Th(t("fund_reports.record")), Th(t("label.reason")), Th(t("fund_reports.posted_as"))], rows))


def activity_view(data: dict, currency: str | None, actions: dict | None = None,
                  banner: FT | str = "") -> FT:
    """Each fund's figures over the period, the period's movements newest first with what
    became of each, and the period's fund changes. ``actions`` (period, today, kept) offers
    Reverse; None renders for print."""
    funds = data.get("funds") or []
    names = _fund_names(funds)
    summary = _table(
        [Th(t("th.fund")), *[Th(t(f"fund_reports.{c}"), cls=_NUM) for c in _COLUMNS]],
        [Tr(Td(f["name"]), *_money_cells([f.get(c) for c in _COLUMNS], currency)) for f in funds])
    movements = data.get("movements") or []
    moved = _table(
        [Th(t("th.date")), Th(t("fund_reports.movement")), Th(t("label.reason")), Th(t("th.fund")),
         Th(t("fund_reports.amount"), cls=_NUM), Th(t("th.status"))],
        [Tr(Td(m.get("date") or ""), Td(_movement_label(m)), Td(m.get("reason") or ""),
            Td(*[Div(_named(names, f)) for f in m.get("amounts") or {}]),
            Td(*[Div(fmt_money(v, currency)) for v in (m.get("amounts") or {}).values()], cls=_NUM),
            Td(_movement_state(m, actions)))
         for m in movements]) if movements else P(t("fund_reports.no_movements"), cls="empty-state")
    changes = data.get("fund_changes") or []
    return Div(banner, summary, _section(t("fund_reports.movements"), moved),
               _fund_changes(changes) if changes else "", id=_ACTIVITY_ID, cls="report-view")


def activity_csv(data: dict, currency: str | None) -> list[list]:
    funds = data.get("funds") or []
    names = _fund_names(funds)
    rows: list[list] = [["Fund", "Opening", "Received", "Spent", "Released", "Transferred", "Closing"]]
    rows += [[f["name"], *[csv_amount(f.get(c, 0), currency) for c in _COLUMNS]] for f in funds]
    rows += [[], ["Date", "Movement", "Entry", "Reason", "Fund", "Amount", "Reverses", "Reversed by"]]
    for m in data.get("movements") or ():
        for fund, amount in (m.get("amounts") or {"": None}).items():
            rows.append([m.get("date"), _movement_label(m), m["entity_id"], m.get("reason") or "",
                         _named(names, fund) if fund else "", csv_amount(amount, currency) if fund else "",
                         m.get("reverses") or "", m.get("reversed_by") or ""])
    rows += [[], ["Date", "Fund change", "Reason", "Posted again as"]]
    rows += [[c.get("date"), c.get("record_id"), c.get("reason") or "", " ".join(c.get("posted") or ())]
             for c in data.get("fund_changes") or ()]
    return rows


# --- the fund reconciliation ------------------------------------------------------------

def reconciliation_view(data: dict, currency: str | None, funds: list[dict]) -> FT:
    """Whether every account's funds add up to the company ledger, and each list of what
    needs a look: differences, overdrawn restricted funds, funds with negative net assets,
    entries mirroring each other across funds, entries on another fund than their document's
    lines, and the fund changes so far."""
    names = _fund_names(funds)
    parts: list = [P(t("fund_reports.balanced"), cls="success-banner") if data.get("balanced") else ""]
    if data.get("differences"):
        parts.append(_section(t("fund_reports.differences"), _table(
            [Th(t("th.date")), Th(t("th.account")), Th(t("fund_reports.company_total"), cls=_NUM),
             Th(t("fund_reports.funds_total"), cls=_NUM)],
            [Tr(Td(d["date"]), Td(d["account"]), *_money_cells([d["company_total"], d["funds_total"]], currency))
             for d in sorted(data["differences"], key=lambda d: d["date"], reverse=True)])))
    if data.get("overdrawn"):
        parts.append(_section(t("fund_reports.overdrawn"), _table(
            [Th(t("th.fund")), Th(t("th.account")), Th(t("th.balance"), cls=_NUM)],
            [Tr(Td(o["fund"]), Td(o["account"]), *_money_cells([o["balance"]], currency)) for o in data["overdrawn"]])))
    if data.get("negative_net_assets"):
        parts.append(_section(t("fund_reports.negative"), _table(
            [Th(t("th.fund")), Th(t("fund_reports.net_assets"), cls=_NUM)],
            [Tr(Td(n["fund"]), *_money_cells([n["net_assets"]], currency)) for n in data["negative_net_assets"]])))
    if data.get("mirrors"):
        parts.append(_section(t("fund_reports.mirrors"), _table(
            [Th(t("fund_reports.entries"))], [Tr(Td(", ".join(m["entries"]))) for m in data["mirrors"]])))
    if data.get("attribution"):
        parts.append(_section(t("fund_reports.attribution"), _table(
            [Th(t("fund_reports.entry")), Th(t("th.document")), Th(t("th.line")), Th(t("fund_reports.posted_on")),
             Th(t("fund_reports.line_fund"))],
            [Tr(Td(a["entity_id"]), Td(a.get("record_id") or ""), Td(str(a.get("line") or "")),
                Td(_named(names, a.get("posted_fund"))), Td(_named(names, a.get("line_fund"))))
             for a in data["attribution"]])))
    if data.get("fund_changes"):
        parts.append(_fund_changes(data["fund_changes"]))
    return Div(*parts, cls="report-view")


def reconciliation_csv(data: dict, currency: str | None, funds: list[dict]) -> list[list]:
    names = _fund_names(funds)
    rows: list[list] = [["Balanced", "yes" if data.get("balanced") else "no"], [],
                        ["Difference", "Date", "Account", "Company ledger", "Funds"]]
    rows += [["", d["date"], d["account"], csv_amount(d["company_total"], currency),
              csv_amount(d["funds_total"], currency)] for d in data.get("differences") or ()]
    rows += [[], ["Overdrawn", "Fund", "Account", "Balance"]]
    rows += [["", o["fund"], o["account"], csv_amount(o["balance"], currency)] for o in data.get("overdrawn") or ()]
    rows += [[], ["Negative net assets", "Fund", "Net assets"]]
    rows += [["", n["fund"], csv_amount(n["net_assets"], currency)] for n in data.get("negative_net_assets") or ()]
    rows += [[], ["Mirror pair", "Entries"]]
    rows += [["", " ".join(m["entries"])] for m in data.get("mirrors") or ()]
    rows += [[], ["Attribution", "Entry", "Document", "Line", "Posted on", "Line's fund"]]
    rows += [["", a["entity_id"], a.get("record_id") or "", a.get("line") or "", _named(names, a.get("posted_fund")),
              _named(names, a.get("line_fund"))] for a in data.get("attribution") or ()]
    return rows


# --- routes -----------------------------------------------------------------------------

def setup_routes(app):

    def _as_of(request: Request, company: dict) -> str:
        return request.query_params.get("as_of", "") or business_today(company.get("timezone")).isoformat()

    def _as_of_form(key: str, as_of: str) -> FT:
        return Div(Form(
            Label(t("label.as_of_date"), cls="form-label"),
            Input(type="date", name="as_of", value=as_of, cls="date-input"),
            Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
            action=safe_url(_report_path(key)), method="get", cls="date-custom-form",
        ), cls="date-filter-bar")

    async def _shown(request: Request, key: str, build) -> FT:
        """The report page: the waiting count, the report links, its filter, Print and CSV,
        and the report ``build(token, company)`` answers as (filter, params, view)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await _get_company(token)
            settings = company.get("settings") or {}
            bar, params, view, d_from, d_to = await build(request, token, company)
            content = Div(
                await fund_waiting_banner(token, settings, get_role(request)),
                report_nav(key, d_from, d_to, company), bar,
                Div(_bars(key, params), cls="flex-row mt-md mb-md"), view,
            )
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            content = _error_content(e)
        title = REPORTS[key][3]
        return await _page(request, page_header(t(title, get_lang(request))), content, title=page_title(title))

    async def _balance_sheet(request, token, company):
        as_of = _as_of(request, company)
        data = await api.get_fund_balance_sheet(token, {"as_of": as_of})
        return (_as_of_form("fund-balance-sheet", as_of), {"as_of": as_of},
                balance_sheet_view(data, company.get("currency")), "", as_of)

    def _activity_actions(company: dict, d_from: str, d_to: str, kept=None) -> dict:
        return {"period": (d_from, d_to), "today": business_today(company.get("timezone")).isoformat(), "kept": kept}

    async def _activity(request, token, company):
        d_from, d_to, preset = _parse_dates(request, company)
        params = date_params(d_from, d_to)
        data = await api.get_fund_activity(token, params)
        bar = _date_filter_bar(_report_path("fund-activity"), d_from, d_to, preset,
                               settings_link="/settings/general?tab=company")
        view = activity_view(data, company.get("currency"), _activity_actions(company, d_from, d_to))
        return bar, params, view, d_from or "", d_to or ""

    async def _reconciliation(request, token, company):
        as_of = _as_of(request, company)
        data = await api.get_fund_reconciliation(token, {"as_of": as_of})
        funds = (await api.list_funds(token)).get("items") or []
        return (_as_of_form("fund-reconciliation", as_of), {"as_of": as_of},
                reconciliation_view(data, company.get("currency"), funds), "", as_of)

    def _report_routes(key: str, build) -> None:
        page_path, print_path, csv_path, _title = REPORTS[key]

        async def report_page(request: Request):
            return await _shown(request, key, build)

        async def report_print(request: Request):
            return await _printed_or_exported(request, key, csv=False)

        async def report_csv(request: Request):
            return await _printed_or_exported(request, key, csv=True)

        app.get(page_path)(report_page)
        app.get(print_path)(report_print)
        app.get(csv_path)(report_csv)

    async def _printed_or_exported(request: Request, key: str, csv: bool):
        """The print view or CSV of a fund report, for the period its query names."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        q = request.query_params
        try:
            company = await _get_company(token)
            currency = company.get("currency")
            if key == "fund-activity":
                d_from, d_to = q.get("date_from", ""), q.get("date_to", "")
                data = await api.get_fund_activity(token, date_params(d_from, d_to))
                subtitle, name = period_subtitle(d_from, d_to), f"{fname_date(d_from)}_{fname_date(d_to)}"
                rows, view = (activity_csv, lambda: activity_view(data, currency))
            else:
                as_of = q.get("as_of", "")
                subtitle, name = (f"{t('label.as_of_date')} {as_of}" if as_of else ""), fname_date(as_of)
                params = {"as_of": as_of} if as_of else {}
                if key == "fund-balance-sheet":
                    data = await api.get_fund_balance_sheet(token, params)
                    rows, view = (balance_sheet_csv, lambda: balance_sheet_view(data, currency))
                else:
                    data = await api.get_fund_reconciliation(token, params)
                    funds = (await api.list_funds(token)).get("items") or []
                    rows, view = (lambda d, c: reconciliation_csv(d, c, funds),
                                  lambda: reconciliation_view(data, currency, funds))
        except APIError as e:
            return plain_error_response(e)
        if csv:
            return csv_response(rows(data, currency), f"{key.replace('-', '_')}_{name}.csv")
        return print_shell(company, t(REPORTS[key][3]), subtitle, view())

    for key, build in (("fund-balance-sheet", _balance_sheet), ("fund-activity", _activity),
                       ("fund-reconciliation", _reconciliation)):
        _report_routes(key, build)

    @app.post("/reports/funds/movements/{je_id}/reverse")
    async def reverse_movement(request: Request, je_id: str):
        """Reverse a movement between funds and redraw fund activity for the same period,
        headed by what it did or why it was refused, a refusal keeping what was typed."""
        token = _token(request)
        form = await request.form()
        typed = {k: str(form.get(k) or "").strip() for k in ("date", "reason")}
        d_from, d_to = str(form.get("from") or ""), str(form.get("to") or "")
        kept = None
        try:
            await api.reverse_fund_movement(token, je_id, typed)
            banner = P(t("fund_reports.reversed"), cls="success-banner mb-md")
        except APIError as e:
            api.raise_if_signed_out(e)
            banner, kept = P(error_message(e), cls="error-banner mb-md"), (je_id, typed)
        try:
            company = await _get_company(token)
            data = await api.get_fund_activity(token, date_params(d_from, d_to))
        except APIError as e:
            api.raise_if_signed_out(e)
            return Div(banner, P(error_message(e), cls="error-banner"), id=_ACTIVITY_ID, cls="report-view")
        return activity_view(data, company.get("currency"),
                             _activity_actions(company, d_from, d_to, kept), banner)
