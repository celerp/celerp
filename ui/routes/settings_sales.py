# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Sales: Taxes, Payment Terms, Connectors, Doc Numbering."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from celerp.services.business_time import business_today

import ui.api_client as api
from ui.api_client import APIError
from ui.components.encode import call, EVENT, THIS
from ui.components.operation_key import required_operation_key
from ui.components.preview_confirm import confirmed_binding
from ui.components.shell import base_shell, flash, page_header, page_title, toast_header
from ui.components.table import EMPTY, bulk_toolbar, fmt_money, select_all_th
from ui.config import COOKIE_NAME
from ui.i18n import doc_type_label, get_lang, refusal_text, t

from ui.routes.settings import (
    _token,
    _check_permission,
    _localization_card,
    _localization_preview,
    _rate_review_panel,
    _taxes_tab,
    _terms_conditions_tab,
)
from ui.routes.reports import _get_company
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


async def _taxes_content(token: str, taxes: list[dict], lang: str):
    """Settings > Taxes: the localization card above the tax table."""
    company = await _get_company(token)
    today = business_today(company.get("timezone"))
    return Div(_localization_card(company.get("settings") or {}), _taxes_tab(taxes, today, lang=lang))


def setup_routes(app):

    # --- Update localization (the card above the tax table) ---

    async def _localization_call(request: Request, call):
        """Run one localization API call for the card's panel: a refusal shows in the panel,
        success reloads the page so the card and the tax table both show the result."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            return await call(token, await request.form())
        except ValueError as exc:
            return P(str(exc), cls="cell-error")
        except APIError as e:
            if e.status == 401:
                return P(t("error.session_expired"), cls="cell-error")
            return P(api.error_message(e), cls="cell-error")

    @app.post("/settings/sales/localization/preview")
    async def localization_preview(request: Request):
        async def call(token, form):
            return _localization_preview(await api.localization_preview(token))
        return await _localization_call(request, call)

    @app.post("/settings/sales/localization/confirm")
    async def localization_confirm(request: Request):
        async def call(token, form):
            await api.localization_confirm(token, confirmed_binding(form, t("localization.stale_preview")),
                                           required_operation_key(form))
            return Response("", status_code=204, headers={"HX-Refresh": "true"})
        return await _localization_call(request, call)

    @app.post("/settings/sales/localization/undo")
    async def localization_undo(request: Request):
        async def call(token, form):
            kept = (await api.localization_undo(token, str(form.get("operation_id") or "")))["kept"]
            notice = toast_header(t("localization.kept", names=", ".join(row["name"] for row in kept)),
                                  "warning", persist=True) if kept else {}
            return Response("", status_code=204, headers={"HX-Refresh": "true", **notice})
        return await _localization_call(request, call)

    async def _rate_review(token: str, operation_id: str, notice: str = "", left: list[str] = ()):
        """The review panel re-read after a change; ``notice`` names the ``left`` item ids."""
        names = [(await api.get_item(token, i)).get("name") or i for i in left]
        code_names = {tax.get("id"): tax.get("name") for tax in await api.get_taxes(token)}
        return _rate_review_panel(operation_id, await api.rate_review_get(token, operation_id), code_names,
                                  t(notice, names=", ".join(names)) if names else "")

    @app.get("/settings/sales/localization/rate-review/{operation_id}")
    async def rate_review(request: Request, operation_id: str):
        async def call(token, form):
            return await _rate_review(token, operation_id)
        return await _localization_call(request, call)

    @app.post("/settings/sales/localization/rate-review/{operation_id}/apply")
    async def rate_review_apply(request: Request, operation_id: str):
        async def call(token, form):
            skipped = (await api.rate_review_apply(token, operation_id, list(dict.fromkeys(form.getlist("selected"))),
                                                   request.query_params.get("binding", "")))["skipped"]
            return await _rate_review(token, operation_id, "rate_review.edited_since", skipped)
        return await _localization_call(request, call)

    @app.post("/settings/sales/localization/rate-review/{operation_id}/undo")
    async def rate_review_undo(request: Request, operation_id: str):
        async def call(token, form):
            kept = (await api.rate_review_undo(token, operation_id))["kept"]
            return await _rate_review(token, operation_id, "localization.kept", kept)
        return await _localization_call(request, call)

    @app.get("/settings/sales/localization/close")
    async def localization_close(request: Request):
        return Response("")

    # --- Withholding corrections (S33): bills from before tax codes ---

    @app.get(_CORRECTIONS)
    async def withholding_corrections_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_accounting")):
            return r
        try:
            view = await api.withholding_corrections(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            view, refused = {}, [_refusal(e)]
        else:
            refused = []
        return await base_shell(
            _section_breadcrumb(t("page.withholding_corrections")),
            page_header(t("page.withholding_corrections")),
            _corrections_card(view, refused, await _today(token)),
            title=page_title("page.withholding_corrections"),
            nav_active="settings",
            request=request,
        )

    async def _today(token: str) -> str:
        """The company's business day, the date a correction is posted on unless changed."""
        return business_today((await _get_company(token)).get("timezone")).isoformat()

    async def _corrections_bulk(request: Request, act) -> FT:
        """Run ``act`` on every ticked bill, then the card re-read; each refusal is listed."""
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        form = await request.form()
        refused = []
        for doc_id in dict.fromkeys(form.getlist("selected")):
            try:
                await act(token, doc_id, form)
            except APIError as e:
                if e.status == 401:
                    return P(t("error.session_expired"), cls="cell-error")
                refused.append(_refusal(e))
        return _corrections_card(await api.withholding_corrections(token), refused, await _today(token))

    @app.post(_CORRECTIONS + "/post")
    async def withholding_corrections_post(request: Request):
        return await _corrections_bulk(
            request, lambda token, doc_id, form: api.withholding_correct(token, doc_id, str(form.get("date") or "")))

    @app.post(_CORRECTIONS + "/undo")
    async def withholding_corrections_undo(request: Request):
        return await _corrections_bulk(request, lambda token, doc_id, form: api.withholding_correction_undo(token, doc_id))

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
            content = await _taxes_content(token, taxes, lang)
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
            content = await _taxes_content(token, taxes, lang)
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
        if r := await _check_permission(request, "manage_module_settings"):
            return r
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
        if r := await _check_permission(request, "manage_module_settings"):
            return r
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
        if r := await _check_permission(request, "manage_module_settings"):
            return r
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
        if r := await _check_permission(request, "manage_module_settings"):
            return r
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


