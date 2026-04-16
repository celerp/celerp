# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Contacts: Payment Terms & Tags configuration."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import COOKIE_NAME

from ui.routes.settings import _token, _check_role, _terms_tab, _price_lists_tab
from ui.routes.settings_general import _section_breadcrumb
from ui.i18n import t, get_lang


def _contacts_tabs(active: str) -> FT:
    tabs: list[tuple[str, str]] = [
        ("payment-terms", "Payment Terms"),
        ("price-lists", "Price Lists"),
        ("tags", "Tags"),
        ("defaults", "Defaults"),
    ]
    return Div(
        *[
            A(label, href=f"/settings/contacts?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _tags_tab(tags: list[dict]) -> FT:
    """Render the managed contact tags vocabulary table with inline editing."""
    rows = []
    for tag in tags:
        name = tag.get("name", "")
        color = tag.get("color") or ""
        category = tag.get("category") or ""
        color_swatch = Span("", cls="tag-swatch", style=f"background:{color}" if color else "background:#ccc")
        rows.append(Tr(
            Td(name),
            Td(color_swatch, Span(color or "—", cls="text--muted" if not color else "")),
            Td(category or "—", cls="text--muted" if not category else ""),
            Td(
                Button(t("btn.edit"),
                       hx_get=f"/settings/contacts/tags/{name}/edit",
                       hx_target=f"#tag-row-{name.replace(' ', '-')}",
                       hx_swap="outerHTML",
                       cls="btn btn--ghost btn--xs"),
                Button(t("btn.delete"),
                       hx_delete=f"/settings/contacts/tags/{name}",
                       hx_target="#tags-table-container",
                       hx_swap="innerHTML",
                       hx_confirm=f"Delete tag '{name}'?",
                       cls="btn btn--ghost btn--xs btn--danger"),
            ),
            cls="data-row", id=f"tag-row-{name.replace(' ', '-')}",
        ))

    table = Table(
        Thead(Tr(Th(t("th.name")), Th(t("th.color")), Th(t("th.category")), Th(t("th.actions")))),
        Tbody(*rows) if rows else Tbody(Tr(Td(t("label.no_tags_defined_yet"), colspan="4", cls="empty-state-msg"))),
        cls="data-table data-table--compact",
    )

    add_form = Form(
        Div(
            Div(Label(t("th.name"), cls="form-label"), Input(type="text", name="name", required=True, cls="form-input"), cls="form-group"),
            Div(Label(t("th.color"), cls="form-label"), Input(type="color", name="color", value="#6366f1", cls="form-input"), cls="form-group"),
            Div(Label(t("th.category"), cls="form-label"), Input(type="text", name="category", placeholder="e.g. Status, Region", cls="form-input"), cls="form-group"),
            Button(t("btn.add_tag"), type="submit", cls="btn btn--primary btn--sm"),
            cls="form-row",
        ),
        hx_post="/settings/contacts/tags",
        hx_target="#tags-table-container",
        hx_swap="innerHTML",
    )

    return Div(table, H3(t("btn.add_tag"), cls="section-title"), add_form, id="tags-table-container")


def _defaults_tab(defaults: dict, price_lists: list[dict], payment_terms: list[dict]) -> FT:
    """Render the contact defaults settings form."""
    cur_pl = defaults.get("default_price_list") or ""
    cur_pt = defaults.get("default_payment_terms") or ""
    cur_cl = defaults.get("default_credit_limit")

    pl_options = [Option(t("label._none"), value="")]
    for pl in price_lists:
        name = pl.get("name", "")
        pl_options.append(Option(name, value=name, selected=(name == cur_pl)))

    pt_options = [Option(t("label._none"), value="")]
    for term in payment_terms:
        name = term.get("name", "")
        pt_options.append(Option(name, value=name, selected=(name == cur_pt)))

    form = Form(
        Div(
            Div(
                Label(t("page.default_price_list"), cls="form-label"),
                Select(*pl_options, name="default_price_list", cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("label.default_payment_terms"), cls="form-label"),
                Select(*pt_options, name="default_payment_terms", cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("label.default_credit_limit"), cls="form-label"),
                Input(type="number", name="default_credit_limit",
                      value=str(cur_cl) if cur_cl else "",
                      step="0.01", min="0", placeholder="0.00",
                      cls="form-input"),
                cls="form-group",
            ),
            Button(t("btn.save_defaults"), type="submit", cls="btn btn--primary btn--sm"),
            cls="form-row",
        ),
        hx_patch="/settings/contacts/defaults",
        hx_target="#defaults-tab-container",
        hx_swap="innerHTML",
    )

    return Div(form, id="defaults-tab-container")


def setup_routes(app):

    @app.get("/settings/contacts")
    async def settings_contacts_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        tab = request.query_params.get("tab", "payment-terms")

        if tab == "tags":
            try:
                tags = await api.get_contact_tags_vocabulary(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                tags = []
            content = _tags_tab(tags)
        elif tab == "price-lists":
            try:
                price_lists = await api.get_price_lists(token)
                default_price_list = await api.get_default_price_list(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                price_lists, default_price_list = [], "Retail"
            content = _price_lists_tab(price_lists, default_price_list)
        elif tab == "defaults":
            try:
                defaults = await api.get_contact_defaults(token)
                price_lists = await api.get_price_lists(token)
                terms = await api.get_payment_terms(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                defaults, price_lists, terms = {}, [], []
            content = _defaults_tab(defaults, price_lists, terms)
        else:
            tab = "payment-terms"
            try:
                terms = await api.get_payment_terms(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                terms = []
            content = _terms_tab(terms, prefix="terms", import_path=None)

        return base_shell(
            _section_breadcrumb("Contacts"),
            page_header("Contacts Settings"),
            _contacts_tabs(tab),
            content,
            title="Contacts Settings - Celerp",
            nav_active="settings",
            request=request,
        )

    @app.post("/settings/contacts/tags")
    async def add_managed_tag(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        color = str(form.get("color", "")).strip() or None
        category = str(form.get("category", "")).strip() or None
        if not name:
            try:
                tags = await api.get_contact_tags_vocabulary(token)
            except Exception:
                tags = []
            return _tags_tab(tags)
        try:
            tags = await api.get_contact_tags_vocabulary(token)
        except Exception:
            tags = []
        if not any(tag.get("name") == name for tag in tags):
            tags.append({"name": name, "color": color, "category": category})
            await api.patch_contact_tags_vocabulary(token, tags)
        return _tags_tab(tags)

    @app.get("/settings/contacts/tags/{tag_name}/edit")
    async def edit_managed_tag_form(request: Request, tag_name: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            tags = await api.get_contact_tags_vocabulary(token)
        except Exception:
            tags = []
        tag = next((tag for tag in tags if tag.get("name") == tag_name), {})
        return Tr(
            Td(Input(type="text", name="name", value=tag.get("name", ""), cls="form-input form-input--sm", required=True)),
            Td(Input(type="color", name="color", value=tag.get("color") or "#6366f1", cls="form-input form-input--sm")),
            Td(Input(type="text", name="category", value=tag.get("category") or "", cls="form-input form-input--sm")),
            Td(
                Button(t("btn.save"), type="submit", cls="btn btn--primary btn--xs"),
                Button(t("btn.cancel"),
                       hx_get="/settings/contacts?tab=tags",
                       hx_target="#tags-table-container",
                       hx_swap="innerHTML",
                       hx_select="#tags-table-container",
                       cls="btn btn--secondary btn--xs"),
            ),
            hx_patch=f"/settings/contacts/tags/{tag_name}",
            hx_target="#tags-table-container",
            hx_swap="innerHTML",
            hx_include="closest tr",
            id=f"tag-row-{tag_name.replace(' ', '-')}",
        )

    @app.patch("/settings/contacts/tags/{tag_name}")
    async def update_managed_tag(request: Request, tag_name: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        new_name = str(form.get("name", "")).strip() or tag_name
        color = str(form.get("color", "")).strip() or None
        category = str(form.get("category", "")).strip() or None
        try:
            tags = await api.get_contact_tags_vocabulary(token)
        except Exception:
            tags = []
        tags = [
            {"name": new_name, "color": color, "category": category} if t.get("name") == tag_name else t
            for tag in tags
        ]
        await api.patch_contact_tags_vocabulary(token, tags)
        return _tags_tab(tags)

    @app.delete("/settings/contacts/tags/{tag_name}")
    async def delete_managed_tag(request: Request, tag_name: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            tags = await api.get_contact_tags_vocabulary(token)
        except Exception:
            tags = []
        tags = [tag for tag in tags if tag.get("name") != tag_name]
        await api.patch_contact_tags_vocabulary(token, tags)
        return _tags_tab(tags)

    @app.patch("/settings/contacts/defaults")
    async def save_contact_defaults(request: Request):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        defaults = {
            "default_price_list": form.get("default_price_list") or None,
            "default_payment_terms": form.get("default_payment_terms") or None,
            "default_credit_limit": float(form.get("default_credit_limit") or 0) or None,
        }
        await api.patch_contact_defaults(token, defaults)
        try:
            price_lists = await api.get_price_lists(token)
            terms = await api.get_payment_terms(token)
        except Exception:
            price_lists, terms = [], []
        return _defaults_tab(defaults, price_lists, terms)
