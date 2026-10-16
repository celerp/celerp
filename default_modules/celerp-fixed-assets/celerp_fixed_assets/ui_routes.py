# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixed assets pages.

GET  /fixed-assets                      The register: start panel, or the asset list
GET  /fixed-assets/intake               Bill and journal lines not yet registered
GET  /fixed-assets/intake/line          What to do with one line
GET  /fixed-assets/assets/new           Add an asset by hand
GET  /fixed-assets/assets/{id}          One asset: details, figures, what holds it, sources, movements
GET  /fixed-assets/runs                 Depreciation runs: run a month, every month's run newest first
GET  /fixed-assets/runs/{month}         One month's run: its journal and the assets held out of it
GET  /settings/fixed-assets             Settings and asset classes

Every financial action is one form with two steps: Review shows the exact journal lines
and the assets it creates, Confirm does what was reviewed (POST /fixed-assets/op/...).
"""
from __future__ import annotations

import logging
from datetime import date
from urllib.parse import quote, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.operation_key import FIELD as OP_KEY_FIELD, operation_key_input
from ui.components.shell import base_shell, page_header, page_title, toast_header
from ui.components.table import (EMPTY, breadcrumbs, display_cell, editable_cell, empty_mark, empty_state_cta,
                                 fmt_money, pagination, searchable_select, _SEARCHABLE_THRESHOLD)
from ui.config import get_token as _token
from ui.i18n import refusal_text, t

log = logging.getLogger(__name__)

PER_PAGE = 50
MANUAL_PREFIX = "je:manual:"
DESCRIPTIVE_FIELDS = ("name", "description", "serial", "make", "model", "notes")


# ── Shared pieces ────────────────────────────────────────────────────────────


def _error_text(e: APIError) -> str:
    return refusal_text((e.data or {}).get("detail")) or str(e.detail) or t("fixed_assets.error")


def _money(value, currency: str | None) -> FT | str:
    return empty_mark() if value in (None, "") else fmt_money(float(value), currency)


def _text(value) -> str:
    return EMPTY if value in (None, "") else str(value)


def _money_td(value, currency):
    return Td(_money(value, currency), cls="cell--money")


def _money_th(label: str):
    return Th(label, cls="cell--number")


def _postable(chart: list[dict]) -> list[dict]:
    """Accounts a journal line can post to: active, with no accounts under them."""
    parents = {a.get("parent_code") for a in chart}
    return [a for a in chart if a.get("is_active", True) and a["code"] not in parents]


def _picker(name: str, options: list[tuple[str, str]], value: str = "", *, label: str,
            empty: str = "fixed_assets.choose") -> FT:
    """A select, searchable once it could hold more than ten options (rule i). `empty` labels no choice."""
    if len(options) > _SEARCHABLE_THRESHOLD:
        return searchable_select(name, options, value=value, placeholder=t(empty), aria_label=label)
    return Select(Option(t(empty), value="", selected=not value),
                  *[Option(text, value=v, selected=v == value) for v, text in options],
                  name=name, cls="form-input", aria_label=label)


def _account_options(chart: list[dict]) -> list[tuple[str, str]]:
    return [(a["code"], f"{a['code']} {a['name']}") for a in _postable(chart)]


def _field(label: str, control: FT, hint: str | None = None) -> FT:
    return Div(Label(label, cls="form-label"), control, *( [P(hint, cls="form-hint")] if hint else []),
               cls="form-group")


def _input(name: str, value="", type_: str = "text", **attrs) -> FT:
    return Input(type=type_, name=name, value="" if value is None else str(value), cls="form-input",
                 onkeydown="if(event.key==='Escape'){this.blur();}", **attrs)


async def _context(token: str) -> tuple[dict, str]:
    """The fixed assets setup and the company currency."""
    setup = await api.fixed_assets_get(token, "/setup")
    currency = ""
    try:
        currency = (await api.get_company(token)).get("currency") or ""
    except APIError:
        pass
    return setup, currency


async def _page(request: Request, *content, title_key: str, nav: str = "fixed_assets"):
    return await base_shell(*content, title=page_title(title_key), nav_active=nav, request=request)


def _refused(request: Request, e: APIError, title_key: str):
    if e.status == 401:
        return RedirectResponse("/login", status_code=302)
    return _page(request, page_header(t(title_key)), Div(_error_text(e), cls="flash flash--error"),
                 title_key=title_key)


# ── Review and confirm ───────────────────────────────────────────────────────


def _preview(action: str, result: dict, currency: str) -> FT:
    """What an operation will do: the assets it creates or changes, and every journal line."""
    parts: list[FT] = []
    assets = result.get("assets") or []
    if assets:
        rows = [Tr(Td(a.get("number") or EMPTY), Td(_text(a.get("name"))),
                   _money_td(a.get("cost", a.get("accumulated_depreciation")), currency)) for a in assets]
        parts.append(Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                                    _money_th(t("fixed_assets.amount")))),
                           Tbody(*rows), cls="data-table"))
    journals = result.get("journals") or []
    parts += [_lines_table(je.get("lines") or [], currency) for je in journals]
    for month in result.get("months") or []:
        parts.append(H4(f"{month['month']}: {_money(month['total'], currency)}"))
        parts.append(_lines_table(month["lines"], currency) if month["lines"]
                     else P(t("fixed_assets.month_posts_nothing"), cls="form-hint"))
        if month["exceptions"]:
            parts.append(_held_table(month["exceptions"], currency))
    if result.get("empty"):
        parts.append(P(t("fixed_assets.nothing_to_depreciate"), cls="form-hint"))
    elif not journals and not result.get("months"):
        parts.append(P(t("fixed_assets.posts_nothing"), cls="form-hint"))
    return Div(H3(t("fixed_assets.review_title"), cls="section-title"), *parts,
               Input(type="hidden", name="fingerprint", value=result.get("fingerprint", "")),
               Div(Button(t("fixed_assets.confirm"), type="button", cls="btn btn--primary",
                          hx_post=f"/fixed-assets/op/{action}/confirm", hx_include="closest form",
                          hx_target="#fa-preview", hx_swap="outerHTML"),
                   cls="form-actions"),
               cls="detail-card section-card", id="fa-preview")


def _lines_table(lines: list[dict], currency: str) -> FT:
    rows = [Tr(Td(ln["account"]), _money_td(ln.get("debit") if ln.get("debit") != "0.00" else None, currency),
               _money_td(ln.get("credit") if ln.get("credit") != "0.00" else None, currency)) for ln in lines]
    return Table(Thead(Tr(Th(t("fixed_assets.account")), _money_th(t("fixed_assets.debit")),
                          _money_th(t("fixed_assets.credit")))), Tbody(*rows), cls="data-table")


def _held_table(held: list[dict], currency: str) -> FT:
    """Assets held out of a run, what they would have posted, and what clears them."""
    rows = [Tr(Td(A(h["number"], href=f"/fixed-assets/assets/{h['asset_id']}")), _money_td(h.get("amount"), currency),
               Td(t(f"fixed_assets.action.{h['action']}"))) for h in held]
    return Table(Thead(Tr(Th(t("fixed_assets.held_asset")), _money_th(t("fixed_assets.held_amount")),
                          Th(t("fixed_assets.held_action")))), Tbody(*rows), cls="data-table")


def _op_form(action: str, *fields: FT, review_label: str | None = None) -> FT:
    """A two-step operation form: Review fills #fa-preview, Confirm (inside it) does it."""
    return Form(*fields, operation_key_input(),
                Div(Button(review_label or t("fixed_assets.review"), type="button", cls="btn btn--secondary",
                           hx_post=f"/fixed-assets/op/{action}/review", hx_include="closest form",
                           hx_target="#fa-preview", hx_swap="outerHTML"),
                    cls="form-actions"),
                Div(id="fa-preview"),
                onsubmit="return false;", cls="settings-card")


