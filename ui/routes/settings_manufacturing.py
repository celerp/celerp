# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Manufacturing: company-wide production preferences + work centers.

Stored under company.settings["manufacturing"]:
- hours_per_day: converts daily labor lines into the To-Make est-hours column (default 8).
- require_issued_before_complete: block completing a run until its components are issued.

Work centers (operational stations) are master data, configured here rather than as a top-level nav.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.config import get_token as _token
from ui.routes.manufacturing import _wc_table

_DEFAULT_HOURS_PER_DAY = 8.0


def _mfg_settings(company: dict) -> dict:
    return ((company.get("settings") or {}).get("manufacturing") or {})


def _info(text: str) -> FT:
    """Small info-icon tooltip (hover/focus reveals the explanation)."""
    return Span("ⓘ", cls="info-tip", tabindex="0", role="img",
                **{"aria-label": text, "data-tip": text})


def setup_routes(app):

    async def _work_centers_section(token: str) -> FT:
        centers, locations = [], []
        try:
            centers = (await api.list_work_centers(token)).get("items", [])
            locations = (await api.get_locations(token)).get("items", [])
        except APIError:
            pass
        loc_names = {l.get("id"): l.get("name") for l in locations}
        return Div(
            Div(
                H2(Span("Work centers"),
                   _info("Operational stations where production happens (e.g. Bench, Polishing, Oven). "
                         "Optional: assign a WIP location, labour rate and capacity. Double-click a cell to edit."),
                   cls="section-title"),
                Button("Add work center", type="button", cls="btn btn--sm btn--primary",
                       hx_post="/manufacturing/work-centers/new", hx_target="#wc-table", hx_swap="outerHTML"),
                cls="flex-row flex-between settings-card-header",
            ),
            _wc_table(centers, loc_names),
            cls="settings-card", id="work-centers",
        )

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
        hours = mfg.get("hours_per_day", _DEFAULT_HOURS_PER_DAY)
        require_issued = bool(mfg.get("require_issued_before_complete"))
        saved = request.query_params.get("saved") == "1"

        prefs = Form(
            flash("Settings saved.", "success") if saved else "",
            H2("Production rules", cls="section-title"),
            P("How production runs behave for this company.", cls="settings-hint"),
            Div(
                Label(
                    Input(type="checkbox", name="require_issued_before_complete", value="1",
                          checked=require_issued),
                    Span(" Require components to be issued before a run can be completed"),
                    _info("When on, a production run cannot be marked complete until its raw materials "
                          "have been issued (consumed) from stock - so finished output is only booked "
                          "after the inputs are actually used. Leave off if you reconcile materials later."),
                    cls="settings-toggle",
                ),
                cls="form-group",
            ),
            Div(
                Label(Span("Hours per day"),
                      _info("Used to convert a recipe's daily labour lines into the estimated-hours "
                            "column on the To-Make board. Default 8."),
                      For="hours_per_day", cls="form-label"),
                Input(type="number", id="hours_per_day", name="hours_per_day", value=f"{float(hours):g}",
                      min="1", step="any", cls="form-input input--narrow"),
                cls="form-group",
            ),
            Button("Save", type="submit", cls="btn btn--primary"),
            method="post", action="/settings/manufacturing", cls="settings-card",
        )

        return base_shell(
            page_header("Manufacturing Settings"),
            prefs,
            await _work_centers_section(token),
            title="Manufacturing Settings - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/settings/manufacturing")
    async def settings_manufacturing_save(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        try:
            hours = float(str(form.get("hours_per_day") or _DEFAULT_HOURS_PER_DAY))
        except ValueError:
            hours = _DEFAULT_HOURS_PER_DAY
        if hours <= 0:
            hours = _DEFAULT_HOURS_PER_DAY
        require_issued = str(form.get("require_issued_before_complete") or "") in ("1", "on", "true")
        try:
            await api.update_mfg_settings(token, {
                "hours_per_day": hours,
                "require_issued_before_complete": require_issued,
            })
        except APIError:
            pass
        return RedirectResponse("/settings/manufacturing?saved=1", status_code=303)
