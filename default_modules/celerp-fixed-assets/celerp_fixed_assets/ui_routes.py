# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fixed assets pages.

GET  /fixed-assets                      The register: start panel, or the asset list
GET  /fixed-assets/intake               Bill and journal lines not yet registered
GET  /fixed-assets/intake/line          What to do with one line
GET  /fixed-assets/assets/new           Add an asset by hand
GET  /fixed-assets/assets/{id}          One asset: details, figures, what holds it, sources, corrections, movements
GET  /fixed-assets/assets/{id}/correct  Correct an asset: depreciation settings, an error, or a source line
GET  /fixed-assets/assets/{id}/dispose  Dispose of an asset, in whole or part
GET  /fixed-assets/dispose?ids=         Dispose of the assets ticked on the register, each in whole
GET  /fixed-assets/assets/{id}/sell     Sell an asset through a draft invoice
GET  /fixed-assets/assets/{id}/remeasure  Revalue an asset, impair it, or reverse an impairment
GET  /fixed-assets/assets/{id}/add      Add an improvement or a replacement part to an asset
GET  /fixed-assets/assets/{id}/place-in-service  Move construction cost to the assets it built
GET  /fixed-assets/reviews              Impairment reviews due or overdue
GET  /fixed-assets/classes/{id}/revalue Revalue every asset in a class on one date
GET  /fixed-assets/corrections          Every correction newest first, and those waiting to be applied
GET  /fixed-assets/corrections/{id}     One correction: what it changed and posted; apply, withdraw or undo it
GET  /fixed-assets/classes/{id}/apply   Apply a class's depreciation settings to the assets on older ones
GET  /fixed-assets/reports/prior-period-adjustments[.csv]  Earlier years restated by corrections
GET  /fixed-assets/runs                 Depreciation runs: run a month, every month's run newest first
GET  /fixed-assets/runs/{month}         One month's run: its journal and the assets held out of it
GET  /settings/fixed-assets             Settings and asset classes

Every financial action is one form with two steps: Review shows the exact journal lines
and the assets it creates, Confirm does what was reviewed (POST /fixed-assets/op/...).
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date
from decimal import Decimal
from urllib.parse import quote, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.operation_key import FIELD as OP_KEY_FIELD, operation_key_input
from ui.components.shell import base_shell, page_header, page_title, toast_header
from ui.components.table import (EMPTY, breadcrumbs, bulk_toolbar, display_cell, editable_cell, empty_mark, empty_state_cta,
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


def _detail_table(rows: list[tuple[str, object]]) -> FT:
    """A label and value table; a value given as its own cell (money) keeps that cell."""
    return Table(Tbody(*[Tr(Th(label), value if getattr(value, "tag", None) == "td" else Td(value))
                         for label, value in rows]), cls="data-table detail-table")


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
    if "missing" in result:
        parts += _class_revaluation_preview(result, currency)
    elif assets:
        rows = [Tr(Td(a.get("number") or EMPTY), Td(_text(a.get("name"))),
                   _money_td(a.get("cost", a.get("accumulated_depreciation")), currency)) for a in assets]
        parts.append(Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                                    _money_th(t("fixed_assets.amount")))),
                           Tbody(*rows), cls="data-table"))
    if result.get("classification"):
        parts += _correction_preview(result, currency)
    if "gain_or_loss" in result:
        parts.append(_detail_table([(t(f"fixed_assets.{k}"), _money_td(result[k], currency))
                                    for k in ("depreciation", "carrying_amount", "gain_or_loss")]))
    if "new_carrying_amount" in result:
        parts.append(_remeasure_figures(result, currency))
    if "remainder" in result:
        parts.append(_detail_table([(t(f"fixed_assets.construction.{k}"), _money_td(result[k], currency))
                                    for k in ("balance", "remainder")]))
    if result.get("kind") in ("improvement", "replacement"):
        parts.append(_addition_figures(result, currency))
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


def _correction_preview(result: dict, currency: str) -> list[FT]:
    """How a correction is classified, when it takes effect or posts, what it restates in
    earlier years, the corrective movements, and the corrected charge ahead."""
    classification = result["classification"]
    when = (t("fixed_assets.effective_from", date=result["effective_date"]) if result.get("effective_date")
            else t("fixed_assets.posts_on", date=_text(result.get("posting_date"))))
    parts: list[FT] = [P(Strong(t(f"fixed_assets.classification.{classification}")), " ", when)]
    if classification != result.get("default_classification"):
        parts.append(P(t("fixed_assets.override_note", reason=_text(result.get("override_reason"))),
                       cls="form-hint"))
    if result.get("prior_years"):
        hint = (t("fixed_assets.prior_to_pl", reason=_text(result.get("immaterial_reason")))
                if result.get("treatment") == "profit_and_loss" else t("fixed_assets.prior_to_equity"))
        parts += [H4(t("fixed_assets.prior_years_title")), P(hint, cls="form-hint"), _restated_table(
            result["prior_years"], currency)]
    if result.get("movements"):
        parts += [H4(t("fixed_assets.movements")), Table(
            Thead(Tr(Th(t("fixed_assets.movement")), Th(t("fixed_assets.effective_date")),
                     Th(t("fixed_assets.posting_date")))),
            Tbody(*[Tr(Td(t(f"fixed_assets.movement.{m['operation']}")), Td(_text(m.get("effective_date"))),
                       Td(_text(m.get("posting_date")))) for m in result["movements"]]), cls="data-table")]
    if result.get("future"):
        parts += [H4(t("fixed_assets.future_title")), Table(
            Thead(Tr(Th(t("fixed_assets.month")), _money_th(t("fixed_assets.amount")))),
            Tbody(*[Tr(Td(f["month"]), _money_td(f["amount"], currency)) for f in result["future"]]),
            cls="data-table")]
    return parts


_REMEASURED = ("depreciation", "carrying_amount", "measured", "new_carrying_amount", "surplus", "profit_or_loss")


def _remeasure_figures(result: dict, currency: str) -> FT:
    """Depreciation brought up to the date, the carrying amount before and after, and where
    the difference goes; a reversal also shows the limit it stops at."""
    keys = _REMEASURED + tuple(k for k in ("reversal", "limit") if k in result)
    return _detail_table([(t(f"fixed_assets.remeasure.{k}"), _money_td(result[k], currency)) for k in keys])


def _class_revaluation_preview(result: dict, currency: str) -> list[FT]:
    """Each asset before and after, the class totals, and any asset still without a fair value."""
    parts: list[FT] = []
    if result["missing"]:
        parts.append(Div(t("fixed_assets.revalue_class_missing", numbers=", ".join(result["missing"])),
                         cls="flash flash--warning"))
    keys = ("carrying_amount", "measured", "surplus", "profit_or_loss")
    rows = [Tr(Td(a["number"]), Td(_text(a.get("name"))), *[_money_td(a[k], currency) for k in keys])
            for a in result["assets"]]
    totals = result["totals"]
    rows.append(Tr(Td(Strong(t("label.total"))), Td(""),
                   *[_money_td(totals[k], currency) for k in ("carrying_amount", "fair_value", "surplus",
                                                              "profit_or_loss")]))
    parts.append(Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                                *[_money_th(t(f"fixed_assets.remeasure.{k}")) for k in keys])),
                       Tbody(*rows), cls="data-table"))
    return parts


def _addition_figures(result: dict, currency: str) -> FT:
    """What is added, over what life, and for a replacement the part that leaves the books."""
    rows = [(t("fixed_assets.add_kind"), t(f"fixed_assets.add_kind.{result['kind']}")),
            (t("fixed_assets.date"), _text(result.get("date"))),
            (t("fixed_assets.amount"), _money_td(result.get("amount"), currency)),
            (t("fixed_assets.life_months"),
             _text(result.get("life_months")) if result.get("life_months") else t("fixed_assets.keep_end_date"))]
    if result["kind"] == "replacement":
        rows.insert(1, (t("fixed_assets.new_part"), f"{result['number_of_part']} {result['name']}"))
        gone = result["replaced"]
        rows += [(t("fixed_assets.replaced_part"), f"{gone['number']} {gone.get('name') or ''}".strip())]
        rows += [(t(f"fixed_assets.replaced.{k}"), _money_td(gone[k], currency))
                 for k in ("cost", "carrying_amount", "gain_or_loss")]
    return _detail_table(rows)


def _restated_table(years: list[dict], currency: str) -> FT:
    return Table(Thead(Tr(Th(t("fixed_assets.year")), _money_th(t("fixed_assets.reported")),
                          _money_th(t("fixed_assets.restated")))),
                 Tbody(*[Tr(Td(str(y["year"])), _money_td(y["reported"], currency), _money_td(y["restated"], currency))
                         for y in years]), cls="data-table")


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


def _q(form, name: str) -> str:
    return quote(str(form.get(name) or ""), safe=":")


def _month_start(form, name: str) -> str | None:
    month = str(form.get(name) or "").strip()
    return f"{month}-01" if month else None


def _settings_changes(form) -> dict:
    """The depreciation settings the user changed: each field against the value it showed."""
    changes: dict = {}
    for name in ("method", "life_months", "residual_percent", "convention"):
        value = str(form.get(name) or "").strip()
        if name in form and value != str(form.get(f"was:{name}") or ""):
            changes[name] = int(value) if name == "life_months" and value.isdigit() else (value or None)
    method = str(form.get("method") or form.get("was:method") or "")
    was = json.loads(str(form.get("was:params") or "{}"))
    prefix = f"params:{method}:"
    params = {k.removeprefix(prefix): str(v).strip() for k, v in form.multi_items()
              if k.startswith(prefix) and not k.endswith(":flag") and str(v).strip()}
    for flag in [k for k, _ in form.multi_items() if k.startswith(prefix) and k.endswith(":flag")]:
        name = flag.removeprefix(prefix).removesuffix(":flag")
        if str(form.get(f"{prefix}{name}") or "") == "true":
            params[name] = True
        elif was.get(name) is True:
            params[name] = False
        else:
            params.pop(name, None)
    if params != was or "method" in changes:
        changes["params"] = params
    return changes