def _amount(form, name: str):
    value = str(form.get(name, "")).strip()
    return value or None


def _register_body(form) -> dict:
    mode = str(form.get("mode") or "separate")
    body = {"source": str(form.get("source") or ""), "mode": mode}
    if mode in ("expense", "maintenance"):
        body["expense_account"] = str(form.get("expense_account") or "")
        if mode == "maintenance":
            body["asset_id"] = str(form.get("asset_id") or "")
    else:
        body.update(class_id=str(form.get("class_id") or ""), name=str(form.get("name") or ""),
                    available_date=str(form.get("available_date") or ""))
    return body


def _link_body(form) -> dict:
    allocations = [{"asset_id": key.removeprefix("share:"), "amount": str(value).strip()}
                   for key, value in form.multi_items() if key.startswith("share:") and str(value).strip()]
    return {"source": str(form.get("source") or ""), "allocations": allocations}


def _asset_body(form) -> dict:
    body = {"kind": str(form.get("kind") or ""), "name": str(form.get("name") or ""),
            "class_id": str(form.get("class_id") or "")}
    if body["kind"] != "physical":
        body.update(available_date=str(form.get("available_date") or ""), cost=_amount(form, "cost"))
    if body["kind"] == "opening":
        body.update(accumulated_depreciation=_amount(form, "accumulated_depreciation") or "0",
                    accumulated_impairment=_amount(form, "accumulated_impairment") or "0")
    if body["kind"] == "contributed":
        body["credit_account"] = str(form.get("credit_account") or "")
    return body


def _financials_body(form) -> dict:
    return {k: v for k, v in _asset_body(form).items() if k != "name"}


# action -> (API path for the form, body from the form, where the done result leads)
_OPERATIONS = {
    "register": (lambda f: "/register", _register_body,
                 lambda r, f: f"/fixed-assets/assets/{r['assets'][0]['id']}" if r.get("assets")
                 else "/fixed-assets/intake"),
    "not-acquisition": (lambda f: "/not-acquisition",
                        lambda f: {"source": str(f.get("source") or ""), "reason": str(f.get("reason") or "")},
                        lambda r, f: "/fixed-assets/intake"),
    "link-depreciation": (lambda f: "/link-depreciation", _link_body, lambda r, f: "/fixed-assets/intake"),
    "add-asset": (lambda f: "/assets", _asset_body,
                  lambda r, f: f"/fixed-assets/assets/{r['assets'][0]['id']}"),
    "financial-details": (lambda f: f"/assets/{quote(str(f.get('asset_id') or ''), safe=':')}/financial-details",
                          _financials_body, lambda r, f: f"/fixed-assets/assets/{f.get('asset_id')}"),
    "undo-asset": (lambda f: f"/assets/{quote(str(f.get('asset_id') or ''), safe=':')}/undo",
                   lambda f: {}, lambda r, f: "/fixed-assets"),
    "run": (lambda f: "/runs", lambda f: {"month": str(f.get("month") or "")}, lambda r, f: "/fixed-assets/runs"),
    "reverse-run": (lambda f: f"/runs/{quote(str(f.get('month') or ''), safe='')}/reverse", lambda f: {},
                    lambda r, f: f"/fixed-assets/runs/{f.get('month')}"),
}


# ── Pages ────────────────────────────────────────────────────────────────────


def _start_panel(currency: str) -> FT:
    from celerp_fixed_assets.setup import STARTER_CLASSES

    return Div(
        H2(t("fixed_assets.start_title"), cls="section-title"),
        P(t("fixed_assets.start_hint"), cls="settings-hint"),
        Ul(*[Li(t(f"fixed_assets.starter.{name.lower().replace(' ', '_')}")) for name, *_ in STARTER_CLASSES]),
        P(t("fixed_assets.start_defaults"), cls="settings-hint"),
        Button(t("fixed_assets.start"), type="button", cls="btn btn--primary",
               hx_post="/fixed-assets/start", hx_swap="none"),
        cls="settings-card", id="fa-start",
    )


def _document(line: dict) -> str:
    return line.get("document_ref") or line.get("document") or EMPTY


def _source_document(source: str) -> str:
    """The document or journal a source line ("<document>:<line>") sits on."""
    return source.rsplit(":", 1)[0]


def _source_href(source: str) -> str:
    document = _source_document(source)
    if document.startswith(MANUAL_PREFIX):
        return f"/accounting?{urlencode({'q': document})}"
    return f"/docs/{quote(document, safe=':')}"


def _class_names(classes: list[dict]) -> dict[str, str]:
    return {c["id"]: c["name"] for c in classes}


