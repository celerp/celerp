# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed asset report pages: one page, one print view and one CSV per report, all
three drawn from the same table so the screen, the paper and the spreadsheet agree. The
period or filters a report is shown for travel in the URL."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable
from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.report_kit import action_bar, csv_amount, csv_response, fname_date, plain_error_response, print_shell
from ui.components.shell import page_header
from ui.components.table import EMPTY, breadcrumbs, empty_state_cta, fmt_money
from ui.config import get_token as _token
from ui.i18n import t

from celerp_fixed_assets.ui_routes import _context, _doc_href, _error_text, _input, _page, _restated_table, _text

_BASE = "/fixed-assets/reports"


def _amount(value, currency: str | None, **attrs) -> FT:
    """A money cell, right-aligned on screen and on paper alike."""
    shown = EMPTY if value in (None, "") else fmt_money(float(value), currency)
    return Td(shown, cls="cell--number", **attrs)


def _amount_th(label: str, **attrs) -> FT:
    return Th(label, cls="cell--number", **attrs)


def _table(head: list[FT], rows: list[FT], *extra_head: FT) -> FT:
    return Div(Table(Thead(*extra_head, Tr(*head)), Tbody(*rows), cls="data-table"), cls="table-scroll-wrap")


def _asset_link(row: dict, key: str = "id") -> FT:
    return A(row.get("number") or EMPTY, href=f"/fixed-assets/assets/{row[key]}")


# ── The schedule ─────────────────────────────────────────────────────────────

_SCHEDULE = (
    ("fixed_assets.cost", ("cost_opening", "additions", "improvements", "transfers", "revaluation", "disposals",
                           "cost_closing")),
    ("fixed_assets.accumulated_depreciation", ("depreciation_opening", "depreciation_charge",
                                               "depreciation_disposals", "depreciation_other",
                                               "depreciation_closing")),
    ("fixed_assets.accumulated_impairment", ("impairment_opening", "impairment_charge", "impairment_disposals",
                                             "impairment_closing")),
    ("fixed_assets.nbv", ("nbv_opening", "nbv_closing")),
    (None, ("planned_not_posted",)),
)
_SCHEDULE_FIELDS = [f for _, fields in _SCHEDULE for f in fields]


def _schedule_body(report: dict, currency: str) -> list[FT]:
    groups = Tr(Th(""), *[Th(t(label) if label else "", colspan=str(len(fields))) for label, fields in _SCHEDULE])
    head = [Th(t("fixed_assets.asset")), *[_amount_th(t(f"fixed_assets.col.{f}")) for f in _SCHEDULE_FIELDS]]
    rows: list[FT] = []
    for c in report["classes"]:
        rows.append(Tr(Td(Strong(_text(c["name"]))), *[_amount(c[f], currency) for f in _SCHEDULE_FIELDS]))
        rows += [Tr(Td(_asset_link(a), " ", _text(a.get("name"))), *[_amount(a[f], currency) for f in _SCHEDULE_FIELDS])
                 for a in c["assets"]]
    if not rows:
        rows = [Tr(Td(t("fixed_assets.schedule_empty")), *[Td(EMPTY, cls="cell--number") for _ in _SCHEDULE_FIELDS])]
    rows.append(Tr(Td(Strong(t("fixed_assets.total"))), *[_amount(report["totals"][f], currency)
                                                          for f in _SCHEDULE_FIELDS]))
    return [_table(head, rows, groups)]


def _schedule_rows(report: dict, currency: str) -> list[list]:
    out = [[t("fixed_assets.class"), t("fixed_assets.number"), t("fixed_assets.asset"),
            *[t(f"fixed_assets.col.{f}") for f in _SCHEDULE_FIELDS]]]
    for c in report["classes"]:
        out += [[c["name"] or "", a.get("number") or "", a.get("name") or "",
                 *[csv_amount(a[f], currency) for f in _SCHEDULE_FIELDS]] for a in c["assets"]]
        out.append([c["name"] or "", "", t("fixed_assets.total"), *[csv_amount(c[f], currency) for f in _SCHEDULE_FIELDS]])
    out.append([t("fixed_assets.total"), "", "", *[csv_amount(report["totals"][f], currency) for f in _SCHEDULE_FIELDS]])
    return out


