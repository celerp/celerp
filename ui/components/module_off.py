# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The page for a module this company cannot open right now.

A module's own pages, and a core page whose work is done by a module, answer with the
same page: the module is turned off for the company, or the company uses it and it
starts after Celerp restarts. Either way the Modules button is the way forward."""

from __future__ import annotations

from fasthtml.common import A, Div, P, to_xml
from starlette.requests import Request
from starlette.responses import HTMLResponse

from celerp.modules.loader import is_running, module_label
from celerp.modules.registry import uses_module


def can_open(company: dict, module: str) -> bool:
    """Whether the company uses *module* and this process has started it."""
    return uses_module(company.get("settings"), module) and is_running(module)


async def module_off_page(request: Request, module: str, company: dict) -> HTMLResponse | None:
    """The refusal page for *module*, or None when the company can open it."""
    if can_open(company, module):
        return None
    import ui.api_client as api
    from ui.components.shell import base_shell, page_header
    from ui.i18n import t
    from ui.routes.modules_page import manages_modules
    from ui.security import hx_redirect, not_permitted_pending
    if request.headers.get("hx-request"):
        # The full page at this same address is the refusal.
        return hx_redirect(str(request.url.replace(scheme="", netloc="")))
    settings = company.get("settings")
    if uses_module(settings, module):
        title_key, message_key, ask_key = ("modules.starts_after_restart_title", "modules.starts_after_restart",
                                           "modules.ask_admin_to_restart")
    else:
        title_key, message_key, ask_key = ("modules.off_for_company_title", "modules.off_for_company",
                                           "modules.ask_admin_to_turn_on")
    if not_permitted_pending(request):
        # Sent here by a refusal: the caller never asked for this module, so the
        # shell's no-access notice is the whole answer.
        body = ()
    else:
        way_on = (A(t("nav.modules"), href="/modules", cls="btn btn--primary")
                  if manages_modules(api.role_from_company(company)) else P(t(ask_key)))
        body = (page_header(t(title_key)),
                Div(P(t(message_key, module=module_label(module)), cls="flash flash--error"), way_on))
    page = await base_shell(*body, title=t(title_key), request=request, company_settings=settings or {})
    return HTMLResponse(to_xml(page), status_code=403)
