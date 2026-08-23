# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → General: Company, Users, Modules, Backup, AI."""

from __future__ import annotations


from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash, page_title
from ui.config import COOKIE_NAME, get_role as _get_role
from ui.i18n import t, get_lang
from celerp.services.permissions import role_has_permission
from ui.components.phone import phone_head_items as _phone_head_items


# shared helpers imported from settings.py (keep DRY - only ONE copy)
from ui.routes.settings import (
    _token,
    _TIMEZONES,
    _TZ_SEARCH,
    _FISCAL_MONTHS,
    _FISCAL_VALUES,
    _tz_offset_str,
    _company_display_cell,
    _user_display_cell,
    _preference_display_cell,
    _backup_tab,
    _company_tab,
    _users_tab,
    _password_form,
)


def _general_tabs(active: str, lang: str = "en", is_admin: bool = True) -> FT:
    tabs: list[tuple[str, str]] = []
    if is_admin:
        tabs += [
            ("company", t("settings.tab_company", lang)),
            ("users", t("settings.tab_users", lang)),
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



def _section_breadcrumb(section_key: str) -> FT:
    return Div(
        A(t("nav.settings"), href="/settings/general", cls="breadcrumb-link"),
        Span(" / ", cls="breadcrumb-sep"),
        Span(t(section_key), cls="breadcrumb-current"),
        cls="settings-breadcrumb",
    )


def setup_routes(app):

    @app.get("/settings/general")
    async def settings_general_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        role = _get_role(request)
        try:
            company = await api.get_company(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            company = {}
        settings = company.get("settings") if isinstance(company, dict) else {}
        is_admin = role_has_permission(settings or {}, role, "manage_company_settings")
        is_owner = role == "owner"
        tab = request.query_params.get("tab", "password" if not is_admin else "company")

        # Non-admins can only access the password tab
        if not is_admin and tab != "password":
            tab = "password"

        lang = get_lang(request)

        if tab == "password":
            content = _password_form(lang=lang)
        else:
            try:
                import asyncio as _asyncio
                results = await _asyncio.gather(
                    api.get_users(token),
                    return_exceptions=True,
                )
                # Re-raise 401 so the auth guard below can redirect properly
                for r in results:
                    if isinstance(r, APIError) and r.status == 401:
                        return RedirectResponse("/login", status_code=302)
                users_resp = results[0] if not isinstance(results[0], Exception) else {}
                users = users_resp.get("items", []) if isinstance(users_resp, dict) else []
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                users = []

            if tab == "company":
                content = _company_tab(company, lang=lang, is_owner=is_owner)
            elif tab == "users":
                content = _users_tab(users, company.get("settings"), lang=lang, is_owner=is_owner)
            elif tab == "backup":
                backup_data: dict | None = None
                try:
                    backup_data = await api.get_backup_status(token)
                except Exception:
                    pass
                content = _backup_tab(backup_data=backup_data)
            else:
                content = _company_tab(company, lang=lang, is_owner=is_owner)
                if is_owner:
                    tab = "company"

        setup_done = request.query_params.get("setup") == "done"
        setup_banner = Div(
            P(t("settings._setup_complete_your_workspace_is_ready"), cls="setup-done-msg"),
            A(t("settings.dismiss"), href="/settings/general", cls="btn btn--secondary btn--sm"),
            cls="setup-done-banner",
            id="setup-done-banner",
        ) if setup_done else None

        return await base_shell(
            _section_breadcrumb("settings_general.breadcrumb_general"),
            page_header(t("page.settings", lang)),
            *([setup_banner] if setup_banner else []),
            _general_tabs(tab, lang=lang, is_admin=is_admin),
            content,
            title=page_title("page.settings"),
            nav_active="settings",
            lang=lang,
            extra_head=_phone_head_items(),
            request=request,
        )