# ── The register ─────────────────────────────────────────────────────────────

_REGISTER_MONEY = ("cost", "accumulated_depreciation", "accumulated_impairment", "nbv")


async def _register_fetch(token: str, params: dict) -> dict:
    """Every asset matching the filters: the register is read page by page."""
    classes = {c["id"]: c["name"] for c in (await api.fixed_assets_get(token, "/classes",
                                                                       {"include_archived": "true"}))["items"]}
    items: list[dict] = []
    while True:
        page = await api.fixed_assets_get(token, "/assets", {**params, "limit": 500, "offset": len(items)})
        items += page["items"]
        if not page["items"] or len(items) >= page["total"]:
            break
    return {"items": items, "classes": classes}


def _register_body(report: dict, currency: str) -> list[FT]:
    if not report["items"]:
        return [empty_state_cta(t("fixed_assets.no_match"))]
    head = [Th(t("fixed_assets.number")), Th(t("fixed_assets.name")), Th(t("fixed_assets.class")),
            Th(t("fixed_assets.status")), Th(t("fixed_assets.available_date")),
            *[_amount_th(t(f"fixed_assets.{k}")) for k in _REGISTER_MONEY]]
    rows = [Tr(Td(_asset_link(a)), Td(_text(a.get("name"))), Td(_text(report["classes"].get(a.get("class_id")))),
               Td(t(f"fixed_assets.status.{a.get('status')}")), Td(_text(a.get("available_date"))),
               *[_amount(a.get(k), currency) for k in _REGISTER_MONEY]) for a in report["items"]]
    return [_table(head, rows)]


def _register_rows(report: dict, currency: str) -> list[list]:
    out = [[t("fixed_assets.number"), t("fixed_assets.name"), t("fixed_assets.class"), t("fixed_assets.status"),
            t("fixed_assets.available_date"), *[t(f"fixed_assets.{k}") for k in _REGISTER_MONEY]]]
    out += [[a.get("number") or "", a.get("name") or "", report["classes"].get(a.get("class_id")) or "",
             t(f"fixed_assets.status.{a.get('status')}"), a.get("available_date") or "",
             *[csv_amount(a.get(k), currency) for k in _REGISTER_MONEY]] for a in report["items"]]
    return out


# ── Depreciation by period ───────────────────────────────────────────────────


def _depreciation_body(report: dict, currency: str) -> list[FT]:
    head = [Th(t("fixed_assets.month")), Th(t("fixed_assets.asset")), _amount_th(t("fixed_assets.posted")),
            _amount_th(t("fixed_assets.forecast")), Th(t("fixed_assets.skipped"))]
    rows: list[FT] = []
    for m in report["months"]:
        rows.append(Tr(Td(Strong(m["month"])), Td(""), _amount(m["posted"], currency), _amount(m["forecast"], currency),
                       Td("")))
        rows += [Tr(Td(""), Td(_asset_link(a), " ", _text(a.get("name"))), _amount(a["posted"], currency),
                    _amount(a["forecast"], currency), Td("")) for a in m["assets"]]
        rows += [Tr(Td(""), Td(_asset_link(s), " ", _text(s.get("name"))), Td(EMPTY, cls="cell--number"),
                    Td(EMPTY, cls="cell--number"), Td(_text(s.get("reason")))) for s in m["skipped"]]
    if not any(m["assets"] or m["skipped"] for m in report["months"]):
        return [empty_state_cta(t("fixed_assets.depreciation_empty"))]
    rows.append(Tr(Td(Strong(t("fixed_assets.total"))), Td(""), _amount(report["totals"]["posted"], currency),
                   _amount(report["totals"]["forecast"], currency), Td("")))
    return [_table(head, rows)]