def _correct_body(form) -> dict:
    """A correction from the Correct form: the one thing changed and how its effect posts."""
    field = str(form.get("field") or "")
    if field == "settings":
        changes = _settings_changes(form)
    elif field == "opening_values":
        changes = {field: {k: str(form.get(k)).strip() for k in ("cost", "accumulated_depreciation",
                                                                 "accumulated_impairment")
                           if str(form.get(k) or "").strip()}}
    elif field == "maintenance":
        changes = {field: {"expense_account": str(form.get("expense_account") or "")}}
    elif field == "replace_source":
        changes = {field: {"old": str(form.get("old") or ""), "new": str(form.get("new") or "")}}
    elif field in ("link_source", "release_source"):
        changes = {field: {"source": str(form.get("source") or "")}}
    else:
        changes = {field: str(form.get(field) or "").strip()} if field else {}
    body = {"changes": changes, "classification": str(form.get("classification") or "") or None,
            "override_reason": str(form.get("override_reason") or "").strip() or None,
            "effective_date": _month_start(form, "effective_month"),
            "posting_date": str(form.get("posting_date") or "") or None,
            "prior_years": str(form.get("prior_years") or "") or None,
            "immaterial_reason": str(form.get("immaterial_reason") or "").strip() or None}
    return {k: v for k, v in body.items() if v is not None}


def _dispose_body(form) -> dict:
    """A disposal from the Dispose form: when, why, and for one asset what share of it."""
    body = {"date": str(form.get("date") or ""), "reason": str(form.get("reason") or ""),
            "note": str(form.get("note") or "").strip() or None,
            "percent": _amount(form, "percent"), "quantity": _amount(form, "quantity")}
    ids = form.getlist("asset_ids")
    if ids:
        body["asset_ids"] = [str(i) for i in ids]
    return {k: v for k, v in body.items() if v is not None}


def _sell_body(form) -> dict:
    return {"date": str(form.get("date") or ""), "price": _amount(form, "price"),
            "contact_name": str(form.get("contact_name") or "").strip()}


def _sale_credit_body(form) -> dict:
    body = {"kind": str(form.get("kind") or ""), "date": str(form.get("date") or ""),
            "amount": _amount(form, "amount")}
    return {k: v for k, v in body.items() if v is not None}


def _remeasure_body(form) -> dict:
    body = {"date": str(form.get("date") or ""), "fair_value": _amount(form, "fair_value"),
            "recoverable_amount": _amount(form, "recoverable_amount"),
            "basis": str(form.get("basis") or "").strip() or None}
    return {k: v for k, v in body.items() if v is not None}


def _class_revalue_body(form) -> dict:
    """One row per asset given a fair value, with its own basis or the one shared by all."""
    shared = str(form.get("basis") or "").strip()
    rows = []
    for key, value in form.multi_items():
        if key.startswith("fair_value:") and str(value).strip():
            asset_id = key.removeprefix("fair_value:")
            rows.append({"asset_id": asset_id, "fair_value": str(value).strip(),
                         "basis": str(form.get(f"basis:{asset_id}") or "").strip() or shared or None})
    return {"date": str(form.get("date") or ""), "rows": rows}


def _add_body(form) -> dict:
    """An addition: what pays for it, and for a replacement part its name, life and the part it replaces."""
    kind = str(form.get("kind") or "")
    body = {"kind": kind, "date": str(form.get("date") or "") or None,
            "source": str(form.get("source") or "") or None, "life_months": _amount(form, "life_months")}
    if not body["source"]:
        body.update(amount=_amount(form, "amount"), credit_account=str(form.get("credit_account") or "") or None)
    if kind == "replacement":
        body["name"] = str(form.get("name") or "").strip()
        part = str(form.get("replaced") or "")
        body["replaced"] = {"asset_id": part} if part else {"estimate": _amount(form, "estimate")}
    return {k: v for k, v in body.items() if v is not None}


def _place_body(form) -> dict:
    """The allocation rows given a name or an amount; empty rows are left out."""
    rows: dict[str, dict] = {}
    for key, value in form.multi_items():
        if key.startswith("row:") and str(value).strip():
            _, n, name = key.split(":", 2)
            rows.setdefault(n, {})[name] = str(value).strip()
    allocations = [r for _, r in sorted(rows.items(), key=lambda kv: int(kv[0])) if r.get("name") or r.get("amount")]
    return {"date": str(form.get("date") or ""), "allocations": allocations}


def _asset_path(suffix: str):
    return lambda f: f"/assets/{_q(f, 'asset_id')}/{suffix}"


def _to_asset(r, f) -> str:
    return f"/fixed-assets/assets/{f.get('asset_id')}"


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
    "correct": (lambda f: f"/assets/{_q(f, 'asset_id')}/correct", _correct_body,
                lambda r, f: f"/fixed-assets/corrections/{r['correction']['id']}"),
    "apply-correction": (lambda f: f"/corrections/{_q(f, 'correction_id')}/apply",
                         lambda f: {"posting_date": str(f.get("posting_date") or "") or None},
                         lambda r, f: f"/fixed-assets/corrections/{f.get('correction_id')}"),
    "undo-correction": (lambda f: f"/corrections/{_q(f, 'correction_id')}/undo", lambda f: {},
                        lambda r, f: f"/fixed-assets/corrections/{f.get('correction_id')}"),
    "apply-class": (lambda f: f"/classes/{_q(f, 'class_id')}/apply-to-assets",
                    lambda f: {k: v for k, v in {"effective_date": _month_start(f, "effective_month")}.items() if v},
                    lambda r, f: "/fixed-assets/corrections"),
    "dispose": (lambda f: f"/assets/{_q(f, 'asset_id')}/dispose" if f.get("asset_id") else "/dispose",
                _dispose_body,
                lambda r, f: f"/fixed-assets/assets/{f.get('asset_id')}" if f.get("asset_id") else "/fixed-assets"),
    "undo-disposal": (lambda f: f"/assets/{_q(f, 'asset_id')}/undo-disposal", lambda f: {},
                      lambda r, f: f"/fixed-assets/assets/{f.get('asset_id')}"),
    "sell": (lambda f: f"/assets/{_q(f, 'asset_id')}/sell", _sell_body, lambda r, f: _doc_href(r["invoice_id"])),
    "sale-reissue": (lambda f: f"/assets/{_q(f, 'asset_id')}/sale/reissue", lambda f: {},
                     lambda r, f: _doc_href(r["invoice_id"])),
    "sale-cancel": (lambda f: f"/assets/{_q(f, 'asset_id')}/sale/cancel", lambda f: {},
                    lambda r, f: f"/fixed-assets/assets/{f.get('asset_id')}"),
    "sale-credit": (lambda f: f"/assets/{_q(f, 'asset_id')}/sale/credit", _sale_credit_body,
                    lambda r, f: _doc_href(r["credit_note_id"])),
    "revalue": (_asset_path("revalue"), _remeasure_body, _to_asset),
    "impair": (_asset_path("impair"), _remeasure_body, _to_asset),
    "reverse-impairment": (_asset_path("reverse-impairment"), _remeasure_body, _to_asset),
    "revalue-class": (lambda f: f"/classes/{_q(f, 'class_id')}/revalue", _class_revalue_body,
                      lambda r, f: "/settings/fixed-assets"),
    "add-to-asset": (_asset_path("add"), _add_body, lambda r, f: f"/fixed-assets/assets/{r['assets'][0]['id']}"),
    "place-in-service": (_asset_path("place-in-service"), _place_body, _to_asset),
    "undo-latest": (_asset_path("undo-latest"), lambda f: {}, _to_asset),
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


def _doc_href(doc_id: str) -> str:
    return f"/docs/{quote(doc_id, safe=':')}"


def _source_href(source: str) -> str:
    document = _source_document(source)
    if document.startswith(MANUAL_PREFIX):
        return f"/accounting?{urlencode({'q': document})}"
    return _doc_href(document)


def _class_names(classes: list[dict]) -> dict[str, str]:
    return {c["id"]: c["name"] for c in classes}


def _register_table(items: list[dict], classes: dict[str, str], currency: str) -> FT:
    """The asset list, each row ticked to dispose of several at once from the toolbar above it.
    Every row can be ticked: an asset that cannot be disposed of is refused at review with
    the reason, rather than its box going missing."""
    rows = [Tr(Td(Input(type="checkbox", cls="bulk-select", name="selected", value=a["id"]), cls="col-checkbox"),
               Td(A(a["number"], href=f"/fixed-assets/assets/{a['id']}")), Td(_text(a.get("name"))),
               Td(_text(classes.get(a.get("class_id")))), Td(_text(a.get("available_date"))),
               _money_td(a.get("cost"), currency), _money_td(a.get("accumulated_depreciation"), currency),
               _money_td(a.get("nbv"), currency), Td(t(f"fixed_assets.status.{a.get('status')}")), cls="data-row")
            for a in items]
    return Table(Thead(Tr(Th(Input(type="checkbox", cls="bulk-select-all", title=t("label.select_all")),
                             cls="col-checkbox"),
                          Th(t("fixed_assets.number")), Th(t("fixed_assets.name")), Th(t("fixed_assets.class")),
                          Th(t("fixed_assets.available_date")), _money_th(t("fixed_assets.cost")),
                          _money_th(t("fixed_assets.accumulated_depreciation")), _money_th(t("fixed_assets.nbv")),
                          Th(t("fixed_assets.status")))),
                 Tbody(*rows), cls="data-table", id="fa-register")


