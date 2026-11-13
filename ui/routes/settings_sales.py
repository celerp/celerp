# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Sales: Taxes, Payment Terms, Connectors, Doc Numbering."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.encode import call, EVENT, THIS
from ui.components.shell import base_shell, page_header, page_title
from ui.components.table import EMPTY
from ui.config import COOKIE_NAME
from ui.i18n import doc_type_label, get_lang, refusal_text, t

from ui.routes.settings import (
    _token,
    _check_permission,
    _taxes_tab,
    _terms_conditions_tab,
)
from ui.routes.settings_general import _section_breadcrumb

# The list types are numbered here too. Transfers and audits may move to an Inventory settings
# tab when the Lists menu is split by type (SAL-001). The old shared "list" counter is listed
# only while a company has it: quotation and untyped Lists, and transfers until their own row
# is saved, keep drawing from it.
_SALES_DOC_TYPES = frozenset({"invoice", "proforma", "memo", "receipt", "credit_note",
                              "quotation", "transfer", "audit", "writeoff", "shipping_doc", "list"})


def _sales_tabs(active: str, lang: str = "en") -> FT:
    tabs: list[tuple[str, str]] = [
        ("taxes", t("settings.tab_taxes", lang)),
        ("terms-conditions", t("page.terms_conditions", lang)),
        ("numbering", t("settings_sales.tab_numbering", lang)),
        ("line-items", t("settings.tab_line_items", lang)),
    ]
    return Div(
        *[
            A(label, href=f"/settings/sales?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


async def _numbering_content(token: str, doc_types: frozenset[str]) -> FT:
    """The Numbering tab for ``doc_types``, or the reason the counters cannot be shown,
    with a link to Company Details when the company time zone is the reason."""
    try:
        sequences = await api.get_doc_sequences(token)
    except APIError as e:
        fix = (A(t("nav.company_details"), href="/finance/company-details", cls="btn btn--secondary btn--xs")
               if (e.data or {}).get("code") == "company_timezone" else "")
        return Div(P(_refusal(e), cls="cell-error"), fix)
    return _numbering_tab([s for s in sequences if s.get("doc_type") in doc_types])


def _numbering_tab(sequences: list[dict]) -> FT:
    """Render the document numbering settings table."""
    rows = [_numbering_row(seq["doc_type"], seq) for seq in sequences]
    return Div(
        H3(t("page.document_numbering"), cls="settings-section-title"),
        P(t("settings_sales.numbering_hint"), cls="settings-hint"),
        Table(
            Thead(Tr(
                Th(t("th.document_type"), cls="th--center"),
                Th(t("th.prefix"), cls="th--center"),
                Th(t("th.pattern"), cls="th--center"),
                Th(t("th.next"), cls="th--center"),
                Th(t("btn.preview"), cls="th--center"),
                Th("", cls="th--center"),
            )),
            Tbody(*rows),
            cls="data-table",
        ),
    )


# Line item identifier: what the identifier column shows on document lines. The
# raw mode (sku/barcode/barcode_sku) is canonical; the label is a display key
# resolved at render time (R1/R3).
_LINE_IDENT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("sku", "field.sku"),
    ("barcode", "field.barcode"),
    ("barcode_sku", "settings_sales.line_ident_barcode_sku"),
)

# One sample line for the preview rows (a lot barcode over a product-type SKU).
_LINE_IDENT_SAMPLE = {"sku": "GEM-001", "barcode": "2130060000068"}


def _line_items_tab(company: dict, lang: str = "en") -> FT:
    """How line items identify the piece, on every document (screen, print, PDF, share)."""
    from celerp.services.line_measures import LINE_IDENTIFIER_MODES, line_identifier

    current = company.get("line_item_identifier") or (company.get("settings") or {}).get("line_item_identifier")
    if current not in LINE_IDENTIFIER_MODES:
        current = "sku"

    def _preview_cell(mode: str) -> FT:
        primary, secondary = line_identifier(_LINE_IDENT_SAMPLE, mode)
        return Td(Div(primary, cls="li-ident-1st"),
                  Div(secondary, cls="li-ident-2nd") if secondary else None,
                  cls="col-sku")

    preview_rows = [
        Tr(Td(t(label_key, lang)), _preview_cell(val))
        for val, label_key in _LINE_IDENT_OPTIONS
    ]
    return Div(
        H3(t("page.line_items", lang), cls="settings-section-title"),
        P(t("msg.line_item_identifier_hint", lang), cls="settings-hint"),
        Div(
            Label(t("label.line_item_identifier", lang), fr="line-item-identifier"),
            Select(
                *[Option(t(label_key, lang), value=val, selected=(val == current))
                  for val, label_key in _LINE_IDENT_OPTIONS],
                name="value", id="line-item-identifier",
                hx_patch="/settings/sales/line-identifier",
                hx_target="#li-ident-saved", hx_swap="innerHTML", hx_include="this",
                hx_trigger="change",
                cls="cell-input cell-input--select",
            ),
            Span("", id="li-ident-saved", cls="settings-hint"),
            cls="settings-field-row",
            style="display:flex;align-items:center;gap:10px;margin-bottom:14px;",
        ),
        Table(
            Thead(Tr(Th(t("th.options", lang)), Th(t("btn.preview", lang)))),
            Tbody(*preview_rows),
            cls="data-table", style="max-width:420px;",
        ),
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/sales")
    async def settings_sales_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_module_settings")):
            return r
        tab = request.query_params.get("tab", "taxes")
        try:
            taxes = await api.get_taxes(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            taxes = []

        lang = get_lang(request)

        if tab == "taxes":
            content = _taxes_tab(taxes, lang=lang)
        elif tab == "terms-conditions":
            try:
                tc_templates = await api.get_terms_conditions(token)
            except (APIError, Exception):
                tc_templates = []
            content = _terms_conditions_tab(tc_templates, scope="sales")
        elif tab == "numbering":
            content = await _numbering_content(token, _SALES_DOC_TYPES)
        elif tab == "line-items":
            try:
                company = await api.get_company(token)
            except (APIError, Exception):
                company = {}
            content = _line_items_tab(company, lang=lang)
        else:
            content = _taxes_tab(taxes, lang=lang)
            tab = "taxes"

        return await base_shell(
            _section_breadcrumb(t("dashboard.sales", lang)),
            page_header(t("settings_sales.page_title", lang)),
            _sales_tabs(tab, lang=lang),
            content,
            title=page_title("page.settings"),
            nav_active="settings",
            lang=lang,
            request=request,
        )

    # --- Inline edit endpoints for numbering ---

    @app.get("/settings/numbering/{doc_type}/{field}/edit")
    async def numbering_field_edit(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            sequences = await api.get_doc_sequences(token)
        except (APIError, Exception):
            return P(t("settings.error_loading_sequences"), cls="cell-error")
        seq = _listed_sequence(sequences, doc_type) or {}
        return _numbering_input(doc_type, field, str(seq.get(field, "")))

    @app.get("/settings/numbering/{doc_type}/{field}/display")
    async def numbering_field_display(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            sequences = await api.get_doc_sequences(token)
        except (APIError, Exception):
            return P(t("settings.error"), cls="cell-error")
        seq = _listed_sequence(sequences, doc_type) or {}
        value = seq.get(field, "")
        return Div(
            str(value) or EMPTY,
            hx_get=f"/settings/numbering/{doc_type}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title=t("settings.click_to_edit"), cls="editable-cell",
        )

    @app.patch("/settings/numbering/{doc_type}/{field}")
    async def numbering_field_patch(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", "")).strip()
        try:
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            return P(_refusal(e), cls="cell-error")
        seq = _listed_sequence(sequences, doc_type)
        # Leaving a cell unchanged saves nothing: a save would split a type still on the shared
        # List counter off it, or be refused for a value the user did not change. A type the API
        # does not list has nothing saved, so its save goes to the API, which refuses it.
        if seq is not None and value == str(seq.get(field, "")).strip():
            return _numbering_row(doc_type, seq)
        patch_data = {}
        if field == "prefix":
            patch_data["prefix"] = value
        elif field == "pattern":
            patch_data["pattern"] = value
        elif field == "next":
            try:
                patch_data["next"] = int(value)
            except ValueError:
                return _numbering_row(doc_type, seq or {}, editing=(field, value),
                                      error=t("settings.must_be_a_number"))
        try:
            await api.patch_doc_sequence(token, doc_type, patch_data)
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            # The row stays as it was with the typed value still open, so the user can change it.
            return _numbering_row(doc_type, seq or {}, editing=(field, value), error=_refusal(e))
        return _numbering_row(doc_type, _listed_sequence(sequences, doc_type) or {})

    @app.post("/settings/numbering/{doc_type}/reset")
    async def numbering_reset(request: Request, doc_type: str):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            return P(_refusal(e), cls="cell-error")
        seq = _listed_sequence(sequences, doc_type) or {}
        try:
            await api.patch_doc_sequence(token, doc_type, {"next": 1})
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            return _numbering_row(doc_type, seq, error=_refusal(e))
        return _numbering_row(doc_type, _listed_sequence(sequences, doc_type) or {})

    @app.patch("/settings/sales/line-identifier")
    async def line_identifier_patch(request: Request):
        """Save the company's line item identifier mode; returns the saved note."""
        from celerp.services.line_measures import LINE_IDENTIFIER_MODES
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        if value not in LINE_IDENTIFIER_MODES:
            return Span(t("error.invalid_value"), cls="cell-error")
        try:
            await api.patch_company(token, {"line_item_identifier": value})
        except APIError as e:
            return Span(_refusal(e), cls="cell-error")
        return Span(t("msg.saved"))


def _refusal(e: APIError) -> str:
    """An API refusal in the user's language."""
    return refusal_text(e.detail) or str(e.detail)


def _numbering_input(dt: str, field: str, value: str, error: FT = "") -> FT:
    """A numbering cell open for editing, with the reason a save was refused when there is
    one: blur saves, Escape restores the saved value."""
    restore_url = f"/settings/numbering/{dt}/{field}/display"
    esc_js = call("celerpCellKey", THIS, EVENT, {"url": restore_url, "target": ".editable-cell"})
    extra = {"type": "number", "min": "1"} if field == "next" else {"type": "text"}
    return Div(
        Input(
            name="value", value=value,
            hx_patch=f"/settings/numbering/{dt}/{field}",
            hx_target="closest tr", hx_swap="outerHTML",
            hx_trigger="blur delay:200ms",
            cls="cell-input", autofocus=True,
            onkeydown=esc_js,
            oninput="this.dataset.dirty='1'",
            **extra,
        ),
        error,
        cls="editable-cell editable-cell--editing",
    )


def _listed_sequence(sequences: list[dict], doc_type: str) -> dict | None:
    """The API's numbering for ``doc_type``, or None when the API does not list that type."""
    return next((s for s in sequences if s["doc_type"] == doc_type), None)


def _numbering_row(dt: str, seq: dict, *, editing: tuple[str, str] | None = None,
                   error: str | None = None) -> FT:
    """The numbering table row of doc type ``dt`` (the table and every HTMX swap after an edit),
    showing ``seq``, the API's numbering for it (empty when the API does not list it).
    ``editing`` is the (field, typed value) left open after a refused save; ``error`` is the
    reason, shown in that field's cell, or beside Reset when no field is open."""
    edit_title = t("settings.click_to_edit")
    error_p = P(error, cls="cell-error", role="alert") if error else ""

    def cell(field: str, shown: str) -> FT:
        if editing and editing[0] == field:
            return Td(_numbering_input(dt, field, editing[1], error_p))
        return Td(Div(
            shown,
            hx_get=f"/settings/numbering/{dt}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title=edit_title, cls="editable-cell",
        ))

    return Tr(
        Td(doc_type_label(dt)),
        cell("prefix", seq.get("prefix") or EMPTY),
        cell("pattern", seq.get("pattern") or EMPTY),
        cell("next", str(seq.get("next", 1))),
        Td(P(refusal_text(seq["refusal"]), cls="cell-error", role="alert") if seq.get("refusal")
           else Code(seq.get("preview", ""), cls="numbering-preview")),
        Td(Button(t("btn.reset"), hx_post=f"/settings/numbering/{dt}/reset",
                  hx_target="closest tr", hx_swap="outerHTML",
                  cls="btn btn--xs btn--secondary"),
           "" if editing else error_p),
    )