def _depreciation_rows(report: dict, currency: str) -> list[list]:
    out = [[t("fixed_assets.month"), t("fixed_assets.number"), t("fixed_assets.asset"), t("fixed_assets.posted"),
            t("fixed_assets.forecast"), t("fixed_assets.skipped")]]
    for m in report["months"]:
        out += [[m["month"], a.get("number") or "", a.get("name") or "", csv_amount(a["posted"], currency),
                 csv_amount(a["forecast"], currency), ""] for a in m["assets"]]
        out += [[m["month"], s.get("number") or "", s.get("name") or "", "", "", s.get("reason") or ""]
                for s in m["skipped"]]
    return out


# ── Disposals ────────────────────────────────────────────────────────────────

_DISPOSAL_MONEY = ("proceeds", "credited", "carrying_amount", "gain_or_loss")


def _disposals_body(report: dict, currency: str) -> list[FT]:
    if not report["items"]:
        return [empty_state_cta(t("fixed_assets.disposals_empty"))]
    head = [Th(t("fixed_assets.date")), Th(t("fixed_assets.asset")), Th(t("fixed_assets.reason")),
            *[_amount_th(t(f"fixed_assets.col.{k}")) for k in _DISPOSAL_MONEY], Th(t("fixed_assets.invoice")),
            Th(t("fixed_assets.credit_notes")), Th(t("fixed_assets.journal"))]
    rows = [Tr(Td(r["date"]), Td(A(r.get("number") or EMPTY, href=f"/fixed-assets/assets/{r['asset_id']}"), " ",
                                 _text(r.get("name"))),
               Td(t(f"fixed_assets.disposal_reason.{r['reason']}") if r.get("reason") else EMPTY),
               *[_amount(r[k], currency) for k in _DISPOSAL_MONEY],
               Td(A(r["invoice_number"], href=_doc_href(r["invoice_id"])) if r.get("invoice_id") else EMPTY),
               Td(*[A(c["number"], href=_doc_href(c["id"]), cls="mr-sm") for c in r["credit_notes"]] or [EMPTY]),
               Td(A(t("fixed_assets.open"), href=f"/accounting?{urlencode({'q': r['journal_id']})}")
                  if r.get("journal_id") else EMPTY))
            for r in report["items"]]
    rows.append(Tr(Td(Strong(t("fixed_assets.total"))), Td(""), Td(""),
                   *[_amount(report["totals"][k], currency) for k in _DISPOSAL_MONEY], Td(""), Td(""), Td("")))
    return [_table(head, rows)]


def _disposals_rows(report: dict, currency: str) -> list[list]:
    out = [[t("fixed_assets.date"), t("fixed_assets.number"), t("fixed_assets.asset"), t("fixed_assets.reason"),
            *[t(f"fixed_assets.col.{k}") for k in _DISPOSAL_MONEY], t("fixed_assets.invoice"),
            t("fixed_assets.credit_notes"), t("fixed_assets.journal")]]
    out += [[r["date"], r.get("number") or "", r.get("name") or "",
             t(f"fixed_assets.disposal_reason.{r['reason']}") if r.get("reason") else "",
             *[csv_amount(r[k], currency) for k in _DISPOSAL_MONEY], r.get("invoice_number") or "",
             "; ".join(c["number"] for c in r["credit_notes"]), r.get("journal_id") or ""] for r in report["items"]]
    return out


# ── Prior-period adjustments ─────────────────────────────────────────────────


def _ppa_body(report: dict, currency: str) -> list[FT]:
    """The year as reported and restated, the corrections restating it, and the ones
    posted to current profit or loss instead."""
    parts: list[FT] = []
    if report["years"]:
        parts += [H2(t("fixed_assets.restated_title"), cls="section-title"), _restated_table(report["years"], currency)]
    for key, items, last in (("fixed_assets.ppa_corrections", report["items"], "fixed_assets.correction"),
                             ("fixed_assets.ppa_immaterial", report["immaterial"], "fixed_assets.reason")):
        if not items:
            continue
        parts.append(H2(t(key), cls="section-title"))
        if key == "fixed_assets.ppa_immaterial":
            parts.append(P(t("fixed_assets.ppa_immaterial_hint"), cls="settings-hint"))
        parts.append(_table([Th(t("fixed_assets.posting_date")), Th(t("fixed_assets.asset")),
                             Th(t("fixed_assets.years")), Th(t(last))],
                            [Tr(Td(_text(i.get("posting_date"))), Td(_asset_link(i, "asset_id")),
                                Td(", ".join(str(y) for y in i.get("years") or []) or EMPTY),
                                Td(_text(i.get("reason")) if key == "fixed_assets.ppa_immaterial"
                                   else A(t("fixed_assets.open"), href=f"/fixed-assets/corrections/{i['id']}")))
                             for i in reversed(items)]))
    return parts or [empty_state_cta(t("fixed_assets.ppa_empty", year=report["year"]))]