def _register_toolbar() -> FT:
    return bulk_toolbar("fa-register", [{"value": "dispose", "label": t("fixed_assets.dispose"), "method": "post",
                                         "url": "/fixed-assets/dispose/selected", "swap": "none"}])


def _disposal_fields(asset: dict | None) -> list[FT]:
    """When and why an asset leaves; for one asset, the share of it that does (blank for all)."""
    from celerp_fixed_assets.disposal import REASONS

    fields = [_field(t("fixed_assets.disposal_date"), _input("date", date.today().isoformat(), type_="date",
                                                             required=True)),
              _field(t("fixed_assets.reason"),
                     Select(*[Option(t(f"fixed_assets.disposal_reason.{r}"), value=r) for r in REASONS],
                            name="reason", cls="form-input")),
              _field(t("fixed_assets.note"), _input("note"), t("fixed_assets.disposal_note_hint"))]
    if asset is None:
        return fields
    if float(asset.get("quantity") or 1) > 1:
        share = _field(t("fixed_assets.quantity_disposed"),
                       _input("quantity", type_="number", step="any", min="0", max=str(asset["quantity"])),
                       t("fixed_assets.quantity_disposed_hint", quantity=asset["quantity"]))
    else:
        share = _field(t("fixed_assets.percent_disposed"), _input("percent", type_="number", step="any", min="0",
                                                                  max="100"), t("fixed_assets.percent_disposed_hint"))
    return [*fields, share]


def _disposal_card(asset: dict, movements: list[dict], currency: str) -> FT | str:
    """The latest disposal still standing, what it posted, and the form that undoes it. A sale
    is shown by its own card and undone through its invoice."""
    undone = {m.get("reverses") for m in movements}
    live = [m for m in movements if m.get("operation") == "disposal" and m["id"] not in undone]
    if not live or live[-1].get("reason") == "sold":
        return ""
    latest = live[-1]
    asset_id = quote(asset["id"], safe=":")
    rows = [(t("fixed_assets.disposal_date"), _text(latest.get("effective_date"))),
            (t("fixed_assets.reason"), t(f"fixed_assets.disposal_reason.{latest.get('reason')}")),
            (t("fixed_assets.note"), _text(latest.get("note"))),
            (t("fixed_assets.gain_or_loss"), _money_td(latest.get("gain_or_loss"), currency)),
            (t("fixed_assets.journal"), A(t("fixed_assets.journal"),
                                          href=f"/accounting?{urlencode({'q': latest['journal_id']})}")
             if latest.get("journal_id") else empty_mark())]
    undo = _op_form("undo-disposal", Input(type="hidden", name="asset_id", value=asset["id"]),
                    P(t("fixed_assets.undo_disposal_hint"), cls="settings-hint"),
                    review_label=t("fixed_assets.undo_disposal"))
    return Div(H2(t("fixed_assets.disposal_title"), cls="section-title"),
               _detail_table(rows), undo, cls="detail-card section-card", id=f"fa-disposal-{asset_id}")


def _sale_card(asset: dict, sale: dict, currency: str) -> FT | str:
    """Where the asset's sale stands: its invoice, what completing it posts or posted, its
    credit notes, and what can be done next."""
    if not sale.get("state"):
        return ""
    asset_id = asset["id"]
    hidden = Input(type="hidden", name="asset_id", value=asset_id)
    rows = [(t("fixed_assets.status"), t(f"fixed_assets.sale_state.{sale['state']}")),
            (t("fixed_assets.sale_invoice"), sale["invoice_number"] if sale.get("draft_deleted")
             else A(sale["invoice_number"], href=_doc_href(sale["invoice_id"]))),
            (t("fixed_assets.sale_date"), _text(sale.get("date"))),
            (t("fixed_assets.buyer"), _text(sale.get("contact_name"))),
            (t("fixed_assets.proceeds"), _money_td(sale.get("proceeds"), currency))]
    if sale["state"] != "pending":
        # Only a completed sale can be credited.
        rows += [(t("fixed_assets.credited"), _money_td(sale.get("credited"), currency)),
                 (t("fixed_assets.remaining"), _money_td(sale.get("remaining"), currency))]
    rows += [(t("fixed_assets.sale_carrying_amount"), _money_td(sale.get("carrying_amount"), currency)),
             (t("fixed_assets.gain_or_loss"), _money_td(sale.get("gain_or_loss"), currency))]
    parts: list[FT] = [H2(t("fixed_assets.sale_title"), cls="section-title")]
    if sale.get("draft_deleted"):
        parts.append(Div(Strong(t("fixed_assets.sale_draft_deleted")), " ", t("fixed_assets.sale_draft_deleted_hint"),
                         cls="flash flash--warning"))
    elif sale["state"] == "pending":
        parts.append(P(t("fixed_assets.sale_pending_hint", invoice=sale["invoice_number"]), cls="settings-hint"))
    if sale.get("problem"):
        parts.append(Div(sale["problem"], cls="flash flash--error"))
    parts.append(_detail_table(rows))
    if (sale.get("journal") or {}).get("lines"):
        parts += [H4(t("fixed_assets.sale_journal_" + ("pending" if sale["state"] == "pending" else "posted"))),
                  _lines_table(sale["journal"]["lines"], currency)]
    notes = sale.get("credit_notes") or []
    if notes:
        parts += [H4(t("fixed_assets.credit_notes")), Table(
            Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.date")), Th(t("fixed_assets.credit_kind")),
                     _money_th(t("fixed_assets.amount")))),
            Tbody(*[Tr(Td(A(n["number"], href=_doc_href(n["id"]))), Td(_text(n.get("date"))),
                       Td(t(f"fixed_assets.credit_kind.{n['kind']}")), _money_td(n.get("amount"), currency))
                    for n in sorted(notes, key=lambda n: n.get("date") or "", reverse=True)]),
            cls="data-table")]
    actions = sale.get("actions") or []
    if "reissue" in actions:
        parts.append(_op_form("sale-reissue", hidden, P(t("fixed_assets.reissue_hint"), cls="settings-hint"),
                              review_label=t("fixed_assets.reissue")))
    if "cancel" in actions:
        parts.append(_op_form("sale-cancel", hidden, P(t("fixed_assets.cancel_sale_hint"), cls="settings-hint"),
                              review_label=t("fixed_assets.cancel_sale")))
    if "credit" in actions:
        parts.append(_op_form(
            "sale-credit", hidden, H4(t("fixed_assets.credit_sale")),
            P(t("fixed_assets.credit_sale_hint"), cls="settings-hint"),
            _field(t("fixed_assets.credit_kind"),
                   Select(*[Option(t(f"fixed_assets.credit_kind.{k}"), value=k) for k in ("adjust", "returned")],
                          name="kind", cls="form-input")),
            _field(t("fixed_assets.date"), _input("date", date.today().isoformat(), type_="date", required=True)),
            _field(t("fixed_assets.amount"), _input("amount", type_="number", step="any", min="0"),
                   t("fixed_assets.credit_amount_hint")),
            review_label=t("fixed_assets.credit_sale")))
    return Div(*parts, cls="detail-card section-card", id=f"fa-sale-{quote(asset_id, safe=':')}")


def doc_sale_actions(doc: dict) -> FT | None:
    """The doc_detail_actions slot: an invoice or credit note carrying an asset line links to
    that asset's sale, where every change after finalize is made."""
    from celerp.services.doc_asset_lines import ASSET_DOC_TYPES, asset_lines

    if doc.get("doc_type") not in ASSET_DOC_TYPES:
        return None
    links = []
    for li in asset_lines(doc):
        asset_id = quote(li["asset_id"], safe=":")
        links.append(A(t("fixed_assets.correct_sale"), href=f"/fixed-assets/assets/{asset_id}#fa-sale-{asset_id}",
                       cls="btn btn--secondary btn--sm", title=_text(li.get("description"))))
    return Span(*links) if links else None


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
               Td(Div(*([] if c.get("archived") else [
                         A(t("fixed_assets.apply_to_assets"), cls="btn btn--sm btn--secondary",
                           href=f"/fixed-assets/classes/{quote(c['id'], safe=':')}/apply"),
                         *([] if c.get("construction") else [
                             A(t("fixed_assets.revalue_class" if c.get("measurement_policy") == "revaluation"
                                 else "fixed_assets.change_to_revaluation"), cls="btn btn--sm btn--secondary",
                               href=f"/fixed-assets/classes/{quote(c['id'], safe=':')}/revalue")])]),
                      Button(t("fixed_assets.restore") if c.get("archived") else t("fixed_assets.archive"),
                             type="button", cls="btn btn--sm btn--secondary",
                             hx_post=f"/settings/fixed-assets/classes/{quote(c['id'], safe=':')}/archive",
                             hx_vals=f'{{"archived": "{"0" if c.get("archived") else "1"}"}}',
                             hx_target="#fa-classes", hx_swap="outerHTML"), cls="flex-row")),
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
    """Each movement's change to cost and accumulated depreciation, and to accumulated
    impairment once any movement has touched it."""
    if not items:
        return P(t("fixed_assets.no_movements"), cls="form-hint")
    columns = ["cost", "accumulated_depreciation"]
    if any(Decimal(str((m.get("amounts") or {}).get("accumulated_impairment") or 0)) for m in items):
        columns.append("accumulated_impairment")
    rows = [Tr(Td(_text(m.get("posting_date"))), Td(_text(m.get("effective_date"))),
               Td(t(f"fixed_assets.movement.{m.get('operation')}")),
               *[_money_td((m.get("amounts") or {}).get(c) if m.get("amounts") is not None else None, currency)
                 for c in columns])
            for m in items]
    return Table(Thead(Tr(Th(t("fixed_assets.posting_date")), Th(t("fixed_assets.effective_date")),
                          Th(t("fixed_assets.movement")), *[_money_th(t(f"fixed_assets.{c}")) for c in columns])),
                 Tbody(*rows), cls="data-table")


