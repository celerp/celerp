# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Manufacturing: company-wide production preferences.

Stored under company.settings["manufacturing"]:
- hours_per_day: converts daily labor lines into the To-Make est-hours column (default 8).
- require_issued_before_complete: block completing a run until its components are issued.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.config import get_token as _token

_DEFAULT_HOURS_PER_DAY = 8.0


def _mfg_settings(company: dict) -> dict:
    return ((company.get("settings") or {}).get("manufacturing") or {})


def setup_routes(app):

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

        form = Form(
            flash("Settings saved.", "success") if saved else "",
            Div(
                Label("Hours per day", For="hours_per_day", cls="form-label"),
                Input(type="number", id="hours_per_day", name="hours_per_day", value=f"{float(hours):g}",
                      min="1", step="any", cls="form-input form-input--sm"),
                P("Used to convert daily labor lines into the estimated-hours column on the To-Make board.",
                  cls="hint"),
                cls="form-group",
            ),
            Div(
                Label(
                    Input(type="checkbox", name="require_issued_before_complete", value="1",
                          checked=require_issued),
                    " Require components issued before completing a run",
                ),
                P("When on, a run cannot be completed until its components have been issued from stock.",
                  cls="hint"),
                cls="form-group",
            ),
            Button("Save", type="submit", cls="btn btn--primary"),
            method="post", action="/settings/manufacturing", cls="form-card",
        )
        return base_shell(
            page_header("Manufacturing Settings"),
            form,
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
