# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Purchasing: Taxes, Payment Terms, Doc Numbering."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import COOKIE_NAME
from ui.i18n import t

from ui.routes.settings import _token, _check_role, _taxes_tab, _terms_conditions_tab
from ui.routes.settings_general import _section_breadcrumb
from ui.routes.settings_sales import _numbering_tab

_PURCHASING_DOC_TYPES = frozenset({"purchase_order", "bill", "consignment_in"})


def _purchasing_tabs(active: str, lang: str = "en") -> FT:
    tabs: list[tuple[str, str]] = [
        ("taxes", t("settings.tab_taxes", lang)),
        ("terms-conditions", "Terms & Conditions"),
        ("numbering", "Numbering"),
    ]
    return Div(
        *[
            A(label, href=f"/settings/purchasing?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def setup_routes(app):

    @app.get("/settings/purchasing")
    async def settings_purchasing_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        tab = request.query_params.get("tab", "taxes")
        lang = request.cookies.get("celerp_lang", "en")
        try:
            purchasing_taxes = await api.get_purchasing_taxes(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            purchasing_taxes = []

        if tab == "taxes":
            content = _taxes_tab(purchasing_taxes, lang=lang, prefix="purchasing-taxes", import_path=None)
        elif tab == "terms-conditions":
            try:
                tc_templates = await api.get_terms_conditions(token)
            except (APIError, Exception):
                tc_templates = []
            content = _terms_conditions_tab(tc_templates, prefix="purchasing-terms-conditions", scope="purchasing")
        elif tab == "numbering":
            try:
                sequences = await api.get_doc_sequences(token)
            except (APIError, Exception):
                sequences = []
            content = _numbering_tab([s for s in sequences if s.get("doc_type") in _PURCHASING_DOC_TYPES])
        else:
            content = _taxes_tab(purchasing_taxes, lang=lang, prefix="purchasing-taxes", import_path=None)
            tab = "taxes"

        return base_shell(
            _section_breadcrumb("Purchasing"),
            page_header("Purchasing Documents Settings"),
            _purchasing_tabs(tab, lang=lang),
            content,
            title="Purchasing Settings - Celerp",
            nav_active="settings",
            lang=lang,
            request=request,
        )