def _asset_cell(asset: dict, field: str) -> FT:
    return display_cell(asset["id"], field, asset.get(field), "text",
                        edit_url=f"/fixed-assets/assets/{quote(asset['id'], safe=':')}/field/{field}/edit")


def _details_card(asset: dict) -> FT:
    return Div(
        H2(t("fixed_assets.details"), cls="section-title"),
        _detail_table([(t(f"fixed_assets.{f}"), _asset_cell(asset, f)) for f in DESCRIPTIVE_FIELDS]),
        cls="detail-card section-card", id="fa-details",
    )


def _figures_card(asset: dict, class_name: str, currency: str) -> FT:
    rows = [(t("fixed_assets.class"), _text(class_name)), (t("fixed_assets.status"),
                                                          t(f"fixed_assets.status.{asset.get('status')}")),
            (t("fixed_assets.quantity"), _text(asset.get("quantity"))),
            (t("fixed_assets.available_date"), _text(asset.get("available_date")))]
    if asset.get("review_due"):
        rows.append((t("fixed_assets.next_review"), asset["review_due"]))
    money = [("cost", "fixed_assets.cost"), ("accumulated_depreciation", "fixed_assets.accumulated_depreciation"),
             ("accumulated_impairment", "fixed_assets.accumulated_impairment"), ("nbv", "fixed_assets.nbv")]
    if float(asset.get("revaluation_surplus") or 0):
        money.append(("revaluation_surplus", "fixed_assets.revaluation_surplus"))
    return Div(
        H2(t("fixed_assets.figures"), cls="section-title"),
        _detail_table(rows + [(t(key), _money_td(asset.get(field), currency)) for field, key in money]),
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


def _components_card(asset: dict, components: list[dict], currency: str) -> FT:
    """The parts registered as components of an asset, each depreciated on its own."""
    rows = [Tr(Td(A(c["number"], href=f"/fixed-assets/assets/{c['id']}")), Td(_text(c.get("name"))),
               Td(t(f"fixed_assets.status.{c.get('status')}")), _money_td(c.get("cost"), currency),
               _money_td(c.get("nbv"), currency)) for c in components]
    return Div(H2(t("fixed_assets.components_title"), cls="section-title"),
               P(t("fixed_assets.components_hint"), cls="settings-hint"),
               Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")), Th(t("fixed_assets.status")),
                              _money_th(t("fixed_assets.cost")), _money_th(t("fixed_assets.nbv")))),
                     Tbody(*rows), cls="data-table"), cls="detail-card section-card")


def _reviews_card(asset: dict, reviews: list[dict], currency: str) -> FT:
    """An indefinite-life intangible's annual impairment reviews, newest first, the latest
    withdrawable, and the form that records the next."""
    from celerp_fixed_assets.remeasure import REVIEW_OUTCOMES

    asset_id = quote(asset["id"], safe=":")
    rows = [Tr(Td(r["date"]), Td(t(f"fixed_assets.review_outcome.{r['outcome']}")),
               _money_td(r.get("recoverable_amount"), currency), Td(_text(r.get("basis"))),
               Td(Button(t("fixed_assets.withdraw_review"), type="button", cls="btn btn--sm btn--secondary",
                         hx_post=f"/fixed-assets/assets/{asset_id}/reviews/{quote(r['id'], safe=':')}/withdraw",
                         hx_swap="none") if i == 0 else ""))
            for i, r in enumerate(reviews)]
    table = Table(Thead(Tr(Th(t("fixed_assets.date")), Th(t("fixed_assets.review_found")),
                           _money_th(t("fixed_assets.recoverable_amount")), Th(t("fixed_assets.basis")), Th(""))),
                  Tbody(*rows), cls="data-table") if rows else P(t("fixed_assets.no_reviews"), cls="form-hint")
    form = Form(_field(t("fixed_assets.date"), _input("date", date.today().isoformat(), type_="date", required=True)),
                _field(t("fixed_assets.review_found"),
                       Select(*[Option(t(f"fixed_assets.review_outcome.{o}"), value=o) for o in REVIEW_OUTCOMES],
                              name="outcome", cls="form-input")),
                _field(t("fixed_assets.recoverable_amount"),
                       _input("recoverable_amount", type_="number", step="0.01", min="0"),
                       t("fixed_assets.recoverable_amount_hint")),
                _field(t("fixed_assets.basis"), _input("basis", required=True)),
                Div(Button(t("fixed_assets.record_review"), type="submit", cls="btn btn--secondary"),
                    cls="form-actions"),
                hx_post=f"/fixed-assets/assets/{asset_id}/reviews", hx_swap="none", cls="flex-row")
    due = t("fixed_assets.review_due", date=asset["review_due"])
    return Div(H2(t("fixed_assets.reviews_title"), cls="section-title"),
               P(t("fixed_assets.reviews_hint"), " ", Strong(due), cls="settings-hint"), table, form,
               cls="detail-card section-card", id="fa-reviews")


def _undo_latest_card(asset: dict) -> FT:
    change = t(f"fixed_assets.movement.{asset['undoable']}")
    return Div(H2(t("fixed_assets.undo_latest"), cls="section-title"),
               _op_form("undo-latest", Input(type="hidden", name="asset_id", value=asset["id"]),
                        P(t("fixed_assets.undo_latest_hint", change=change), cls="settings-hint"),
                        review_label=t("fixed_assets.undo_latest")),
               cls="detail-card section-card")


def _attention_card(items: list[dict]) -> FT:
    return Div(H3(t("fixed_assets.attention_title")), P(t("fixed_assets.attention_hint")),
               Ul(*[Li(A(f"{a['number']} {a.get('name') or ''}".strip(), href=f"/fixed-assets/assets/{a['asset_id']}"),
                       " ", t(f"fixed_assets.action.{a['exception']['action']}")) for a in items]),
               cls="flash flash--warning", id="fa-attention")


def _reviews_due_card(items: list[dict]) -> FT:
    return Div(H3(t("fixed_assets.reviews_due_title")),
               P(t("fixed_assets.reviews_due_count", count=len(items)), " ",
                 A(t("fixed_assets.reviews_open"), href="/fixed-assets/reviews")),
               cls="flash flash--warning", id="fa-reviews-due")


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


def _tabs(base: str, param: str, current: str, choices, label_prefix: str) -> FT:
    """One button per choice, the current one highlighted, each passing its choice in the URL."""
    return Div(*[A(t(f"{label_prefix}.{c}"), href=f"{base}?{urlencode({param: c})}",
                   cls="btn btn--sm " + ("btn--primary" if c == current else "btn--secondary")) for c in choices],
               cls="flex-row")


def _kind_tabs(base: str, kind: str, kinds: tuple[str, ...]) -> FT:
    return _tabs(base, "kind", kind, kinds, "fixed_assets.kind")


# ── Corrections ──────────────────────────────────────────────────────────────

_PRIOR_TREATMENTS = ("retained_earnings", "profit_and_loss")


def _correction_what(record: dict) -> str:
    return ", ".join(t(f"fixed_assets.correct_field.{k}") for k in record.get("changes") or {}) or EMPTY


def _correction_day(record: dict) -> str:
    return _text((record.get("applied_at") or record.get("requested_at") or "")[:10])


def _corrections_table(items: list[dict], *, with_asset: bool = True) -> FT:
    """Corrections newest first, each opening its own page."""
    rows = [Tr(Td(_correction_day(r)),
               *([Td(A(r.get("number") or EMPTY, href=f"/fixed-assets/assets/{r['asset_id']}"))] if with_asset else []),
               Td(A(_correction_what(r), href=f"/fixed-assets/corrections/{r['id']}")),
               Td(t(f"fixed_assets.classification.{r['classification']}") if r.get("classification") else EMPTY),
               Td(t(f"fixed_assets.correction_state.{r.get('state')}")))
            for r in items]
    return Table(Thead(Tr(Th(t("fixed_assets.date")), *([Th(t("fixed_assets.asset"))] if with_asset else []),
                          Th(t("fixed_assets.correction")), Th(t("fixed_assets.classification")),
                          Th(t("fixed_assets.status")))),
                 Tbody(*rows), cls="data-table", id="fa-corrections")


def _corrections_card(asset: dict, items: list[dict]) -> FT:
    correct = (A(t("fixed_assets.correct"), href=f"/fixed-assets/assets/{asset['id']}/correct",
                 cls="btn btn--sm btn--primary") if asset.get("status") == "active" else "")
    return Div(Div(H2(t("fixed_assets.corrections_title"), cls="section-title"), correct,
                   cls="flex-row flex-between settings-card-header"),
               P(t("fixed_assets.corrections_card_hint"), cls="settings-hint"),
               _corrections_table(items, with_asset=False) if items
               else P(t("fixed_assets.no_corrections"), cls="form-hint"),
               cls="detail-card section-card")


def _param_inputs(policy: dict) -> list[FT]:
    """Each method's settings, filled from the asset's own where its method is that one."""
    from celerp_fixed_assets.calc.rules import METHODS

    groups = []
    for key, method in METHODS.items():
        if not method.params_schema:
            continue
        current = (policy.get("params") or {}) if policy.get("method") == key else {}
        inputs = []
        for name, kind in method.params_schema.items():
            field = f"params:{key}:{name}"
            if kind == "bool":
                inputs += [Input(type="hidden", name=f"{field}:flag", value="1"),
                           Label(Input(type="checkbox", name=field, value="true", checked=current.get(name) is True),
                                 t(f"fixed_assets.param.{name}"), cls="settings-toggle")]
            else:
                inputs.append(_field(t(f"fixed_assets.param.{name}"),
                                     _input(field, current.get(name), type_="number", step="any", min="0")))
        groups.append(Fieldset(Legend(t("fixed_assets.params_for", method=t(f"fixed_assets.method.{key}"))),
                               *inputs, cls="form-fieldset"))
    return groups