def _register_table(items: list[dict], classes: dict[str, str], currency: str) -> FT:
    rows = [Tr(Td(A(a["number"], href=f"/fixed-assets/assets/{a['id']}")), Td(_text(a.get("name"))),
               Td(_text(classes.get(a.get("class_id")))), Td(_text(a.get("available_date"))),
               _money_td(a.get("cost"), currency), _money_td(a.get("accumulated_depreciation"), currency),
               _money_td(a.get("nbv"), currency), Td(t(f"fixed_assets.status.{a.get('status')}")))
            for a in items]
    return Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")), Th(t("fixed_assets.class")),
                          Th(t("fixed_assets.available_date")), _money_th(t("fixed_assets.cost")),
                          _money_th(t("fixed_assets.accumulated_depreciation")), _money_th(t("fixed_assets.nbv")),
                          Th(t("fixed_assets.status")))),
                 Tbody(*rows), cls="data-table", id="fa-register")


def _filters(q: str, class_id: str, classes: list[dict]) -> FT:
    return Form(
        _input("q", q, type_="search", placeholder=t("fixed_assets.search"), aria_label=t("fixed_assets.search")),
        _picker("class_id", [(c["id"], c["name"]) for c in classes], class_id, label=t("fixed_assets.class"),
                empty="fixed_assets.all_classes"),
        Button(t("fixed_assets.filter"), type="submit", cls="btn btn--secondary"),
        method="get", action="/fixed-assets", cls="flex-row",
    )


def _settings_form(settings: dict) -> FT:
    from celerp_fixed_assets.calc.rules import CONVENTIONS
    from celerp_fixed_assets.setup import RUN_MODES

    return Form(
        H2(t("fixed_assets.settings_title"), cls="section-title"),
        _field(t("fixed_assets.run_mode"), Select(
            *[Option(t(f"fixed_assets.run_mode.{m}"), value=m, selected=m == settings.get("run_mode"))
              for m in RUN_MODES], name="run_mode", cls="form-input"), t("fixed_assets.run_mode_hint")),
        _field(t("fixed_assets.run_day"), _input("run_day", settings.get("run_day"), type_="number", min="1",
                                                 max="28", step="1"), t("fixed_assets.run_day_hint")),
        _field(t("fixed_assets.convention"), Select(
            *[Option(t(f"fixed_assets.convention.{c}"), value=c, selected=c == settings.get("convention"))
              for c in CONVENTIONS], name="convention", cls="form-input")),
        _field(t("fixed_assets.threshold"), _input("threshold", settings.get("threshold"), type_="number",
                                                   step="0.01", min="0"), t("fixed_assets.threshold_hint")),
        _field(t("fixed_assets.first_managed_period"),
               _input("first_managed_period", settings.get("first_managed_period"), type_="date"),
               t("fixed_assets.first_managed_period_hint")),
        hx_post="/settings/fixed-assets", hx_trigger="change", hx_swap="none", cls="settings-card",
    )


_CLASS_EDITABLE = {"name": "text", "method": "select", "life_months": "number", "residual_percent": "number",
                   "asset_account": "select", "accumulated_depreciation_account": "select",
                   "depreciation_account": "select"}


def _class_cell(cls: dict, field: str, chart: list[dict]) -> FT:
    value = cls.get(field)
    labels = None
    if field == "method":
        labels = {m: t(f"fixed_assets.method.{m}") for m in _methods()}
    return display_cell(cls["id"], field, value, "text", label_map=labels,
                        cls_extra="cell--number" if field in ("life_months", "residual_percent") else "",
                        edit_url=f"/settings/fixed-assets/classes/{quote(cls['id'], safe=':')}/{field}/edit")


def _methods() -> list[str]:
    from celerp_fixed_assets.calc.rules import METHODS

    return list(METHODS)


def _classes_card(classes: list[dict], chart: list[dict]) -> FT:
    rows = [Tr(*[_class_cell(c, f, chart) for f in _CLASS_EDITABLE],
               Td(Button(t("fixed_assets.restore") if c.get("archived") else t("fixed_assets.archive"),
                         type="button", cls="btn btn--sm btn--secondary",
                         hx_post=f"/settings/fixed-assets/classes/{quote(c['id'], safe=':')}/archive",
                         hx_vals=f'{{"archived": "{"0" if c.get("archived") else "1"}"}}',
                         hx_target="#fa-classes", hx_swap="outerHTML")),
               cls="row--muted" if c.get("archived") else None)
            for c in classes]
    return Div(
        Div(H2(t("fixed_assets.classes_title"), cls="section-title"),
            Button(t("fixed_assets.add_class"), type="button", cls="btn btn--sm btn--primary",
                   hx_post="/settings/fixed-assets/classes", hx_target="#fa-classes", hx_swap="outerHTML"),
            cls="flex-row flex-between settings-card-header"),
        P(t("fixed_assets.classes_hint"), cls="settings-hint"),
        Div(Table(Thead(Tr(Th(t("fixed_assets.name")), Th(t("fixed_assets.method")),
                       Th(t("fixed_assets.life_months"), cls="cell--number"),
                       Th(t("fixed_assets.residual_percent"), cls="cell--number"),
                       Th(t("fixed_assets.asset_account")), Th(t("fixed_assets.accumulated_depreciation_account")),
                       Th(t("fixed_assets.depreciation_account")), Th(""))),
              Tbody(*rows), cls="data-table"), cls="table-scroll-wrap"),
        cls="settings-card", id="fa-classes",
    )


def _movements_table(items: list[dict], currency: str) -> FT:
    if not items:
        return P(t("fixed_assets.no_movements"), cls="form-hint")
    rows = []
    for m in items:
        amounts = m.get("amounts") or {}
        rows.append(Tr(Td(_text(m.get("posting_date"))), Td(_text(m.get("effective_date"))),
                       Td(t(f"fixed_assets.movement.{m.get('operation')}")),
                       _money_td(amounts.get("cost") if m.get("amounts") is not None else None, currency),
                       _money_td(amounts.get("accumulated_depreciation") if m.get("amounts") is not None else None,
                                 currency)))
    return Table(Thead(Tr(Th(t("fixed_assets.posting_date")), Th(t("fixed_assets.effective_date")),
                          Th(t("fixed_assets.movement")), _money_th(t("fixed_assets.cost")),
                          _money_th(t("fixed_assets.accumulated_depreciation")))),
                 Tbody(*rows), cls="data-table")