_CORRECTIONS = "/settings/taxes/withholding-corrections"


def _corrections_card(view: dict, refused: list[str], today: str) -> FT:
    """Withholding corrections: the bills waiting for theirs, ticked and posted on one date with
    the bulk toolbar, and the corrected bills, ticked and undone the same way."""
    def table(table_id: str, docs: list[dict]) -> FT:
        def row(doc: dict) -> FT:
            return Tr(Td(Input(type="checkbox", cls="bulk-select", name="selected", value=doc["doc_id"]),
                         cls="col-checkbox"),
                      Td(A(doc["number"], href=f"/docs/{doc['doc_id']}")), Td(doc.get("contact_name") or EMPTY),
                      Td(doc.get("issue_date") or EMPTY),
                      Td(fmt_money(doc.get("withheld") or 0, doc.get("currency")), cls="cell--number"), cls="data-row")
        return Div(Table(Thead(Tr(select_all_th(), Th(t("th.number")), Th(t("th.contact")), Th(t("th.date")),
                                  Th(t("documents.withheld"), cls="cell--number"))),
                         Tbody(*[row(doc) for doc in docs]), cls="data-table", id=table_id),
                   cls="table-scroll-wrap")

    def action(value: str, label: str, path: str, **extra) -> dict:
        return {"value": value, "label": label, "method": "post", "url": _CORRECTIONS + path,
                "target": "#withholding-corrections", "swap": "outerHTML", **extra}

    bills, corrected = view.get("bills") or [], view.get("corrected") or []
    day = Input(type="date", name="date", value=today,
                cls="form-input form-input--sm bulk-field")
    waiting = (bulk_toolbar("corrections-waiting", [action("post", t("tax.corrections_post"), "/post", fields=True)],
                            fields=[day]),
               table("corrections-waiting", bills)) if bills else (P(t("tax.corrections_none"), cls="hint"),)
    done = (H4(t("tax.corrections_done")),
            bulk_toolbar("corrections-done", [action("undo", t("btn.undo"), "/undo")]),
            table("corrections-done", corrected)) if corrected else ()
    return Div(*[flash(text) for text in refused], P(t("tax.corrections_hint"), cls="text-muted"),
               *waiting, *done, id="withholding-corrections")


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
        data_numbering_type=dt,
    )
