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
from ui.components.phone import phone_head_items as _phone_head_items


# shared helpers imported from settings.py (keep DRY - only ONE copy)
from ui.routes.settings import (
    _check_role,
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


def _danger_zone_section() -> FT:
    """Owner-only danger zone with factory reset."""
    modal_id = "factory-reset-modal"
    step1_id = "factory-reset-step1"
    step2_id = "factory-reset-step2"
    input_id = "factory-reset-confirm-input"
    btn_id = "factory-reset-confirm-btn"

    to_step2_js = (
        f"document.getElementById('{step1_id}').style.display='none';"
        f"document.getElementById('{step2_id}').style.display='block';"
        f"document.getElementById('{input_id}').focus();"
    )
    validate_js = (
        f"document.getElementById('{btn_id}').disabled="
        f"document.getElementById('{input_id}').value !== 'RESET';"
    )
    success_js = (
        f"document.getElementById('{modal_id}').addEventListener('htmx:afterRequest', function(e){{"
        f"if(e.detail.xhr.status===200){{window.location.href='/setup';}}"
        f"}}, {{once:true}});"
    )

    return Div(
        H3("Danger Zone", cls="danger-zone__title"),
        P("Permanently delete all company data and return to the initial setup wizard. "
          "Your installed modules and server settings will be preserved.", cls="danger-zone__desc"),
        Button("Reset All Data",
               type="button",
               cls="btn btn--outline btn--danger",
               onclick=f"document.getElementById('{modal_id}').showModal()"),
        Div(id="reset-flash"),
        Dialog(
            Div(
                Div(
                    H3("Reset all data?", cls="modal-dialog__title"),
                    P("This will permanently delete all business data — contacts, items, "
                      "transactions, documents, and all other records. "
                      "Your app settings and installed modules will be preserved. "
                      "This cannot be undone."),
                    Div(
                        A("Download backup first",
                          href="/backup/export",
                          cls="btn btn--sm btn--ghost",
                          onclick=to_step2_js,
                          download=True),
                        Button("Skip — continue",
                               type="button",
                               cls="btn btn--sm",
                               onclick=to_step2_js),
                        cls="modal-dialog__actions",
                    ),
                    id=step1_id,
                ),
                Div(
                    H3("Type RESET to confirm", cls="modal-dialog__title"),
                    P("This action is irreversible. Type ", Strong("RESET"), " below to confirm."),
                    Input(type="text", id=input_id, placeholder="RESET",
                          autocomplete="off", cls="form-input",
                          oninput=validate_js),
                    Div(
                        Button("Delete everything",
                               type="submit",
                               id=btn_id,
                               cls="btn btn--danger",
                               disabled=True,
                               hx_post="/system/factory-reset",
                               hx_target="#reset-flash",
                               hx_swap="innerHTML",
                               onclick=success_js),
                        Button("Cancel",
                               type="button",
                               cls="btn btn--ghost",
                               onclick=f"document.getElementById('{modal_id}').close()"),
                        cls="modal-dialog__actions",
                    ),
                    id=step2_id,
                    style="display:none",
                ),
                cls="modal-dialog__body",
            ),
            id=modal_id,
            cls="modal-dialog",
        ),
        cls="danger-zone-section",
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
                    api.get_company(token),
                    api.get_users(token),
                    api.get_modules(token),
                    return_exceptions=True,
                )
                # Re-raise 401 so the auth guard below can redirect properly
                for r in results:
                    if isinstance(r, APIError) and r.status == 401:
                        return RedirectResponse("/login", status_code=302)
                company   = results[0] if not isinstance(results[0], Exception) else {}
                users_resp = results[1] if not isinstance(results[1], Exception) else {}
                modules   = results[2] if not isinstance(results[2], Exception) else []
                users = users_resp.get("items", []) if isinstance(users_resp, dict) else []
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
                company_content = _company_tab(company, locations=company_locations, lang=lang, is_owner=is_owner)
                content = Div(company_content, _danger_zone_section()) if is_owner else company_content
            elif tab == "users":
                content = _users_tab(users, lang=lang)
            elif tab == "modules":
                content = _modules_tab(modules, restart_pending=False)
            elif tab == "backup":
                backup_data: dict | None = None
                try:
                    backup_data = await api.get_backup_status(token)
                except Exception:
                    pass
                content = _backup_tab(backup_data=backup_data)
            else:
                try:
                    loc_resp = await api.get_locations(token)
                    company_locations = loc_resp.get("items") or loc_resp.get("locations") or (loc_resp if isinstance(loc_resp, list) else [])
                except Exception:
                    company_locations = []
                content = _company_tab(company, locations=company_locations, lang=lang, is_owner=is_owner)
                if is_owner:
                    content = Div(content, _danger_zone_section())
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
            extra_head=_phone_head_items(),
            request=request,
        )