def _asset_cell(asset: dict, field: str) -> FT:
    return display_cell(asset["id"], field, asset.get(field), "text",
                        edit_url=f"/fixed-assets/assets/{quote(asset['id'], safe=':')}/field/{field}/edit")


def _details_card(asset: dict) -> FT:
    return Div(
        H2(t("fixed_assets.details"), cls="section-title"),
        Table(Tbody(*[Tr(Th(t(f"fixed_assets.{f}")), _asset_cell(asset, f)) for f in DESCRIPTIVE_FIELDS]),
              cls="data-table detail-table"),
        cls="detail-card section-card", id="fa-details",
    )


def _figures_card(asset: dict, class_name: str, currency: str) -> FT:
    rows = [(t("fixed_assets.class"), _text(class_name)), (t("fixed_assets.status"),
                                                          t(f"fixed_assets.status.{asset.get('status')}")),
            (t("fixed_assets.quantity"), _text(asset.get("quantity"))),
            (t("fixed_assets.available_date"), _text(asset.get("available_date")))]
    money = [("cost", "fixed_assets.cost"), ("accumulated_depreciation", "fixed_assets.accumulated_depreciation"),
             ("accumulated_impairment", "fixed_assets.accumulated_impairment"), ("nbv", "fixed_assets.nbv")]
    return Div(
        H2(t("fixed_assets.figures"), cls="section-title"),
        Table(Tbody(*[Tr(Th(label), Td(value)) for label, value in rows],
                    *[Tr(Th(t(key)), _money_td(asset.get(field), currency)) for field, key in money]),
              cls="data-table detail-table"),
        cls="detail-card section-card",
    )


def _sources_card(asset: dict, currency: str) -> FT:
    sources = asset.get("sources") or []
    if not sources:
        body = P(t("fixed_assets.no_sources"), cls="form-hint")
    else:
        body = Ul(*[Li(A(s.get("ref") or _source_document(s["source"]), href=_source_href(s["source"])), " ",
                       _money(s.get("amount"), currency) if s.get("amount") is not None else "")
                    for s in sources])
    return Div(H2(t("fixed_assets.sources"), cls="section-title"), body, cls="detail-card section-card")


def _exception_card(asset: dict, currency: str) -> FT:
    """What holds an asset out of depreciation, and what clears it."""
    held = asset["exception"]
    months = ", ".join(held.get("months") or [])
    return Div(H2(t("fixed_assets.exception_title"), cls="section-title"),
               P(t("fixed_assets.exception_months", months=months), " ",
                 t("fixed_assets.exception_amount"), ": ", _money(held.get("amount"), currency)),
               P(Strong(t(f"fixed_assets.action.{held['action']}")), " ", t(f"fixed_assets.action_hint.{held['action']}")),
               cls="flash flash--warning", id="fa-exception")


def _skips_card(asset: dict) -> FT:
    """Months held out of depreciation with a reason, and the form that adds one."""
    asset_id = quote(asset["id"], safe=":")
    skips = sorted((asset.get("skips") or {}).items(), reverse=True)
    rows = [Tr(Td(month), Td(reason),
               Td(Button(t("fixed_assets.withdraw_skip"), type="button", cls="btn btn--sm btn--secondary",
                         hx_delete=f"/fixed-assets/assets/{asset_id}/skip/{month}", hx_swap="none")))
            for month, reason in skips]
    table = Table(Thead(Tr(Th(t("fixed_assets.month")), Th(t("fixed_assets.reason")), Th(""))), Tbody(*rows),
                  cls="data-table") if rows else P(t("fixed_assets.no_skips"), cls="form-hint")
    form = Form(_field(t("fixed_assets.month"), _input("month", type_="month", required=True)),
                _field(t("fixed_assets.reason"), _input("reason", required=True)),
                Div(Button(t("fixed_assets.skip_month"), type="submit", cls="btn btn--secondary"), cls="form-actions"),
                hx_post=f"/fixed-assets/assets/{asset_id}/skip", hx_swap="none", cls="flex-row")
    return Div(H2(t("fixed_assets.skips_title"), cls="section-title"), P(t("fixed_assets.skips_hint"), cls="settings-hint"),
               table, form, cls="detail-card section-card")


def _readings_card(asset: dict) -> FT:
    """Units an asset produced per month, for a class that depreciates by units."""
    asset_id = quote(asset["id"], safe=":")
    readings = sorted((asset.get("readings") or {}).items(), reverse=True)
    table = Table(Thead(Tr(Th(t("fixed_assets.month")), Th(t("fixed_assets.units"), cls="cell--number"))),
                  Tbody(*[Tr(Td(p), Td(u, cls="cell--number")) for p, u in readings]),
                  cls="data-table") if readings else P(t("fixed_assets.no_readings"), cls="form-hint")
    form = Form(_field(t("fixed_assets.month"), _input("period", type_="month", required=True)),
                _field(t("fixed_assets.units"), _input("units", type_="number", step="any", min="0", required=True)),
                Div(Button(t("fixed_assets.record_reading"), type="submit", cls="btn btn--secondary"),
                    cls="form-actions"),
                hx_post=f"/fixed-assets/assets/{asset_id}/readings", hx_swap="none", cls="flex-row")
    return Div(H2(t("fixed_assets.readings_title"), cls="section-title"), table, form, cls="detail-card section-card")


def _attention_card(items: list[dict]) -> FT:
    return Div(H3(t("fixed_assets.attention_title")), P(t("fixed_assets.attention_hint")),
               Ul(*[Li(A(f"{a['number']} {a.get('name') or ''}".strip(), href=f"/fixed-assets/assets/{a['asset_id']}"),
                       " ", t(f"fixed_assets.action.{a['exception']['action']}")) for a in items]),
               cls="flash flash--warning", id="fa-attention")


def _last_month(today: date) -> str:
    return f"{today.year - 1}-12" if today.month == 1 else f"{today.year}-{today.month - 1:02d}"


