# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Manufacturing: company-wide production preferences + work centers.

Stored under company.settings["manufacturing"]:
- require_issued_before_complete: block completing a run until its components are issued.
- auto_create_work_orders: create a work order per manufacturable line when an order is finalized.
- auto_complete_work_orders: also complete each auto-created work order on the spot (consume
  components, produce finished goods, post the completion journal entry). Applies only when
  auto_create_work_orders is on.

Work centers (operational stations) are master data, configured here rather than as a top-level nav.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, page_title, toast_header
from ui.config import get_token as _token
from ui.i18n import t
from ui.routes.manufacturing import _wc_table


def _mfg_settings(company: dict) -> dict:
    return ((company.get("settings") or {}).get("manufacturing") or {})


def _info(text: str) -> FT:
    """Small info-icon tooltip (hover/focus reveals the explanation)."""
    return Span("ⓘ", cls="info-tip", tabindex="0", role="img",
                **{"aria-label": text, "data-tip": text})


def _work_centers_card(centers: list, loc_names: dict) -> FT:
    """Work-centers settings card. Chrome resolves via t() at render time."""
    return Div(
        Div(
            H2(Span(t("settings_manufacturing.work_centers")),
               _info(t("settings_manufacturing.work_centers_info")),
               cls="section-title"),
            Button(t("settings_manufacturing.add_work_center"), type="button", cls="btn btn--sm btn--primary",
                   hx_post="/manufacturing/work-centers/new", hx_target="#wc-table", hx_swap="outerHTML"),
            cls="flex-row flex-between settings-card-header",
        ),
        _wc_table(centers, loc_names),
        cls="settings-card", id="work-centers",
    )


def _production_rules_form(require_issued: bool, auto_create: bool, auto_complete: bool) -> FT:
    """Production-rules preferences form. Headings, toggle labels and help text
    resolve via t() at render time."""
    return Form(
        H2(t("settings_manufacturing.production_rules"), cls="section-title"),
        P(t("settings_manufacturing.production_rules_hint"), cls="settings-hint"),
        Div(
            Label(
                Input(type="checkbox", name="require_issued_before_complete", value="1",
                      checked=require_issued),
                Span(t("settings_manufacturing.require_issued_label")),
                _info(t("settings_manufacturing.require_issued_info")),
                cls="settings-toggle",
            ),
            cls="form-group",
        ),
        Div(
            Label(
                Input(type="checkbox", name="auto_create_work_orders", value="1", checked=auto_create,
                      onchange="document.getElementById('auto-complete-row').style.display = this.checked ? '' : 'none'"),
                Span(t("settings_manufacturing.auto_create_label")),
                _info(t("settings_manufacturing.auto_create_info")),
                cls="settings-toggle",
            ),
            cls="form-group",
        ),
        Div(
            Label(
                Input(type="checkbox", name="auto_complete_work_orders", value="1", checked=auto_complete),
                Span(t("settings_manufacturing.auto_complete_label")),
                _info(t("settings_manufacturing.auto_complete_info")),
                cls="settings-toggle",
            ),
            cls="form-group settings-toggle-child", id="auto-complete-row",
            **({"style": "display:none"} if not auto_create else {}),
        ),
        Div(
            P(t("settings_manufacturing.per_workcenter_hours_note"), cls="form-hint"),
            cls="form-group",
        ),
        hx_post="/settings/manufacturing", hx_trigger="change",
        hx_swap="none", cls="settings-card",
    )


def setup_routes(app):

    async def _work_centers_section(token: str) -> FT:
        centers, locations = [], []
        try:
            centers = (await api.list_work_centers(token)).get("items", [])
            locations = (await api.get_locations(token)).get("items", [])
        except APIError:
            pass
        loc_names = {l.get("id"): l.get("name") for l in locations}
        return _work_centers_card(centers, loc_names)

    @app.get("/settings/manufacturing")
    async def settings_manufacturing_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        mfg = _mfg_settings(company)
        require_issued = bool(mfg.get("require_issued_before_complete"))
        auto_create = bool(mfg.get("auto_create_work_orders"))
        auto_complete = bool(mfg.get("auto_complete_work_orders"))

        prefs = _production_rules_form(require_issued, auto_create, auto_complete)

        return await base_shell(
            page_header(t("settings_manufacturing.page_header")),
            prefs,
            await _work_centers_section(token),
            title=page_title("settings_manufacturing.page_header"),
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/settings/manufacturing")
    async def settings_manufacturing_save(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        require_issued = str(form.get("require_issued_before_complete") or "") in ("1", "on", "true")
        auto_create = str(form.get("auto_create_work_orders") or "") in ("1", "on", "true")
        auto_complete = str(form.get("auto_complete_work_orders") or "") in ("1", "on", "true")
        try:
            await api.update_mfg_settings(token, {
                "require_issued_before_complete": require_issued,
                "auto_create_work_orders": auto_create,
                "auto_complete_work_orders": auto_complete,
            })
        except APIError:
            return Response("", headers=toast_header(t("settings_manufacturing.save_failed"), "error"))
        return Response("", headers=toast_header(t("settings_manufacturing.settings_saved")))