def _settings_fields(policy: dict) -> list[FT]:
    from celerp_fixed_assets.calc.rules import CONVENTIONS

    def select(name: str, values, label_prefix: str) -> FT:
        return Select(*[Option(t(f"{label_prefix}.{v}"), value=v, selected=v == policy.get(name)) for v in values],
                      name=name, cls="form-input", aria_label=t(f"fixed_assets.{name}"))

    was = [Input(type="hidden", name=f"was:{k}", value=_blank(policy.get(k)))
           for k in ("method", "life_months", "residual_percent", "convention")]
    return [*was, Input(type="hidden", name="was:params", value=json.dumps(policy.get("params") or {})),
            _field(t("fixed_assets.method"), select("method", _methods(), "fixed_assets.method")),
            _field(t("fixed_assets.life_months"), _input("life_months", policy.get("life_months"), type_="number",
                                                        step="1", min="1")),
            _field(t("fixed_assets.residual_percent"), _input("residual_percent", policy.get("residual_percent"),
                                                             type_="number", step="0.01", min="0", max="100")),
            _field(t("fixed_assets.convention"), select("convention", CONVENTIONS, "fixed_assets.convention")),
            *_param_inputs(policy),
            _field(t("fixed_assets.effective_month"), _input("effective_month", type_="month"),
                   t("fixed_assets.effective_month_hint")),
            _field(t("fixed_assets.classification"),
                   Select(Option(t("fixed_assets.classification.estimate"), value="", selected=True),
                          Option(t("fixed_assets.classification.error"), value="error"),
                          name="classification", cls="form-input", aria_label=t("fixed_assets.classification")),
                   t("fixed_assets.classification_hint")),
            _field(t("fixed_assets.override_reason"), _input("override_reason"),
                   t("fixed_assets.override_reason_hint"))]


def _blank(value) -> str:
    return "" if value is None else str(value)


def _posting_fields() -> list[FT]:
    """How an error correction posts: its day, and where earlier years' effect goes."""
    return [_field(t("fixed_assets.posting_date"), _input("posting_date", type_="date"),
                   t("fixed_assets.correction_posting_hint")),
            _field(t("fixed_assets.prior_years"), Select(
                *[Option(t(f"fixed_assets.prior_years.{p}"), value=p, selected=p == "retained_earnings")
                  for p in _PRIOR_TREATMENTS], name="prior_years", cls="form-input",
                aria_label=t("fixed_assets.prior_years"))),
            _field(t("fixed_assets.immaterial_reason"), _input("immaterial_reason"),
                   t("fixed_assets.immaterial_reason_hint"))]


def _source_options(items: list[dict], currency: str) -> list[tuple[str, str]]:
    return [(s["source"], " ".join(x for x in (s.get("document_ref") or s.get("ref") or _source_document(s["source"]),
                                               s.get("description") or "",
                                               fmt_money(float(s["amount"]), currency) if s.get("amount") else "")
                                   if x)) for s in items]


def _error_fields(field: str, asset: dict, offer: dict, classes: list[dict], chart: list[dict],
                  intake: list[dict], currency: str) -> list[FT]:
    held = asset.get("sources") or []
    if field == "cost":
        controls = [_field(t("fixed_assets.cost"), _input("cost", asset.get("cost"), type_="number", step="0.01",
                                                          min="0"))]
    elif field == "available_date":
        controls = [_field(t("fixed_assets.available_date"), _input("available_date", asset.get("available_date"),
                                                                    type_="date"))]
    elif field == "class_id":
        controls = [_field(t("fixed_assets.class"), _class_select(classes, asset.get("class_id") or ""))]
    elif field == "opening_values":
        controls = [_field(t(f"fixed_assets.{k}"), _input(k, asset.get(k), type_="number", step="0.01", min="0"))
                    for k in ("cost", "accumulated_depreciation", "accumulated_impairment")]
    elif field == "maintenance":
        controls = [P(t("fixed_assets.maintenance_hint"), cls="settings-hint"),
                    _field(t("fixed_assets.expense_account"), _picker("expense_account", _account_options(chart),
                                                                      label=t("fixed_assets.expense_account")))]
    elif field == "replace_source":
        controls = [_field(t("fixed_assets.old_source"), _picker(
                        "old", _source_options([s for s in held if not s.get("linked")], currency),
                        label=t("fixed_assets.old_source"))),
                    _field(t("fixed_assets.new_source"), _picker(
                        "new", _source_options([ln for ln in intake if ln["kind"] == "acquisition"], currency),
                        label=t("fixed_assets.new_source")))]
    elif field == "link_source":
        controls = [_field(t("fixed_assets.source_line"), _picker(
            "source", _source_options(offer.get("sources") or [], currency), label=t("fixed_assets.source_line")),
            t("fixed_assets.link_source_hint"))]
    else:
        controls = [_field(t("fixed_assets.source_line"), _picker(
            "source", _source_options([s for s in held if s.get("linked")], currency),
            label=t("fixed_assets.source_line")))]
    return [*controls, *_posting_fields()]


def _report_table(report: dict, currency: str) -> list[FT]:
    """The prior-period adjustments for one year: the year as reported and restated, the
    corrections restating it, and the ones posted to current profit or loss instead."""
    parts: list[FT] = []
    if report["years"]:
        parts += [H2(t("fixed_assets.restated_title"), cls="section-title"), _restated_table(report["years"], currency)]
    if report["items"]:
        parts += [H2(t("fixed_assets.ppa_corrections"), cls="section-title"), Table(
            Thead(Tr(Th(t("fixed_assets.posting_date")), Th(t("fixed_assets.asset")), Th(t("fixed_assets.years")),
                     Th(t("fixed_assets.correction")))),
            Tbody(*[Tr(Td(_text(i.get("posting_date"))),
                       Td(A(i.get("number") or EMPTY, href=f"/fixed-assets/assets/{i['asset_id']}")),
                       Td(", ".join(str(y) for y in i.get("years") or []) or EMPTY),
                       Td(A(t("fixed_assets.open"), href=f"/fixed-assets/corrections/{i['id']}")))
                    for i in reversed(report["items"])]), cls="data-table")]
    if report["immaterial"]:
        parts += [H2(t("fixed_assets.ppa_immaterial"), cls="section-title"),
                  P(t("fixed_assets.ppa_immaterial_hint"), cls="settings-hint"), Table(
            Thead(Tr(Th(t("fixed_assets.posting_date")), Th(t("fixed_assets.asset")), Th(t("fixed_assets.years")),
                     Th(t("fixed_assets.reason")))),
            Tbody(*[Tr(Td(_text(i.get("posting_date"))),
                       Td(A(i.get("number") or EMPTY, href=f"/fixed-assets/assets/{i['asset_id']}")),
                       Td(", ".join(str(y) for y in i.get("years") or []) or EMPTY), Td(_text(i.get("reason"))))
                    for i in reversed(report["immaterial"])]), cls="data-table")]
    return parts or [empty_state_cta(t("fixed_assets.ppa_empty", year=report["year"]))]