def _runs_table(items: list[dict], currency: str) -> FT:
    rows = [Tr(Td(A(r["month"], href=f"/fixed-assets/runs/{r['month']}")),
               Td(t(f"fixed_assets.run_status.{r['status']}")), _money_td(r.get("total"), currency),
               Td(str(len(r.get("exceptions") or [])), cls="cell--number")) for r in items]
    return Table(Thead(Tr(Th(t("fixed_assets.month")), Th(t("fixed_assets.status")), _money_th(t("label.total")),
                          Th(t("fixed_assets.held_count"), cls="cell--number"))),
                 Tbody(*rows), cls="data-table", id="fa-runs")


async def _hx_write(token: str, method: str, path: str, body: dict, done: str) -> Response:
    """A plain write from a small form: on success go to ``done`` with a toast, else say why."""
    try:
        await api.fixed_assets_send(token, method, path, body)
    except APIError as e:
        return Response(headers=toast_header(_error_text(e), "error", persist=True))
    return Response(headers={"HX-Redirect": done, **toast_header(t("fixed_assets.done"))})


def _class_select(classes: list[dict], value: str = "") -> FT:
    return _picker("class_id", [(c["id"], c["name"]) for c in classes], value, label=t("fixed_assets.class"))


def _asset_fields(kind: str, classes: list[dict], chart: list[dict], *, with_name: bool = True) -> list[FT]:
    fields = []
    if with_name:
        fields.append(_field(t("fixed_assets.name"), _input("name", required=True)))
    fields.append(_field(t("fixed_assets.class"), _class_select(classes)))
    if kind != "physical":
        fields += [_field(t("fixed_assets.available_date"), _input("available_date", type_="date")),
                   _field(t("fixed_assets.cost"), _input("cost", type_="number", step="0.01", min="0"))]
    if kind == "opening":
        fields += [_field(t("fixed_assets.accumulated_depreciation"),
                          _input("accumulated_depreciation", "0", type_="number", step="0.01", min="0")),
                   _field(t("fixed_assets.accumulated_impairment"),
                          _input("accumulated_impairment", "0", type_="number", step="0.01", min="0"))]
    if kind == "contributed":
        fields.append(_field(t("fixed_assets.credit_account"),
                             _picker("credit_account", _account_options(chart), label=t("fixed_assets.credit_account")),
                             t("fixed_assets.contributed_bill_hint")))
    return fields


def _kind_tabs(base: str, kind: str, kinds: tuple[str, ...]) -> FT:
    return Div(*[A(t(f"fixed_assets.kind.{k}"), href=f"{base}?kind={k}",
                   cls="btn btn--sm " + ("btn--primary" if k == kind else "btn--secondary")) for k in kinds],
               cls="flex-row")