def _ppa_rows(report: dict, currency: str) -> list[list]:
    out = [[t("fixed_assets.section"), t("fixed_assets.year"), t("fixed_assets.asset"), t("fixed_assets.posting_date"),
            t("fixed_assets.reported"), t("fixed_assets.restated"), t("fixed_assets.reason")]]
    out += [[t("fixed_assets.restated_title"), y["year"], "", "", csv_amount(y["reported"], currency),
             csv_amount(y["restated"], currency), ""] for y in report["years"]]
    for key, items in (("fixed_assets.ppa_corrections", report["items"]),
                       ("fixed_assets.ppa_immaterial", report["immaterial"])):
        out += [[t(key), "; ".join(str(y) for y in i.get("years") or []), i.get("number") or "",
                 i.get("posting_date") or "", "", "", i.get("reason") or ""] for i in reversed(items)]
    return out


# ── The reports and their routes ─────────────────────────────────────────────


@dataclass(frozen=True)
class Report:
    key: str
    title_key: str
    hint_key: str
    # "range" (date_from, date_to), "year", or "register" (class and search filters).
    period: str
    fetch: Callable
    body: Callable[[dict, str], list[FT]]
    rows: Callable[[dict, str], list[list]]
    # Too many columns for portrait paper: printed landscape, in a smaller type.
    wide: bool = False


_WIDE_PRINT_CSS = "@page { size: A4 landscape; } body { padding: 10mm; } table { font-size: 7pt; }"


def _api(path: str) -> Callable:
    async def fetch(token: str, params: dict) -> dict:
        return await api.fixed_assets_get(token, path, params)
    return fetch


REPORTS = (
    Report("schedule", "fixed_assets.schedule_title", "fixed_assets.schedule_hint", "range",
           _api("/reports/schedule"), _schedule_body, _schedule_rows, wide=True),
    Report("register", "fixed_assets.register_report_title", "fixed_assets.register_report_hint", "register",
           _register_fetch, _register_body, _register_rows),
    Report("depreciation", "fixed_assets.depreciation_title", "fixed_assets.depreciation_hint", "range",
           _api("/reports/depreciation"), _depreciation_body, _depreciation_rows),
    Report("disposals", "fixed_assets.disposals_title", "fixed_assets.disposals_hint", "range",
           _api("/reports/disposals"), _disposals_body, _disposals_rows),
    Report("prior-period-adjustments", "fixed_assets.ppa_title", "fixed_assets.ppa_hint", "year",
           _api("/reports/prior-period-adjustments"), _ppa_body, _ppa_rows),
)


def _params(report: Report, request: Request, setup: dict) -> dict:
    """The period or filters asked for, defaulting to this year (from the first managed
    period when that is later) or, for the prior-period adjustments, last year."""
    q = request.query_params
    if report.period == "year":
        return {"year": q.get("year") or str(date.today().year - 1)}
    if report.period == "register":
        return {k: q[k] for k in ("class_id", "q") if q.get(k)}
    today = date.today()
    first = (setup.get("settings") or {}).get("first_managed_period") or ""
    start = max(today.replace(month=1, day=1).isoformat(), first)
    return {"date_from": q.get("date_from") or start, "date_to": q.get("date_to") or max(today.isoformat(), start)}