def _report_csv(report: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "year", "asset", "posting_date", "reported", "restated", "reason"])
    for y in report["years"]:
        w.writerow(["restated", y["year"], "", "", y["reported"], y["restated"], ""])
    for i in report["items"]:
        w.writerow(["correction", ";".join(str(y) for y in i.get("years") or []), i.get("number") or "",
                    i.get("posting_date") or "", "", "", ""])
    for i in report["immaterial"]:
        w.writerow(["immaterial", ";".join(str(y) for y in i.get("years") or []), i.get("number") or "",
                    i.get("posting_date") or "", "", "", i.get("reason") or ""])
    return out.getvalue()


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
            due = (await api.fixed_assets_get(token, "/reviews"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.title")
        actions = (A(t("fixed_assets.corrections_title"), href="/fixed-assets/corrections", cls="btn btn--secondary"),
                   A(t("fixed_assets.runs_title"), href="/fixed-assets/runs", cls="btn btn--secondary"),
                   A(t("fixed_assets.register_from_bills"), href="/fixed-assets/intake", cls="btn btn--secondary"),
                   A(t("fixed_assets.add_asset"), href="/fixed-assets/assets/new", cls="btn btn--primary"))
        if not found["items"] and not params:
            body = Div(empty_state_cta(t("fixed_assets.empty"), t("fixed_assets.register_from_bills"),
                                       "/fixed-assets/intake", hint=t("fixed_assets.empty_hint")))
        elif not found["items"]:
            body = P(t("fixed_assets.no_match"), cls="form-hint")
        else:
            body = Div(_register_toolbar(), _register_table(found["items"], _class_names(classes), currency),
                       pagination(page, found["total"], per_page, "/fixed-assets", urlencode(params)))
        return await _page(request, page_header(t("fixed_assets.title"), *actions),
                           P(t("fixed_assets.register_hint"), cls="settings-hint"),
                           _attention_card(held) if held else "", _reviews_due_card(due) if due else "",
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
            fixes = (await api.fixed_assets_get(token, "/corrections", {"asset_id": asset_id}))["items"]
            sale = (await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/sale")
                    if asset.get("sale") else {})
            components = (await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/components"))["items"]
            reviews = ((await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/reviews"))["items"]
                       if asset.get("review_due") else [])
            whole = (await api.fixed_assets_get(token, f"/assets/{quote(asset['parent_id'], safe=':')}")
                     if asset.get("parent_id") else None)
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
        if asset.get("review_due"):
            extra.append(_reviews_card(asset, reviews, currency))
        if components:
            extra.append(_components_card(asset, components, currency))
        if asset.get("status") == "active" or fixes:
            extra.append(_corrections_card(asset, fixes))
        if asset.get("undoable"):
            extra.append(_undo_latest_card(asset))
        title = f"{asset['number']} {asset.get('name') or ''}".strip()
        actions: tuple = ()
        if asset.get("status") == "active" and sale.get("state") != "pending":
            base = f"/fixed-assets/assets/{asset_id}"
            actions = ((A(t("fixed_assets.place_in_service"), href=f"{base}/place-in-service", cls="btn btn--primary"),)
                       if asset.get("under_construction") else
                       (A(t("fixed_assets.remeasure"), href=f"{base}/remeasure", cls="btn btn--secondary"),
                        A(t("fixed_assets.sell"), href=f"{base}/sell", cls="btn btn--secondary"),
                        A(t("fixed_assets.dispose"), href=f"{base}/dispose", cls="btn btn--secondary"))) + (
                A(t("fixed_assets.add_to_asset"), href=f"{base}/add", cls="btn btn--secondary"),)
        parent = (P(t("fixed_assets.component_of"), " ",
                    A(f"{whole['number']} {whole.get('name') or ''}".strip(), href=f"/fixed-assets/assets/{whole['id']}"),
                    cls="settings-hint") if whole else "")
        return await _page(request, crumbs, page_header(title, *actions), parent,
                           _exception_card(asset, currency) if asset.get("exception") else "",
                           _sale_card(asset, sale, currency),
                           _disposal_card(asset, movements, currency),
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
        summary = _detail_table([
            (t("fixed_assets.status"), t(f"fixed_assets.run_status.{record['status']}")),
            (t("label.total"), _money_td(record.get("total"), currency)),
            # The journal's source label in accounting reads the same: "Depreciation <month>".
            (t("fixed_assets.journal"),
             Td(*[A(f"{t('fixed_assets.movement.run')} {month}", href=f"/accounting?{urlencode({'q': j})}")
                  for j in record.get("journal_ids") or []] or [empty_mark()]))])
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

    @app.get("/fixed-assets/assets/{asset_id}/correct")
    async def correct_page(request: Request, asset_id: str, field: str = "settings"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        path = f"/assets/{quote(asset_id, safe=':')}"
        try:
            _, currency = await _context(token)
            asset = await api.fixed_assets_get(token, path)
            offer = await api.fixed_assets_get(token, f"{path}/correct-fields")
            classes = (await api.fixed_assets_get(token, "/classes"))["items"]
            choices = ["settings", *[f["field"] for f in offer["fields"] if f["classification"] == "error"],
                       *[a["action"] for a in offer["source_actions"]]]
            field = field if field in choices else "settings"
            chart = (await api.get_chart(token)).get("items") or [] if field == "maintenance" else []
            intake = (await api.fixed_assets_get(token, "/intake"))["items"] if field == "replace_source" else []
        except APIError as e:
            return await _refused(request, e, "fixed_assets.correct")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (asset["number"], f"/fixed-assets/assets/{asset_id}"), (t("fixed_assets.correct"), None)])
        tabs = _tabs(f"/fixed-assets/assets/{asset_id}/correct", "field", field, choices, "fixed_assets.correct_field")
        if field == "settings":
            fields = [P(t("fixed_assets.correct_hint.estimate"), cls="settings-hint"), *_settings_fields(asset["policy"])]
        else:
            fields = [P(t("fixed_assets.correct_hint.error"), cls="settings-hint"),
                      *_error_fields(field, asset, offer, classes, chart, intake, currency)]
        ask = Fieldset(Legend(t("fixed_assets.request_title")), P(t("fixed_assets.request_hint"), cls="form-hint"),
                       _field(t("fixed_assets.request_note"), _input("reason")),
                       Button(t("fixed_assets.request_correction"), type="button", cls="btn btn--secondary",
                              hx_post=f"/fixed-assets/assets/{asset_id}/request-correction",
                              hx_include="closest form", hx_swap="none"), cls="form-fieldset")
        form = _op_form("correct", Input(type="hidden", name="asset_id", value=asset_id),
                        Input(type="hidden", name="field", value=field), *fields, ask)
        title = t("fixed_assets.correct_title", number=asset["number"])
        return await _page(request, crumbs, page_header(title), tabs, form, title_key="fixed_assets.correct")

    @app.get("/fixed-assets/assets/{asset_id}/dispose")
    async def dispose_page(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
        except APIError as e:
            return await _refused(request, e, "fixed_assets.dispose")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (asset["number"], f"/fixed-assets/assets/{asset_id}"), (t("fixed_assets.dispose"), None)])
        form = _op_form("dispose", Input(type="hidden", name="asset_id", value=asset_id),
                        P(t("fixed_assets.dispose_hint"), cls="settings-hint"), *_disposal_fields(asset))
        return await _page(request, crumbs, page_header(t("fixed_assets.dispose_title", number=asset["number"])),
                           form, title_key="fixed_assets.dispose")

    @app.get("/fixed-assets/assets/{asset_id}/sell")
    async def sell_page(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
        except APIError as e:
            return await _refused(request, e, "fixed_assets.sell")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (asset["number"], f"/fixed-assets/assets/{asset_id}"), (t("fixed_assets.sell"), None)])
        form = _op_form("sell", Input(type="hidden", name="asset_id", value=asset_id),
                        P(t("fixed_assets.sell_hint"), cls="settings-hint"),
                        _field(t("fixed_assets.sale_date"), _input("date", date.today().isoformat(), type_="date",
                                                                   required=True)),
                        _field(t("fixed_assets.sale_price"), _input("price", type_="number", step="any", min="0",
                                                                    required=True), t("fixed_assets.sale_price_hint")),
                        _field(t("fixed_assets.buyer"), _input("contact_name", required=True)))
        return await _page(request, crumbs, page_header(t("fixed_assets.sell_title", number=asset["number"])),
                           form, title_key="fixed_assets.sell")

    async def _asset_crumbs(token: str, asset_id: str, label_key: str) -> tuple[dict, FT]:
        asset = await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}")
        return asset, breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                                   (asset["number"], f"/fixed-assets/assets/{asset_id}"), (t(label_key), None)])

    @app.get("/fixed-assets/assets/{asset_id}/remeasure")
    async def remeasure_page(request: Request, asset_id: str, op: str = "revaluation", value: str = "",
                             basis: str = ""):
        """Revalue, impair or reverse an impairment; a review that found a loss arrives here
        with its date, recoverable amount and basis filled in."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        ops = {"revaluation": "revalue", "impairment": "impair", "impairment_reversal": "reverse-impairment"}
        op = op if op in ops else "revaluation"
        try:
            await _context(token)
            asset, crumbs = await _asset_crumbs(token, asset_id, "fixed_assets.remeasure")
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.remeasure")
        cls = next((c for c in classes if c["id"] == asset.get("class_id")), {})
        field = "fair_value" if op == "revaluation" else "recoverable_amount"
        notes: list[FT] = [P(t(f"fixed_assets.remeasure_hint.{op}"), cls="settings-hint")]
        if op == "revaluation" and cls.get("measurement_policy") != "revaluation":
            notes.append(Div(t("fixed_assets.cost_model_hint"), " ",
                             A(t("fixed_assets.change_to_revaluation"),
                               href=f"/fixed-assets/classes/{quote(cls.get('id') or '', safe=':')}/revalue",
                               cls="btn btn--sm btn--secondary"), cls="flash flash--warning"))
        form = _op_form(ops[op], Input(type="hidden", name="asset_id", value=asset_id), *notes,
                        _field(t("fixed_assets.measurement_date"),
                               _input("date", request.query_params.get("date") or date.today().isoformat(), type_="date",
                                      required=True),
                               t("fixed_assets.measurement_date_hint")),
                        _field(t(f"fixed_assets.{field}"), _input(field, value, type_="number", step="0.01", min="0",
                                                                  required=True)),
                        _field(t("fixed_assets.basis"), _input("basis", basis, required=True),
                               t("fixed_assets.basis_hint")))
        return await _page(request, crumbs, page_header(t("fixed_assets.remeasure_title", number=asset["number"])),
                           _tabs(f"/fixed-assets/assets/{asset_id}/remeasure", "op", op, ops, "fixed_assets.remeasure_op"),
                           form, title_key="fixed_assets.remeasure")

    @app.get("/fixed-assets/assets/{asset_id}/add")
    async def add_page(request: Request, asset_id: str, kind: str = "improvement"):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        kinds = ("improvement", "replacement", "repair")
        kind = kind if kind in kinds else "improvement"
        try:
            _, currency = await _context(token)
            asset, crumbs = await _asset_crumbs(token, asset_id, "fixed_assets.add_to_asset")
            if kind != "repair":
                intake = [s for s in (await api.fixed_assets_get(token, "/intake"))["items"]
                          if s.get("kind") == "acquisition"]
                chart = (await api.get_chart(token)).get("items") or []
                parts = (await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/components"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.add_to_asset")
        tabs = _tabs(f"/fixed-assets/assets/{asset_id}/add", "kind", kind, kinds, "fixed_assets.add_kind")
        title = page_header(t("fixed_assets.add_to_asset_title", number=asset["number"]))
        if kind == "repair":
            body = Div(P(t("fixed_assets.add_hint.repair"), cls="settings-hint"),
                       A(t("fixed_assets.register_from_bills"), href="/fixed-assets/intake", cls="btn btn--primary"),
                       cls="settings-card")
            return await _page(request, crumbs, title, tabs, body, title_key="fixed_assets.add_to_asset")
        fields: list[FT] = [Input(type="hidden", name="asset_id", value=asset_id),
                            Input(type="hidden", name="kind", value=kind),
                            P(t(f"fixed_assets.add_hint.{kind}"), cls="settings-hint")]
        if kind == "replacement":
            fields.append(_field(t("fixed_assets.part_name"), _input("name", required=True)))
        fields += [_field(t("fixed_assets.source_line"), _picker("source", _source_options(intake, currency),
                                                                 label=t("fixed_assets.source_line"),
                                                                 empty="fixed_assets.enter_by_hand"),
                          t("fixed_assets.source_line_hint")),
                   _field(t("fixed_assets.amount"), _input("amount", type_="number", step="0.01", min="0")),
                   _field(t("fixed_assets.credit_account"),
                          _picker("credit_account", _account_options(chart), label=t("fixed_assets.credit_account")),
                          t("fixed_assets.credit_account_hint")),
                   _field(t("fixed_assets.date"), _input("date", type_="date"), t("fixed_assets.add_date_hint")),
                   _field(t(f"fixed_assets.life_label.{kind}"), _input("life_months", type_="number", step="1", min="1",
                                                                     required=kind == "replacement"),
                          t(f"fixed_assets.life_hint.{kind}"))]
        if kind == "replacement":
            fields += [_field(t("fixed_assets.replaced_component"),
                              _picker("replaced", [(c["id"], f"{c['number']} {c.get('name') or ''}".strip())
                                                   for c in parts if c.get("status") == "active"],
                                      label=t("fixed_assets.replaced_component"), empty="fixed_assets.not_separate"),
                              t("fixed_assets.replaced_component_hint")),
                       _field(t("fixed_assets.old_part_estimate"), _input("estimate", type_="number", step="0.01",
                                                                          min="0"),
                              t("fixed_assets.old_part_estimate_hint"))]
        return await _page(request, crumbs, title, tabs, _op_form("add-to-asset", *fields),
                           title_key="fixed_assets.add_to_asset")

    def _allocation_row(n: int, classes: list[dict]) -> FT:
        return Tr(Td(_input(f"row:{n}:name", aria_label=t("fixed_assets.name"))),
                  Td(_picker(f"row:{n}:class_id", [(c["id"], c["name"]) for c in classes],
                             label=t("fixed_assets.class"))),
                  Td(_input(f"row:{n}:amount", type_="number", step="0.01", min="0", aria_label=t("fixed_assets.amount"))),
                  Td(_input(f"row:{n}:life_months", type_="number", step="1", min="1",
                            aria_label=t("fixed_assets.life_months"))))

    async def _in_service_classes(token: str) -> list[dict]:
        return [c for c in (await api.fixed_assets_get(token, "/classes"))["items"] if not c.get("construction")]

    @app.get("/fixed-assets/assets/{asset_id}/place-in-service")
    async def place_in_service_page(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            asset, crumbs = await _asset_crumbs(token, asset_id, "fixed_assets.place_in_service")
            classes = await _in_service_classes(token)
        except APIError as e:
            return await _refused(request, e, "fixed_assets.place_in_service")
        rows = Table(Thead(Tr(Th(t("fixed_assets.name")), Th(t("fixed_assets.class")),
                              _money_th(t("fixed_assets.amount")), Th(t("fixed_assets.life_months")))),
                     Tbody(*[_allocation_row(n, classes) for n in range(3)], id="fa-allocations"), cls="data-table")
        add_row = Button(t("fixed_assets.add_row"), type="button", cls="btn btn--sm btn--secondary",
                         hx_get=f"/fixed-assets/assets/{quote(asset_id, safe=':')}/place-in-service/row",
                         hx_vals="js:{n: document.querySelectorAll('#fa-allocations tr').length}",
                         hx_target="#fa-allocations", hx_swap="beforeend")
        form = _op_form("place-in-service", Input(type="hidden", name="asset_id", value=asset_id),
                        P(t("fixed_assets.place_hint", balance=_money(asset.get("cost"), currency)), cls="settings-hint"),
                        _field(t("fixed_assets.available_date"),
                               _input("date", date.today().isoformat(), type_="date", required=True)),
                        rows, add_row, P(t("fixed_assets.allocation_hint"), cls="form-hint"))
        return await _page(request, crumbs, page_header(t("fixed_assets.place_title", number=asset["number"])),
                           form, title_key="fixed_assets.place_in_service")

    @app.get("/fixed-assets/assets/{asset_id}/place-in-service/row")
    async def allocation_row(request: Request, asset_id: str, n: int = 0):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        try:
            return _allocation_row(n, await _in_service_classes(token))
        except APIError as e:
            return Tr(Td(_error_text(e), colspan="4", cls="cell-error"))

    @app.post("/fixed-assets/assets/{asset_id}/reviews")
    async def record_review(request: Request, asset_id: str):
        """Record a review; one that found a loss goes on to the impairment it calls for."""
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        body = {"date": str(form.get("date") or ""), "outcome": str(form.get("outcome") or ""),
                "basis": str(form.get("basis") or "").strip()}
        if body["outcome"] == "recoverable_amount":
            body["recoverable_amount"] = _amount(form, "recoverable_amount")
        try:
            done = await api.fixed_assets_send(token, "POST", f"/assets/{quote(asset_id, safe=':')}/reviews", body)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))
        found = done.get("impairment")
        if not found:
            return Response(headers={"HX-Redirect": f"/fixed-assets/assets/{asset_id}",
                                     **toast_header(t("fixed_assets.done"))})
        params = {"op": "impairment", "date": found["date"], "value": found["recoverable_amount"],
                  "basis": found["basis"]}
        return Response(headers={"HX-Redirect": f"/fixed-assets/assets/{asset_id}/remeasure?{urlencode(params)}",
                                 **toast_header(t("fixed_assets.review_found_loss"), persist=True)})

    @app.post("/fixed-assets/assets/{asset_id}/reviews/{review_id}/withdraw")
    async def withdraw_review(request: Request, asset_id: str, review_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        return await _hx_write(token, "POST", f"/assets/{quote(asset_id, safe=':')}/reviews/"
                                              f"{quote(review_id, safe=':')}/withdraw", {},
                               f"/fixed-assets/assets/{asset_id}")

    @app.get("/fixed-assets/reviews")
    async def reviews_page(request: Request):
        """Impairment reviews due within 30 days or overdue. Soonest first, not newest: this
        is a list of work to do, and the most overdue is the most urgent."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            items = (await api.fixed_assets_get(token, "/reviews"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.reviews_due_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.reviews_due_title"), None)])
        rows = [Tr(Td(A(r["number"], href=f"/fixed-assets/assets/{r['asset_id']}")), Td(_text(r.get("name"))),
                   Td(r["due"]), Td(Span(t("fixed_assets.overdue"), cls="badge badge--overdue") if r["overdue"] else ""))
                for r in items]
        body = (Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")), Th(t("fixed_assets.due")),
                               Th(""))), Tbody(*rows), cls="data-table") if rows
                else empty_state_cta(t("fixed_assets.reviews_empty"), t("fixed_assets.title"), "/fixed-assets"))
        return await _page(request, crumbs, page_header(t("fixed_assets.reviews_due_title")),
                           P(t("fixed_assets.reviews_due_hint"), cls="settings-hint"), body,
                           title_key="fixed_assets.reviews_due_title")

    @app.get("/fixed-assets/classes/{class_id}/revalue")
    async def revalue_class_page(request: Request, class_id: str):
        """Every asset in use in a class, each with its fair value and basis, revalued on one date."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
            assets = [a for a in (await api.fixed_assets_get(token, "/assets", {"class_id": class_id, "limit": 500}))[
                "items"] if a.get("status") == "active"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.revalue_class")
        cls = next((c for c in classes if c["id"] == class_id), None)
        crumbs = breadcrumbs([(t("fixed_assets.settings_title"), "/settings/fixed-assets"),
                              (cls["name"] if cls else EMPTY, None)])
        if cls is None or not assets:
            return await _page(request, crumbs, page_header(t("fixed_assets.revalue_class")),
                               empty_state_cta(t("fixed_assets.class_missing" if cls is None
                                                 else "fixed_assets.revalue_class_empty"),
                                               t("fixed_assets.settings_title"), "/settings/fixed-assets"),
                               title_key="fixed_assets.revalue_class")
        rows = [Tr(Td(A(a["number"], href=f"/fixed-assets/assets/{a['id']}")), Td(_text(a.get("name"))),
                   _money_td(a.get("nbv"), currency),
                   Td(_input(f"fair_value:{a['id']}", type_="number", step="0.01", min="0",
                             aria_label=t("fixed_assets.fair_value"))),
                   Td(_input(f"basis:{a['id']}", aria_label=t("fixed_assets.basis"))))
                for a in sorted(assets, key=lambda a: a["number"])]
        table = Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                               _money_th(t("fixed_assets.carrying_amount_now")), _money_th(t("fixed_assets.fair_value")),
                               Th(t("fixed_assets.basis")))), Tbody(*rows), cls="data-table")
        policy = "revaluation" if cls.get("measurement_policy") == "revaluation" else "cost"
        form = _op_form("revalue-class", Input(type="hidden", name="class_id", value=class_id),
                        P(t(f"fixed_assets.revalue_class_hint.{policy}", name=cls["name"], count=len(assets)),
                          cls="settings-hint"),
                        _field(t("fixed_assets.measurement_date"),
                               _input("date", date.today().isoformat(), type_="date", required=True),
                               t("fixed_assets.measurement_date_hint")),
                        _field(t("fixed_assets.basis"), _input("basis"), t("fixed_assets.shared_basis_hint")),
                        Div(table, cls="table-scroll-wrap"))
        title = t(f"fixed_assets.revalue_class_title.{policy}", name=cls["name"])
        return await _page(request, crumbs, page_header(title), form, title_key="fixed_assets.revalue_class")

    @app.post("/fixed-assets/dispose/selected")
    async def dispose_selected(request: Request):
        """The register's ticked rows, carried to the disposal form in the URL."""
        ids = [str(i) for i in (await request.form()).getlist("selected")]
        return Response(headers={"HX-Redirect": f"/fixed-assets/dispose?{urlencode({'ids': ','.join(ids)})}"})

    @app.get("/fixed-assets/dispose")
    async def dispose_several_page(request: Request, ids: str = ""):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        chosen = [i for i in ids.split(",") if i]
        try:
            _, currency = await _context(token)
            assets = [await api.fixed_assets_get(token, f"/assets/{quote(i, safe=':')}") for i in chosen]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.dispose")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.dispose"), None)])
        listed = Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                                _money_th(t("fixed_assets.nbv")))),
                       Tbody(*[Tr(Td(A(a["number"], href=f"/fixed-assets/assets/{a['id']}")), Td(_text(a.get("name"))),
                                  _money_td(a.get("nbv"), currency)) for a in assets]), cls="data-table")
        form = _op_form("dispose", *[Input(type="hidden", name="asset_ids", value=a["id"]) for a in assets],
                        P(t("fixed_assets.dispose_several_hint"), cls="settings-hint"), listed,
                        *_disposal_fields(None))
        return await _page(request, crumbs, page_header(t("fixed_assets.dispose_several_title", n=len(assets))),
                           form if assets else P(t("label.select_rows_first"), cls="form-hint"),
                           title_key="fixed_assets.dispose")

    @app.post("/fixed-assets/assets/{asset_id}/request-correction")
    async def request_correction(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        body = {**_correct_body(form), "reason": str(form.get("reason") or "").strip() or None}
        try:
            saved = await api.fixed_assets_send(token, "POST", f"/assets/{quote(asset_id, safe=':')}/corrections",
                                                body)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))
        return Response(headers={"HX-Redirect": f"/fixed-assets/corrections/{saved['id']}",
                                 **toast_header(t("fixed_assets.requested"))})

    @app.get("/fixed-assets/corrections")
    async def corrections_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            items = (await api.fixed_assets_get(token, "/corrections"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.corrections_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.corrections_title"), None)])
        waiting = sum(1 for r in items if r.get("state") == "requested")
        body = (_corrections_table(items) if items
                else empty_state_cta(t("fixed_assets.corrections_empty"), t("fixed_assets.title"), "/fixed-assets",
                                     hint=t("fixed_assets.corrections_empty_hint")))
        report = A(t("fixed_assets.ppa_title"), href="/fixed-assets/reports/prior-period-adjustments",
                   cls="btn btn--secondary")
        return await _page(request, crumbs, page_header(t("fixed_assets.corrections_title"), report),
                           P(t("fixed_assets.corrections_hint"), cls="settings-hint"),
                           Div(t("fixed_assets.notice.corrections.body", count=waiting), cls="flash flash--warning")
                           if waiting else "", body, title_key="fixed_assets.corrections_title")

    @app.get("/fixed-assets/corrections/{correction_id}")
    async def correction_page(request: Request, correction_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            _, currency = await _context(token)
            record = await api.fixed_assets_get(token, f"/corrections/{quote(correction_id, safe=':')}")
        except APIError as e:
            return await _refused(request, e, "fixed_assets.corrections_title")
        state = record.get("state")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (t("fixed_assets.corrections_title"), "/fixed-assets/corrections"),
                              (record.get("number") or EMPTY, None)])
        classification = record.get("classification")
        # A request holds the chosen treatment under prior_years; once applied, the years it restated.
        applied_years = record.get("prior_years") if isinstance(record.get("prior_years"), list) else []
        treatment = record.get("treatment") if applied_years else (
            record.get("prior_years") if isinstance(record.get("prior_years"), str) else None)
        rows = [(t("fixed_assets.asset"), A(record.get("number") or EMPTY,
                                           href=f"/fixed-assets/assets/{record['asset_id']}")),
                (t("fixed_assets.status"), t(f"fixed_assets.correction_state.{state}")),
                (t("fixed_assets.correction"), _correction_what(record)),
                (t("fixed_assets.classification"),
                 t(f"fixed_assets.classification.{classification}") if classification else EMPTY),
                (t("fixed_assets.override_reason"), _text(record.get("override_reason"))),
                (t("fixed_assets.effective_date"), _text(record.get("effective_date"))),
                (t("fixed_assets.posting_date"), _text(record.get("posting_date"))),
                (t("fixed_assets.years"), ", ".join(str(y) for y in applied_years) or EMPTY),
                (t("fixed_assets.prior_years"), t(f"fixed_assets.prior_years.{treatment}") if treatment else EMPTY),
                (t("fixed_assets.immaterial_reason"), _text(record.get("immaterial_reason"))),
                (t("fixed_assets.request_note"), _text(record.get("reason"))),
                (t("fixed_assets.journal"),
                 Span(*[A(f"{t('fixed_assets.journal')} {n}", href=f"/accounting?{urlencode({'q': j})}",
                          cls="link-list-item") for n, j in enumerate(record.get("journals") or [], 1)]
                      or [empty_mark()]))]
        parts: list[FT] = [Div(_detail_table(rows), cls="detail-card section-card")]
        if record.get("prior_detail"):
            parts.append(Div(H2(t("fixed_assets.prior_years_title"), cls="section-title"),
                             _restated_table(record["prior_detail"], currency), cls="detail-card section-card"))
        hidden = Input(type="hidden", name="correction_id", value=correction_id)
        if state == "requested":
            parts.append(Div(H2(t("fixed_assets.apply_correction"), cls="section-title"),
                             _op_form("apply-correction", hidden,
                                      P(t("fixed_assets.apply_correction_hint"), cls="settings-hint"),
                                      _field(t("fixed_assets.posting_date"), _input("posting_date", type_="date"),
                                             t("fixed_assets.correction_posting_hint")),
                                      review_label=t("fixed_assets.review")),
                             Div(Button(t("fixed_assets.withdraw_correction"), type="button",
                                        cls="btn btn--secondary",
                                        hx_post=f"/fixed-assets/corrections/{correction_id}/withdraw",
                                        hx_swap="none"),
                                 P(t("fixed_assets.withdraw_hint"), cls="form-hint"), cls="form-actions"),
                             cls="detail-card section-card"))
        elif state == "applied":
            parts.append(Div(H2(t("fixed_assets.undo_correction"), cls="section-title"),
                             _op_form("undo-correction", hidden,
                                      P(t("fixed_assets.undo_correction_hint"), cls="settings-hint"),
                                      review_label=t("fixed_assets.undo_correction")),
                             cls="detail-card section-card"))
        else:
            parts.append(P(t("fixed_assets.correction_closed"), cls="form-hint"))
        return await _page(request, crumbs, page_header(t("fixed_assets.correction_title",
                                                          number=record.get("number") or EMPTY)),
                           *parts, title_key="fixed_assets.corrections_title")

    @app.post("/fixed-assets/corrections/{correction_id}/withdraw")
    async def withdraw_correction(request: Request, correction_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        return await _hx_write(token, "POST", f"/corrections/{quote(correction_id, safe=':')}/withdraw", {},
                               f"/fixed-assets/corrections/{correction_id}")

    @app.get("/fixed-assets/classes/{class_id}/apply")
    async def apply_class_page(request: Request, class_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            classes = (await api.fixed_assets_get(token, "/classes", {"include_archived": "true"}))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.apply_to_assets")
        cls = next((c for c in classes if c["id"] == class_id), None)
        crumbs = breadcrumbs([(t("fixed_assets.settings_title"), "/settings/fixed-assets"),
                              (cls["name"] if cls else EMPTY, None)])
        if cls is None:
            return await _page(request, crumbs, page_header(t("fixed_assets.apply_to_assets")),
                               empty_state_cta(t("fixed_assets.class_missing"), t("fixed_assets.settings_title"),
                                               "/settings/fixed-assets"),
                               title_key="fixed_assets.apply_to_assets")
        form = _op_form("apply-class", Input(type="hidden", name="class_id", value=class_id),
                        P(t("fixed_assets.apply_to_assets_hint", name=cls["name"]), cls="settings-hint"),
                        _field(t("fixed_assets.effective_month"), _input("effective_month", type_="month"),
                               t("fixed_assets.effective_month_hint")))
        return await _page(request, crumbs, page_header(t("fixed_assets.apply_to_assets_title", name=cls["name"])),
                           form, title_key="fixed_assets.apply_to_assets")

    async def _prior_period_report(token: str, year: int) -> dict:
        return await api.fixed_assets_get(token, "/reports/prior-period-adjustments", {"year": year})

    @app.get("/fixed-assets/reports/prior-period-adjustments")
    async def prior_period_page(request: Request, year: int | None = None):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        year = year or date.today().year - 1
        try:
            _, currency = await _context(token)
            report = await _prior_period_report(token, year)
        except APIError as e:
            return await _refused(request, e, "fixed_assets.ppa_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"),
                              (t("fixed_assets.corrections_title"), "/fixed-assets/corrections"),
                              (t("fixed_assets.ppa_title"), None)])
        chooser = Form(_input("year", year, type_="number", step="1", min="1900", aria_label=t("fixed_assets.year")),
                       Button(t("fixed_assets.show"), type="submit", cls="btn btn--secondary"),
                       method="get", action="/fixed-assets/reports/prior-period-adjustments", cls="flex-row")
        download = A(t("fixed_assets.download_csv"), cls="btn btn--secondary",
                     href=f"/fixed-assets/reports/prior-period-adjustments.csv?{urlencode({'year': year})}")
        return await _page(request, crumbs, page_header(t("fixed_assets.ppa_title_year", year=year), download),
                           P(t("fixed_assets.ppa_hint"), cls="settings-hint"), chooser,
                           Div(*_report_table(report, currency), cls="detail-card section-card"),
                           title_key="fixed_assets.ppa_title")

    @app.get("/fixed-assets/reports/prior-period-adjustments.csv")
    async def prior_period_csv(request: Request, year: int):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            report = await _prior_period_report(token, year)
        except APIError as e:
            return PlainTextResponse(_error_text(e), status_code=e.status or 400)
        return PlainTextResponse(_report_csv(report), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="prior-period-adjustments-{year}.csv"'})

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
