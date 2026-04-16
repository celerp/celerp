# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Sales: Taxes, Payment Terms, Connectors, Doc Numbering."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY
from ui.config import COOKIE_NAME
from ui.i18n import t, get_lang

from ui.routes.settings import (
    _token,
    _check_role,
    _taxes_tab,
    _connectors_tab,
    _terms_conditions_tab,
)
from ui.routes.settings_general import _section_breadcrumb

_DOC_TYPE_LABELS = {
    "invoice": "Invoice",
    "purchase_order": "Purchase Order",
    "quotation": "Quotation",
    "credit_note": "Credit Note",
    "bill": "Bill",
    "memo": "Memo",
    "shipping_doc": "Shipping Doc",
    "list": "List",
    "consignment_in": "Consignment In",
}

_SALES_DOC_TYPES = frozenset({"invoice", "proforma", "memo", "receipt", "credit_note"})


def _sales_tabs(active: str, enabled_modules: set[str], lang: str = "en") -> FT:
    tabs: list[tuple[str, str]] = [
        ("taxes", t("settings.tab_taxes", lang)),
        ("terms-conditions", "Terms & Conditions"),
        ("numbering", "Numbering"),
    ]
    if "celerp-connectors" in enabled_modules:
        tabs.append(("connectors", t("settings.tab_connectors", lang)))
    return Div(
        *[
            A(label, href=f"/settings/sales?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _numbering_tab(sequences: list[dict]) -> FT:
    """Render the document numbering settings table."""
    rows = []
    for seq in sequences:
        dt = seq["doc_type"]
        label = _DOC_TYPE_LABELS.get(dt, dt.replace("_", " ").title())
        rows.append(Tr(
            Td(label),
            Td(
                Div(
                    seq.get("prefix") or EMPTY,
                    hx_get=f"/settings/numbering/{dt}/prefix/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="click",
                    title="Click to edit", cls="editable-cell",
                ),
            ),
            Td(
                Div(
                    seq.get("pattern") or EMPTY,
                    hx_get=f"/settings/numbering/{dt}/pattern/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="click",
                    title="Click to edit", cls="editable-cell",
                ),
            ),
            Td(
                Div(
                    str(seq.get("next", 1)),
                    hx_get=f"/settings/numbering/{dt}/next/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="click",
                    title="Click to edit", cls="editable-cell",
                ),
            ),
            Td(Code(seq.get("preview", ""), cls="numbering-preview")),
            Td(
                Button(t("btn.reset"), hx_post=f"/settings/numbering/{dt}/reset",
                       hx_target="closest tr", hx_swap="outerHTML",
                       cls="btn btn--xs btn--secondary"),
            ),
        ))
    return Div(
        H3(t("page.document_numbering"), cls="settings-section-title"),
        P("Configure the format and sequence for each document type. "
          "Tokens: {PREFIX}, {YYYY}, {YY}, {MM}, {DD}, {##} (# count = digit padding).",
          cls="settings-hint"),
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


def setup_routes(app):

    @app.get("/settings/sales")
    async def settings_sales_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        tab = request.query_params.get("tab", "taxes")
        try:
            taxes = await api.get_taxes(token)
            modules = await api.get_modules(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            taxes, modules = [], []

        enabled_modules = {m["name"] for m in modules if m.get("enabled")}
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
            try:
                sequences = await api.get_doc_sequences(token)
            except (APIError, Exception):
                sequences = []
            content = _numbering_tab([s for s in sequences if s.get("doc_type") in _SALES_DOC_TYPES])
        elif tab == "connectors":
            content = _connectors_tab()
        else:
            content = _taxes_tab(taxes, lang=lang)
            tab = "taxes"

        return base_shell(
            _section_breadcrumb("Sales"),
            page_header("Sales Documents Settings"),
            _sales_tabs(tab, enabled_modules=enabled_modules, lang=lang),
            content,
            title="Settings - Celerp",
            nav_active="settings",
            lang=lang,
            request=request,
        )

    # --- Inline edit endpoints for numbering ---

    @app.get("/settings/numbering/{doc_type}/{field}/edit")
    async def numbering_field_edit(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            sequences = await api.get_doc_sequences(token)
        except (APIError, Exception):
            return P(t("settings.error_loading_sequences"), cls="cell-error")
        seq = next((s for s in sequences if s["doc_type"] == doc_type), {})
        value = str(seq.get(field, ""))

        restore_url = f"/settings/numbering/{doc_type}/{field}/display"
        esc_js = (f"if(event.key==='Escape'){{htmx.ajax('GET','{restore_url}',"
                  f"{{target:this.closest('.editable-cell'),swap:'outerHTML'}});event.preventDefault();}}")

        input_type = "number" if field == "next" else "text"
        extra = {"min": "1"} if field == "next" else {}
        return Div(
            Input(
                type=input_type, name="value", value=value,
                hx_patch=f"/settings/numbering/{doc_type}/{field}",
                hx_target="closest tr", hx_swap="outerHTML",
                hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
                onkeydown=esc_js,
                oninput="this.dataset.dirty='1'",
                **extra,
            ),
            cls="editable-cell editable-cell--editing",
        )

    @app.get("/settings/numbering/{doc_type}/{field}/display")
    async def numbering_field_display(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            sequences = await api.get_doc_sequences(token)
        except (APIError, Exception):
            return P(t("settings.error"), cls="cell-error")
        seq = next((s for s in sequences if s["doc_type"] == doc_type), {})
        value = seq.get(field, "")
        return Div(
            str(value) or EMPTY,
            hx_get=f"/settings/numbering/{doc_type}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title="Click to edit", cls="editable-cell",
        )

    @app.patch("/settings/numbering/{doc_type}/{field}")
    async def numbering_field_patch(request: Request, doc_type: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", "")).strip()
        patch_data = {}
        if field == "prefix":
            patch_data["prefix"] = value
        elif field == "pattern":
            patch_data["pattern"] = value
        elif field == "next":
            try:
                patch_data["next"] = int(value)
            except ValueError:
                return P(t("settings.must_be_a_number"), cls="cell-error")
        try:
            await api.patch_doc_sequence(token, doc_type, patch_data)
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        seq = next((s for s in sequences if s["doc_type"] == doc_type), {})
        return _numbering_row(seq)

    @app.post("/settings/numbering/{doc_type}/reset")
    async def numbering_reset(request: Request, doc_type: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.patch_doc_sequence(token, doc_type, {"next": 1})
            sequences = await api.get_doc_sequences(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        seq = next((s for s in sequences if s["doc_type"] == doc_type), {})
        return _numbering_row(seq)


def _numbering_row(seq: dict) -> FT:
    """Render a single numbering table row (used for HTMX swap after edit)."""
    dt = seq["doc_type"]
    label = _DOC_TYPE_LABELS.get(dt, dt.replace("_", " ").title())
    return Tr(
        Td(label),
        Td(Div(
            seq.get("prefix") or EMPTY,
            hx_get=f"/settings/numbering/{dt}/prefix/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title="Click to edit", cls="editable-cell",
        )),
        Td(Div(
            seq.get("pattern") or EMPTY,
            hx_get=f"/settings/numbering/{dt}/pattern/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title="Click to edit", cls="editable-cell",
        )),
        Td(Div(
            str(seq.get("next", 1)),
            hx_get=f"/settings/numbering/{dt}/next/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            title="Click to edit", cls="editable-cell",
        )),
        Td(Code(seq.get("preview", ""), cls="numbering-preview")),
        Td(Button(t("btn.reset"), hx_post=f"/settings/numbering/{dt}/reset",
                  hx_target="closest tr", hx_swap="outerHTML",
                  cls="btn btn--xs btn--secondary")),
    )