def _chooser(report: Report, params: dict, classes: list[dict]) -> FT:
    if report.period == "year":
        fields = [_input("year", params["year"], type_="number", step="1", min="1900",
                         aria_label=t("fixed_assets.year"))]
    elif report.period == "register":
        fields = [_input("q", params.get("q"), placeholder=t("fixed_assets.search"), aria_label=t("fixed_assets.search")),
                  Select(Option(t("fixed_assets.all_classes"), value=""),
                         *[Option(c["name"], value=c["id"], selected=c["id"] == params.get("class_id"))
                           for c in classes], name="class_id", cls="form-input", aria_label=t("fixed_assets.class"))]
    else:
        fields = [_input("date_from", params["date_from"], type_="date", aria_label=t("fixed_assets.date_from")),
                  _input("date_to", params["date_to"], type_="date", aria_label=t("fixed_assets.date_to"))]
    return Form(*fields, Button(t("fixed_assets.show"), type="submit", cls="btn btn--secondary"),
                method="get", action=f"{_BASE}/{report.key}", cls="flex-row")


def _subtitle(params: dict) -> str:
    if "year" in params:
        return params["year"]
    if "date_from" in params:
        return t("fixed_assets.period_range", date_from=params["date_from"], date_to=params["date_to"])
    return ""


def _filename(report: Report, params: dict) -> str:
    suffix = fname_date(params.get("date_to") or params.get("year") or date.today().isoformat())
    return f"fixed-asset-{report.key}-{suffix}.csv"


def _crumbs(report: Report | None):
    trail = [(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.reports_title"), _BASE if report else None)]
    return breadcrumbs(trail + ([(t(report.title_key), None)] if report else []))


def _register_routes(app, report: Report) -> None:
    path = f"{_BASE}/{report.key}"

    @app.get(path, name=f"fa_report_{report.key}")
    async def page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            setup, currency = await _context(token)
            classes = ((await api.fixed_assets_get(token, "/classes"))["items"]
                       if report.period == "register" else [])
        except APIError as e:
            return await _page(request, page_header(t(report.title_key)), Div(_error_text(e), cls="flash flash--error"),
                               title_key=report.title_key)
        params = _params(report, request, setup)
        try:
            body = Div(*report.body(await report.fetch(token, params), currency), cls="detail-card section-card")
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            body = Div(_error_text(e), cls="flash flash--error")
        return await _page(request, _crumbs(report),
                           page_header(t(report.title_key), action_bar(f"{path}/print", f"{path}.csv", params)),
                           P(t(report.hint_key), cls="settings-hint"), _chooser(report, params, classes),
                           *([H2(_subtitle(params), cls="section-title")] if _subtitle(params) else []), body,
                           title_key=report.title_key)

    @app.get(f"{path}/print", name=f"fa_report_{report.key}_print")
    async def printed(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            setup, currency = await _context(token)
            params = _params(report, request, setup)
            data = await report.fetch(token, params)
            company = await api.get_company(token)
        except APIError as e:
            return plain_error_response(e)
        wide = Style(_WIDE_PRINT_CSS) if report.wide else None
        return print_shell(company, t(report.title_key), _subtitle(params), Div(wide, *report.body(data, currency)))

    @app.get(f"{path}.csv", name=f"fa_report_{report.key}_csv")
    async def exported(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            setup, currency = await _context(token)
            params = _params(report, request, setup)
            data = await report.fetch(token, params)
        except APIError as e:
            return plain_error_response(e)
        return csv_response(report.rows(data, currency), _filename(report, params))


def setup_report_routes(app) -> None:
    """The reports index and every report's page, print view and CSV."""

    @app.get(_BASE)
    async def index(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        items = [Div(A(t(r.title_key), href=f"{_BASE}/{r.key}", cls="section-title"),
                     P(t(r.hint_key), cls="settings-hint"), cls="detail-card section-card") for r in REPORTS]
        return await _page(request, _crumbs(None), page_header(t("fixed_assets.reports_title")),
                           P(t("fixed_assets.reports_hint"), cls="settings-hint"), *items,
                           title_key="fixed_assets.reports_title")

    for report in REPORTS:
        _register_routes(app, report)
