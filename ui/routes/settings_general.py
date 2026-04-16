# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → General: Company, Users, Modules, Backup, AI."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.config import COOKIE_NAME, get_role as _get_role
from ui.i18n import t, get_lang
from celerp.services.auth import ROLE_LEVELS as _ROLE_LEVELS

# shared helpers imported from settings.py (keep DRY - only ONE copy)
from ui.routes.settings import (
    _check_role,
    _token,
    _CURRENCIES,
    _CURRENCY_CODES,
    _TIMEZONES,
    _TZ_SEARCH,
    _FISCAL_MONTHS,
    _FISCAL_VALUES,
    _tz_offset_str,
    _company_display_cell,
    _user_display_cell,
    _preference_display_cell,
    _backup_tab,
    _modules_tab,
    _company_tab,
    _users_tab,
    _company_addresses_section,
    _password_form,
)


def _general_tabs(active: str, lang: str = "en", is_admin: bool = True) -> FT:
    tabs: list[tuple[str, str]] = []
    if is_admin:
        tabs += [
            ("company", t("settings.tab_company", lang)),
            ("users", t("settings.tab_users", lang)),
            ("modules", t("settings.tab_modules", lang)),
            ("backup", t("settings.tab_backup", lang)),
        ]
    tabs.append(("password", t("settings.change_password", lang)))
    return Div(
        *[
            A(label, href=f"/settings/general?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _section_breadcrumb(section: str) -> FT:
    return Div(
        A(t("nav.settings"), href="/settings/general", cls="breadcrumb-link"),
        Span(" / ", cls="breadcrumb-sep"),
        Span(section, cls="breadcrumb-current"),
        cls="settings-breadcrumb",
    )


def setup_routes(app):

    @app.get("/settings/general")
    async def settings_general_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        role = _get_role(request)
        is_admin = _ROLE_LEVELS.get(role, 0) >= _ROLE_LEVELS["admin"]
        tab = request.query_params.get("tab", "password" if not is_admin else "company")

        # Non-admins can only access the password tab
        if not is_admin and tab != "password":
            tab = "password"

        lang = get_lang(request)

        if tab == "password":
            content = _password_form(lang=lang)
        else:
            try:
                company = await api.get_company(token)
                users = (await api.get_users(token)).get("items", [])
                modules = await api.get_modules(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                company, users, modules = {}, [], []

            company_locations: list[dict] = []
            if tab in ("company",):
                try:
                    loc_resp = await api.get_locations(token)
                    company_locations = loc_resp.get("items") or loc_resp.get("locations") or (loc_resp if isinstance(loc_resp, list) else [])
                except Exception:
                    company_locations = []

            if tab == "company":
                content = _company_tab(company, locations=company_locations, lang=lang)
            elif tab == "users":
                content = _users_tab(users, lang=lang)
            elif tab == "modules":
                content = _modules_tab(modules, restart_pending=False)
            elif tab == "backup":
                content = _backup_tab()
            else:
                try:
                    loc_resp = await api.get_locations(token)
                    company_locations = loc_resp.get("items") or loc_resp.get("locations") or (loc_resp if isinstance(loc_resp, list) else [])
                except Exception:
                    company_locations = []
                content = _company_tab(company, locations=company_locations, lang=lang)
                tab = "company"

        setup_done = request.query_params.get("setup") == "done"
        setup_banner = Div(
            P(t("settings._setup_complete_your_workspace_is_ready"), cls="setup-done-msg"),
            A(t("settings.dismiss"), href="/settings/general", cls="btn btn--secondary btn--sm"),
            cls="setup-done-banner",
            id="setup-done-banner",
        ) if setup_done else None

        return base_shell(
            _section_breadcrumb("General"),
            page_header(t("page.settings", lang)),
            *([setup_banner] if setup_banner else []),
            Div(
                id="email-warning-banner",
                hx_get="/settings/email-status",
                hx_trigger="load",
                hx_target="this",
                hx_swap="outerHTML",
            ),
            _general_tabs(tab, lang=lang, is_admin=is_admin),
            content,
            title="Settings - Celerp",
            nav_active="settings",
            lang=lang,
            request=request,
        )