def setup_ui_routes(app) -> None:
    """Register the fixed assets pages into the FastHTML app."""

    @app.get("/fixed-assets")
    async def register_page(request: Request, q: str = "", class_id: str = "", page: int = 1,
                            per_page: int = PER_PAGE):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        page, per_page = max(page, 1), min(max(per_page, 1), 500)
        try:
            setup, currency = await _context(token)
            if not setup["started"]:
                return await _page(request, page_header(t("fixed_assets.title")), _start_panel(currency),
                                   title_key="fixed_assets.title")
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
            params = {k: v for k, v in {"q": q, "class_id": class_id}.items() if v}
            found = await api.fixed_assets_get(token, "/assets", {**params, "limit": per_page,
                                                                   "offset": (page - 1) * per_page})
            held = (await api.fixed_assets_get(token, "/attention"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.title")
        actions = (A(t("fixed_assets.runs_title"), href="/fixed-assets/runs", cls="btn btn--secondary"),
                   A(t("fixed_assets.register_from_bills"), href="/fixed-assets/intake", cls="btn btn--secondary"),
                   A(t("fixed_assets.add_asset"), href="/fixed-assets/assets/new", cls="btn btn--primary"))
        if not found["items"] and not params:
            body = Div(empty_state_cta(t("fixed_assets.empty"), t("fixed_assets.register_from_bills"),
                                       "/fixed-assets/intake", hint=t("fixed_assets.empty_hint")))
        elif not found["items"]:
            body = P(t("fixed_assets.no_match"), cls="form-hint")
        else:
            body = Div(_register_table(found["items"], _class_names(classes), currency),
                       pagination(page, found["total"], per_page, "/fixed-assets", urlencode(params)))
        return await _page(request, page_header(t("fixed_assets.title"), *actions),
                           P(t("fixed_assets.register_hint"), cls="settings-hint"),
                           _attention_card(held) if held else "",
                           _filters(q, class_id, [c for c in classes if not c.get("archived")]), body,
                           title_key="fixed_assets.title")

    @app.post("/fixed-assets/start")
    async def start(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        try:
            await api.fixed_assets_send(token, "POST", "/start", {})
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))
        return Response(headers={"HX-Redirect": "/fixed-assets"})

    @app.get("/fixed-assets/intake")
    async def intake_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            items = (await api.fixed_assets_get(token, "/intake"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.intake_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.intake_title"), None)])
        if not items:
            body = empty_state_cta(t("fixed_assets.intake_empty"), t("fixed_assets.title"), "/fixed-assets")
        else:
            rows = [Tr(Td(_text(ln.get("date"))),
                       Td(A(_document(ln), href=ln["href"])),
                       Td(_text(ln.get("description"))), Td(_text(ln.get("account"))),
                       Td(_text(ln.get("quantity")), cls="cell--number"), _money_td(ln.get("amount"), currency),
                       Td(A(t("fixed_assets.deal_with_line"), cls="btn btn--sm btn--primary",
                            href=f"/fixed-assets/intake/line?{urlencode({'source': ln['source']})}")))
                    for ln in items]
            body = Table(Thead(Tr(Th(t("fixed_assets.date")), Th(t("fixed_assets.document")),
                                  Th(t("fixed_assets.description")), Th(t("fixed_assets.account")),
                                  Th(t("fixed_assets.quantity"), cls="cell--number"),
                                  _money_th(t("fixed_assets.amount")), Th(""))),
                         Tbody(*rows), cls="data-table")
        return await _page(request, crumbs, page_header(t("fixed_assets.intake_title")),
                           P(t("fixed_assets.intake_hint"), cls="settings-hint"), body,
                           title_key="fixed_assets.intake_title")

    @app.get("/fixed-assets/intake/line")
    async def line_page(request: Request, source: str = "", action: str = ""):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            items = (await api.fixed_assets_get(token, "/intake"))["items"]
            classes = (await api.fixed_assets_get(token, "/classes"))["items"]
            assets = (await api.fixed_assets_get(token, "/assets", {"limit": 500}))["items"]
            chart = (await api.get_chart(token)).get("items") or []
        except APIError as e:
            return await _refused(request, e, "fixed_assets.intake_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (t("fixed_assets.intake_title"), "/fixed-assets/intake"),
                              (t("fixed_assets.line_title"), None)])
        line = next((ln for ln in items if ln["source"] == source), None)
        if line is None:
            return await _page(request, crumbs, page_header(t("fixed_assets.line_title")),
                               empty_state_cta(t("fixed_assets.line_gone"), t("fixed_assets.intake_title"),
                                               "/fixed-assets/intake"),
                               title_key="fixed_assets.line_title")
        summary = Div(P(Strong(_text(line.get("description"))), " ", _document(line), " ",
                        _text(line.get("date"))),
                      P(t("fixed_assets.account"), ": ", _text(line.get("account")), " ",
                        t("fixed_assets.amount"), ": ", _money(line.get("amount"), currency)),
                      cls="detail-card section-card")
        hidden = Input(type="hidden", name="source", value=source)
        manual = line["source"].startswith(MANUAL_PREFIX)
        if line["kind"] != "acquisition":
            active = [a for a in assets if a.get("status") == "active"]
            shares = [_field(f"{a['number']} {a.get('name') or ''}",
                             _input(f"share:{a['id']}", type_="number", step="0.01", min="0")) for a in active]
            form = _op_form("link-depreciation", hidden, P(t("fixed_assets.link_hint"), cls="settings-hint"),
                            *(shares or [P(t("fixed_assets.no_active_assets"), cls="form-hint")]))
            return await _page(request, crumbs, page_header(t("fixed_assets.line_title")), summary, form,
                               title_key="fixed_assets.line_title")
        choices = ["register", "expense", "maintenance"] + (["not-acquisition"] if manual else [])
        action = action if action in choices else "register"
        tabs = Div(*[A(t(f"fixed_assets.line_action.{c}"), cls="btn btn--sm " + (
            "btn--primary" if c == action else "btn--secondary"),
            href=f"/fixed-assets/intake/line?{urlencode({'source': source, 'action': c})}") for c in choices],
            cls="flex-row")
        if action == "not-acquisition":
            form = _op_form("not-acquisition", hidden,
                            _field(t("fixed_assets.reason"), _input("reason", required=True),
                                   t("fixed_assets.reason_hint")))
        elif action in ("expense", "maintenance"):
            fields = [hidden, Input(type="hidden", name="mode", value=action),
                      _field(t("fixed_assets.expense_account"),
                             _picker("expense_account", _account_options(chart),
                                     label=t("fixed_assets.expense_account")))]
            if action == "maintenance":
                fields.append(_field(t("fixed_assets.asset"), _picker(
                    "asset_id", [(a["id"], f"{a['number']} {a.get('name') or ''}") for a in assets
                                 if a.get("status") == "active"], label=t("fixed_assets.asset"))))
            form = _op_form("register", *fields)
        else:
            quantity = line.get("quantity") or "1"
            modes = [Label(Input(type="radio", name="mode", value="separate", checked=True),
                           t("fixed_assets.mode.separate", quantity=quantity), cls="settings-toggle"),
                     Label(Input(type="radio", name="mode", value="quantity"),
                           t("fixed_assets.mode.quantity", quantity=quantity), cls="settings-toggle")]
            threshold = line.get("below_threshold")
            form = _op_form("register", hidden,
                            *([P(t("fixed_assets.below_threshold"), cls="flash flash--warning")] if threshold else []),
                            _field(t("fixed_assets.name"), _input("name", line.get("description"))),
                            _field(t("fixed_assets.class"), _class_select(classes)),
                            _field(t("fixed_assets.available_date"), _input("available_date", line.get("date"),
                                                                            type_="date")),
                            *(modes if str(quantity) not in ("1", "1.0", "1.00") else []))
        return await _page(request, crumbs, page_header(t("fixed_assets.line_title")), summary, tabs, form,
                           title_key="fixed_assets.line_title")

    @app.post("/fixed-assets/op/{action}/{step}")
    async def operate(request: Request, action: str, step: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        if action not in _OPERATIONS or step not in ("review", "confirm"):
            return Response(status_code=404)
        path_of, body_of, done_href = _OPERATIONS[action]
        form = await request.form()
        body = {**body_of(form), "op_key": str(form.get(OP_KEY_FIELD) or "")}
        if step == "review":
            body["preview"] = True
        else:
            body["fingerprint"] = str(form.get("fingerprint") or "")
        try:
            result = await api.fixed_assets_send(token, "POST", path_of(form), body)
            _, currency = await _context(token)
        except APIError as e:
            return Div(Div(_error_text(e), cls="flash flash--error"),
                       *([Button(t("fixed_assets.review_again"), type="button", cls="btn btn--secondary",
                                 hx_post=f"/fixed-assets/op/{action}/review", hx_include="closest form",
                                 hx_target="#fa-preview", hx_swap="outerHTML")] if step == "confirm" else []),
                       id="fa-preview")
        if step == "review":
            return _preview(action, result, currency)
        return Response(headers={"HX-Redirect": done_href(result, form),
                                 **toast_header(t("fixed_assets.done"))})

    @app.get("/fixed-assets/assets/new")
    async def new_asset_page(request: Request, kind: str = "opening"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        kinds = ("opening", "contributed", "physical")
        kind = kind if kind in kinds else "opening"
        try:
            await _context(token)
            classes = (await api.fixed_assets_get(token, "/classes"))["items"]
            chart = (await api.get_chart(token)).get("items") or [] if kind == "contributed" else []
        except APIError as e:
            return await _refused(request, e, "fixed_assets.add_asset")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.add_asset"), None)])
        form = _op_form("add-asset", Input(type="hidden", name="kind", value=kind),
                        P(t(f"fixed_assets.kind_hint.{kind}"), cls="settings-hint"),
                        *_asset_fields(kind, classes, chart))
        return await _page(request, crumbs, page_header(t("fixed_assets.add_asset")),
                           _kind_tabs("/fixed-assets/assets/new", kind, kinds), form,
                           title_key="fixed_assets.add_asset")

    @app.get("/fixed-assets/assets/{asset_id}")
    async def asset_page(request: Request, asset_id: str, kind: str = "opening"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
            movements = (await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/movements"))["items"]
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (asset["number"], None)])
        undo = _op_form("undo-asset", Input(type="hidden", name="asset_id", value=asset_id),
                        P(t("fixed_assets.undo_hint"), cls="settings-hint"),
                        review_label=t("fixed_assets.undo"))
        extra: list[FT] = []
        if asset.get("status") == "physical":
            kinds = ("opening", "contributed")
            kind = kind if kind in kinds else "opening"
            chart = []
            if kind == "contributed":
                try:
                    chart = (await api.get_chart(token)).get("items") or []
                except APIError:
                    chart = []
            extra = [Div(H2(t("fixed_assets.add_financial_details"), cls="section-title"),
                         _kind_tabs(f"/fixed-assets/assets/{asset_id}", kind, kinds),
                         _op_form("financial-details", Input(type="hidden", name="asset_id", value=asset_id),
                                  Input(type="hidden", name="kind", value=kind),
                                  *_asset_fields(kind, classes, chart, with_name=False)),
                         cls="detail-card section-card")]
        if asset.get("status") == "active":
            from celerp_fixed_assets.calc.rules import METHODS

            cls = next((c for c in classes if c["id"] == asset.get("class_id")), {})
            if cls.get("method") in METHODS and METHODS[cls["method"]].uses_readings:
                extra.append(_readings_card(asset))
            extra.append(_skips_card(asset))
        title = f"{asset['number']} {asset.get('name') or ''}".strip()
        return await _page(request, crumbs, page_header(title),
                           _exception_card(asset, currency) if asset.get("exception") else "",
                           Div(_details_card(asset),
                               _figures_card(asset, _class_names(classes).get(asset.get("class_id")), currency),
                               cls="detail-grid"),
                           _sources_card(asset, currency), *extra,
                           Div(H2(t("fixed_assets.movements"), cls="section-title"),
                               _movements_table(movements, currency), cls="detail-card section-card"),
                           Div(H2(t("fixed_assets.undo"), cls="section-title"), undo,
                               cls="detail-card section-card") if asset.get("status") != "undone" else "",
                           title_key="fixed_assets.title")

    async def _asset_field_cell(token: str, asset_id: str, field: str) -> FT:
        asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
        return _asset_cell(asset, field)

    @app.get("/fixed-assets/assets/{asset_id}/field/{field}/edit")
    async def asset_field_edit(request: Request, asset_id: str, field: str):
        token = _token(request)
        if not token or field not in DESCRIPTIVE_FIELDS:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
        except APIError as e:
            return P(_error_text(e), cls="cell-error")
        url = f"/fixed-assets/assets/{quote(asset_id, safe=':')}/field/{field}"
        return editable_cell(asset_id, field, asset.get(field), "text", patch_url=url, restore_url=f"{url}/display",
                             aria_label=t(f"fixed_assets.{field}"))

    @app.get("/fixed-assets/assets/{asset_id}/field/{field}/display")
    async def asset_field_display(request: Request, asset_id: str, field: str):
        token = _token(request)
        if not token or field not in DESCRIPTIVE_FIELDS:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            return await _asset_field_cell(token, asset_id, field)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")

    @app.patch("/fixed-assets/assets/{asset_id}/field/{field}")
    async def asset_field_patch(request: Request, asset_id: str, field: str):
        token = _token(request)
        if not token or field not in DESCRIPTIVE_FIELDS:
            return P(t("fixed_assets.error"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", "")).strip() or None
        path = f"/assets/{quote(asset_id, safe=':')}"
        try:
            asset = await api.fixed_assets_get(token, path)
            asset = await api.fixed_assets_send(token, "PATCH", path, {
                "changes": {field: value}, "previous": {field: asset.get(field)}})
        except APIError as e:
            return Td(P(_error_text(e), cls="cell-error"))
        return _asset_cell(asset, field)

    @app.post("/fixed-assets/assets/{asset_id}/skip")
    async def skip_month(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        return await _hx_write(token, "POST", f"/assets/{quote(asset_id, safe=':')}/skip",
                               {"month": str(form.get("month") or ""), "reason": str(form.get("reason") or "")},
                               f"/fixed-assets/assets/{asset_id}")

    @app.delete("/fixed-assets/assets/{asset_id}/skip/{month}")
    async def withdraw_skip(request: Request, asset_id: str, month: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        return await _hx_write(token, "DELETE", f"/assets/{quote(asset_id, safe=':')}/skip/{quote(month, safe='')}",
                               {}, f"/fixed-assets/assets/{asset_id}")

    @app.post("/fixed-assets/assets/{asset_id}/readings")
    async def record_reading(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        return await _hx_write(token, "POST", f"/assets/{quote(asset_id, safe=':')}/readings",
                               {"period": str(form.get("period") or ""), "units": str(form.get("units") or "")},
                               f"/fixed-assets/assets/{asset_id}")

    @app.get("/fixed-assets/runs")
    async def runs_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            setup, currency = await _context(token)
            if not setup["started"]:
                return RedirectResponse("/fixed-assets", status_code=302)
            items = (await api.fixed_assets_get(token, "/runs"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.runs_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.runs_title"), None)])
        form = _op_form("run", _field(t("fixed_assets.run_through"),
                                      _input("month", _last_month(date.today()), type_="month", required=True),
                                      t("fixed_assets.run_through_hint")))
        body = _runs_table(items, currency) if items else empty_state_cta(t("fixed_assets.runs_empty"))
        mode = t(f"fixed_assets.run_mode.{setup['settings']['run_mode']}")
        return await _page(request, crumbs, page_header(t("fixed_assets.runs_title")),
                           P(t("fixed_assets.runs_hint", mode=mode, day=setup["settings"]["run_day"]),
                             " ", A(t("fixed_assets.settings_title"), href="/settings/fixed-assets"),
                             cls="settings-hint"),
                           form, body, title_key="fixed_assets.runs_title")

    @app.get("/fixed-assets/runs/{month}")
    async def run_page(request: Request, month: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            items = (await api.fixed_assets_get(token, "/runs"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.runs_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (t("fixed_assets.runs_title"), "/fixed-assets/runs"), (month, None)])
        record = next((r for r in items if r["month"] == month), None)
        if record is None:
            return await _page(request, crumbs, page_header(month),
                               empty_state_cta(t("fixed_assets.run_missing"), t("fixed_assets.runs_title"),
                                               "/fixed-assets/runs"),
                               title_key="fixed_assets.runs_title")
        summary = Table(Tbody(
            Tr(Th(t("fixed_assets.status")), Td(t(f"fixed_assets.run_status.{record['status']}"))),
            Tr(Th(t("label.total")), _money_td(record.get("total"), currency)),
            # The journal's source label in accounting reads the same: "Depreciation <month>".
            Tr(Th(t("fixed_assets.journal")),
               Td(*[A(f"{t('fixed_assets.movement.run')} {month}", href=f"/accounting?{urlencode({'q': j})}")
                    for j in record.get("journal_ids") or []] or [empty_mark()]))),
            cls="data-table detail-table")
        parts: list[FT] = [Div(summary, cls="detail-card section-card")]
        if record.get("exceptions"):
            parts.append(Div(H2(t("fixed_assets.held_title"), cls="section-title"),
                             _held_table(record["exceptions"], currency), cls="detail-card section-card"))
        if record["status"] in ("posted", "posted_with_exceptions"):
            parts.append(Div(H2(t("fixed_assets.reverse_run"), cls="section-title"),
                             _op_form("reverse-run", Input(type="hidden", name="month", value=month),
                                      P(t("fixed_assets.reverse_run_hint"), cls="settings-hint"),
                                      review_label=t("fixed_assets.reverse_run")),
                             cls="detail-card section-card"))
        return await _page(request, crumbs, page_header(t("fixed_assets.run_title", month=month)), *parts,
                           title_key="fixed_assets.runs_title")

    @app.get("/settings/fixed-assets")
    async def settings_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            setup, _ = await _context(token)
            if not setup["started"]:
                return RedirectResponse("/fixed-assets", status_code=302)
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
            chart = (await api.get_chart(token)).get("items") or []
        except APIError as e:
            return await _refused(request, e, "fixed_assets.settings_title")
        return await _page(request, page_header(t("fixed_assets.settings_title")), _settings_form(setup["settings"]),
                           _classes_card(classes, chart), title_key="fixed_assets.settings_title")

    @app.post("/settings/fixed-assets")
    async def settings_save(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        body = {k: str(form.get(k) or "").strip() or None
                for k in ("run_mode", "run_day", "convention", "threshold", "first_managed_period") if k in form}
        if (body.get("run_day") or "").isdigit():
            body["run_day"] = int(body["run_day"])
        try:
            await api.fixed_assets_send(token, "PATCH", "/settings", body)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))
        return Response(headers=toast_header(t("fixed_assets.saved")))

    async def _classes_fragment(token: str) -> FT:
        classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
        return _classes_card(classes, (await api.get_chart(token)).get("items") or [])

    @app.post("/settings/fixed-assets/classes")
    async def class_add(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        try:
            await api.fixed_assets_send(token, "POST", "/classes", {"name": t("fixed_assets.new_class"),
                                                                    "life_months": 60})
            return await _classes_fragment(token)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))

    @app.post("/settings/fixed-assets/classes/{class_id}/archive")
    async def class_archive(request: Request, class_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        try:
            await api.fixed_assets_send(token, "PATCH", f"/classes/{quote(class_id, safe=':')}",
                                        {"archived": str(form.get("archived")) == "1"})
            return await _classes_fragment(token)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))

    async def _class_and_chart(token: str, class_id: str) -> tuple[dict, list[dict]]:
        classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
        cls = next((c for c in classes if c["id"] == class_id), None)
        if cls is None:
            raise APIError(404, t("fixed_assets.error"))
        return cls, (await api.get_chart(token)).get("items") or []

    @app.get("/settings/fixed-assets/classes/{class_id}/{field}/edit")
    async def class_field_edit(request: Request, class_id: str, field: str):
        token = _token(request)
        if not token or field not in _CLASS_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            cls, chart = await _class_and_chart(token, class_id)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")
        url = f"/settings/fixed-assets/classes/{quote(class_id, safe=':')}/{field}"
        options, labels = None, None
        if field == "method":
            options, labels = _methods(), {m: t(f"fixed_assets.method.{m}") for m in _methods()}
        elif field.endswith("account"):
            options = _account_options(chart)
        return editable_cell(class_id, field, cls.get(field),
                             "select" if options is not None else _CLASS_EDITABLE[field], options=options,
                             label_map=labels, patch_url=url, restore_url=f"{url}/display",
                             aria_label=t(f"fixed_assets.{field}"), searchable=field.endswith("account"))

    @app.get("/settings/fixed-assets/classes/{class_id}/{field}/display")
    async def class_field_display(request: Request, class_id: str, field: str):
        token = _token(request)
        if not token or field not in _CLASS_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            cls, chart = await _class_and_chart(token, class_id)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")
        return _class_cell(cls, field, chart)

    @app.patch("/settings/fixed-assets/classes/{class_id}/{field}")
    async def class_field_patch(request: Request, class_id: str, field: str):
        token = _token(request)
        if not token or field not in _CLASS_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        raw = str((await request.form()).get("value", "")).strip()
        value: object = raw
        if field == "life_months":
            value = int(raw) if raw.isdigit() else (None if not raw else raw)
        try:
            cls = await api.fixed_assets_send(token, "PATCH", f"/classes/{quote(class_id, safe=':')}",
                                              {field: value})
        except APIError as e:
            return Td(P(_error_text(e), cls="cell-error"))
        return _class_cell(cls, field, [])

    log.info("celerp-fixed-assets: UI routes registered")
