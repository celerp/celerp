# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Modules — top-level admin surface (owner/admin only).

Two tabs:
  - Local Modules: what is on this machine — enable/disable, Import Module
    (zip upload or desktop folder picker), Open Modules Folder, a working
    Restart button, load-error surfacing, and a build-your-own card.
  - Marketplace: the catalog (relay-backed fragment today; the repo-direct
    catalog lands with marketplace M0).

Restart is ONE endpoint for both modes: POST /system/restart writes the
sentinel and SIGTERMs. In desktop mode Electron's restart manager respawns
the servers and reloads the window; in server mode `celerp start` respawns
and the page's poll-and-reload script recovers the browser.
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import get_role as _get_role
from ui.i18n import t, get_lang
from celerp.services.auth import ROLE_LEVELS as _ROLE_LEVELS

from ui.routes.settings import _token

_TEMPLATE_REPO = "https://github.com/celerp/celerp-module-template"
_DOCS_URL = "https://celerp.com/docs/modules.html"


def _is_admin(request: Request) -> bool:
    return _ROLE_LEVELS.get(_get_role(request), 0) >= _ROLE_LEVELS["admin"]


def _modules_dir_display() -> str:
    import os
    raw = os.environ.get("MODULE_DIR", "")
    return raw.split(",")[0].strip()


def _restart_pending(modules: list[dict]) -> bool:
    """Derived, not transient: a module is enabled but not running (and not a
    load failure that a restart cannot fix by itself — those still need a
    restart after the module is repaired, so they count too)."""
    return any(m.get("enabled") and not m.get("running") for m in modules)


# ── tabs chrome ────────────────────────────────────────────────────────────────

def _tabs(active: str, lang: str) -> FT:
    items = [
        ("local", t("modules.tab_local", lang)),
        ("marketplace", t("modules.tab_marketplace", lang)),
    ]
    return Div(
        *[A(label, href=f"/modules?tab={key}",
            cls=f"tab {'tab--active' if key == active else ''}")
          for key, label in items],
        cls="settings-tabs",
    )


# ── local modules panel ────────────────────────────────────────────────────────

def _local_panel(modules: list[dict], lang: str = "en",
                 flash_text: str | None = None, flash_error: bool = False) -> FT:
    enabled_names = {m["name"] for m in modules if m.get("enabled") or m.get("running")}
    required_by: dict[str, list[str]] = {}
    for m in modules:
        if not (m.get("enabled") or m.get("running")):
            continue
        for dep in (m.get("depends_on") or []):
            required_by.setdefault(dep, []).append(m.get("label") or m["name"])

    rows = []
    for m in modules:
        name = m["name"]
        label = m.get("label") or name
        version = m.get("version", "")
        description = m.get("description", "")
        author = m.get("author", "")
        enabled = bool(m.get("enabled"))
        running = bool(m.get("running"))
        load_error = m.get("load_error")
        effectively_enabled = enabled or running

        status_parts = []
        if running:
            status_parts.append(Span("running", cls="badge badge--green"))
        elif enabled and load_error:
            # A broken module fails loudly, not silently.
            status_parts.append(Span(t("modules.badge_failed", lang), cls="badge badge--red"))
            status_parts.append(Div(load_error, cls="text-muted small module-load-error"))
        elif enabled:
            status_parts.append(Span(t("settings.restart_needed", lang), cls="badge badge--yellow"))
        else:
            status_parts.append(Span("disabled", cls="badge badge--grey"))

        dependents = required_by.get(name, [])
        if effectively_enabled:
            if dependents:
                toggle_btn = Button(t("btn.disable", lang),
                    title=f"Required by: {', '.join(dependents)}",
                    disabled=True,
                    cls="btn btn--sm btn--danger btn--disabled",
                )
            else:
                toggle_btn = Button(t("btn.disable", lang),
                    hx_post=f"/modules/{name}/disable",
                    hx_target="#local-modules-panel",
                    hx_swap="outerHTML",
                    cls="btn btn--sm btn--danger",
                )
        else:
            toggle_btn = Button(t("btn.enable", lang),
                hx_post=f"/modules/{name}/enable",
                hx_target="#local-modules-panel",
                hx_swap="outerHTML",
                cls="btn btn--sm btn--primary",
            )

        rows.append(Tr(
            Td(Div(Strong(label), Div(description, cls="text-muted small") if description else "", cls="module-name-cell")),
            Td(f"v{version}" if version and version != "unknown" else ""),
            Td(author),
            Td(*status_parts),
            Td(toggle_btn),
        ))

    # Derived restart banner, with a button that actually restarts.
    banner = Div(
        Span(t("settings._a_restart_is_required_for_module_changes_to_take", lang)),
        Button(t("btn.restart_now", lang),
            hx_post="/modules/restart",
            hx_target="#local-modules-panel",
            hx_swap="outerHTML",
            cls="btn btn--sm btn--primary",
            style="margin-left:12px;",
        ),
        id="modules-restart-banner",
        cls="error-banner mb-md",
    ) if _restart_pending(modules) else Div(id="modules-restart-banner")

    flash_div = Div(
        flash_text,
        cls=f"flash {'flash--error' if flash_error else 'flash--success'}",
    ) if flash_text else ""

    mdir = _modules_dir_display()

    import_section = Details(
        Summary(t("btn.import_module", lang), cls="btn btn--sm btn--secondary", id="import-module-summary"),
        Div(
            P(t("modules.import_warning", lang), cls="text-muted small"),
            Form(
                Input(type="file", name="file", accept=".zip", required=True),
                Button(t("btn.import_module", lang), type="submit", cls="btn btn--sm btn--primary"),
                hx_post="/modules/import",
                hx_encoding="multipart/form-data",
                hx_target="#local-modules-panel",
                hx_swap="outerHTML",
                cls="module-import-form",
            ),
            Button(t("btn.choose_folder", lang),
                id="import-folder-btn",
                type="button",
                cls="btn btn--sm btn--secondary",
                style="display:none;margin-top:6px;",
            ),
            cls="module-import-body",
        ),
        id="module-import",
        cls="module-import mt-md",
    )

    folder_row = Div(
        Span(t("modules.folder_label", lang) + " ", cls="text-muted small"),
        Code(mdir or "-", dir="ltr"),
        Button(t("btn.open_modules_folder", lang),
            id="open-modules-folder-btn",
            type="button",
            cls="btn btn--sm btn--secondary",
            style="display:none;margin-left:10px;",
        ),
        cls="mt-md",
    ) if mdir else Div()

    if not rows:
        content = Div(
            P(t("modules.empty_state", lang), cls="text-muted"),
            cls="modules-empty",
        )
    else:
        content = Table(
            Thead(Tr(Th(t("th.module", lang)), Th(t("th.version", lang)), Th(t("th.author", lang)), Th(t("th.status", lang)), Th(""))),
            Tbody(*rows),
            cls="data-table",
        )

    build_card = Div(
        H3(t("modules.build_title", lang), cls="section-title mt-lg"),
        P(t("modules.build_body", lang), cls="text-muted mb-sm"),
        Div(
            A(t("modules.build_template_link", lang), href=_TEMPLATE_REPO, target="_blank", rel="noopener", cls="btn btn--sm btn--secondary"),
            A(t("modules.build_docs_link", lang), href=_DOCS_URL, target="_blank", rel="noopener", cls="btn btn--sm btn--secondary", style="margin-left:8px;"),
            cls="mf-btns",
        ),
        cls="module-build-card",
    )

    # Desktop-only affordances light up when the Electron bridge is present.
    desktop_js = Script("""
    (function () {
      var open = document.getElementById('open-modules-folder-btn');
      if (open && window.celerp && window.celerp.openModulesFolder) {
        open.style.display = '';
        open.onclick = function () { window.celerp.openModulesFolder(); };
      }
      var pick = document.getElementById('import-folder-btn');
      if (pick && window.celerp && window.celerp.pickModuleFolder) {
        pick.style.display = '';
        pick.onclick = function () {
          window.celerp.pickModuleFolder().then(function (p) {
            if (!p) return;
            fetch('/modules/import-path', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({path: p}),
            }).then(function () { window.location = '/modules'; });
          });
        };
      }
      if (new URLSearchParams(window.location.search).get('import') === '1') {
        var d = document.getElementById('module-import');
        if (d) { d.open = true; d.scrollIntoView(); }
      }
    })();
    """)

    return Div(
        banner,
        flash_div,
        content,
        import_section,
        folder_row,
        build_card,
        desktop_js,
        id="local-modules-panel",
        cls="settings-card",
    )


def _restarting_panel(lang: str) -> FT:
    """Shown right after POST /system/restart. Desktop: Electron reloads the
    window itself once the servers are back. Server mode: this script polls
    the same origin and reloads when the UI answers again."""
    return Div(
        P(t("modules.restarting", lang), cls="text-muted"),
        Script("""
        setTimeout(function poll() {
          fetch('/', {cache: 'no-store'}).then(function (r) {
            if (r.ok) { window.location = '/modules'; } else { setTimeout(poll, 1500); }
          }).catch(function () { setTimeout(poll, 1500); });
        }, 2500);
        """),
        id="local-modules-panel",
        cls="settings-card",
    )


# ── routes ─────────────────────────────────────────────────────────────────────

def setup_routes(app):

    async def _guard(request: Request):
        token = _token(request)
        if not token:
            return None, RedirectResponse("/login", status_code=302)
        if not _is_admin(request):
            return None, RedirectResponse("/dashboard", status_code=302)
        return token, None

    @app.get("/modules")
    async def modules_page(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        tab = request.query_params.get("tab", "local")

        if tab == "marketplace":
            content = Div(
                H3(t("page.explore_marketplace", lang), cls="section-title"),
                P(t("settings.browse_premium_and_community_modules_available_for", lang), cls="text-muted mb-sm"),
                Button(t("btn.load_available_modules", lang),
                    hx_get="/modules/marketplace-panel",
                    hx_target="#marketplace-panel",
                    hx_swap="outerHTML",
                    hx_indicator="#mkt-loading",
                    cls="btn btn--sm btn--secondary",
                ),
                Span(" ", id="mkt-loading", cls="htmx-indicator text-muted"),
                Div(id="marketplace-panel"),
                cls="settings-card",
            )
        else:
            tab = "local"
            try:
                modules = await api.get_modules(token)
            except APIError as e:
                if e.status == 401:
                    return RedirectResponse("/login", status_code=302)
                modules = []
            content = _local_panel(modules, lang=lang)

        return base_shell(
            page_header(t("modules.title", lang)),
            _tabs(tab, lang),
            content,
            title="Modules - Celerp",
            nav_active="modules",
            lang=lang,
            request=request,
        )

    @app.post("/modules/{module_name}/enable")
    async def module_enable(request: Request, module_name: str):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        try:
            await api.enable_module(token, module_name)
            modules = await api.get_modules(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            modules = []
        return _local_panel(modules, lang=lang)

    @app.post("/modules/{module_name}/disable")
    async def module_disable(request: Request, module_name: str):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        try:
            await api.disable_module(token, module_name)
            modules = await api.get_modules(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            modules = []
        return _local_panel(modules, lang=lang)

    @app.post("/modules/import")
    async def module_import(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        form = await request.form()
        file_field = form.get("file")
        flash_text, flash_error = None, False
        if file_field is None:
            flash_text, flash_error = t("msg.no_file_selected", lang), True
        else:
            import asyncio
            data = await asyncio.to_thread(file_field.file.read)
            try:
                info = await api.import_module_zip(token, file_field.filename or "module.zip", data)
                flash_text = t("modules.import_success", lang, name=info.get("display_name") or info.get("name", ""))
            except APIError as e:
                flash_text, flash_error = e.detail or str(e), True
        try:
            modules = await api.get_modules(token)
        except APIError:
            modules = []
        return _local_panel(modules, lang=lang, flash_text=flash_text, flash_error=flash_error)

    @app.post("/modules/import-path")
    async def module_import_path(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        body = await request.json()
        try:
            info = await api.import_module_path(token, str(body.get("path", "")))
            return JSONResponse({"ok": True, **info})
        except APIError as e:
            return JSONResponse({"ok": False, "detail": e.detail or str(e)}, status_code=422)

    @app.post("/modules/restart")
    async def module_restart(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        try:
            await api.restart_system(token)
        except APIError as e:
            modules = []
            try:
                modules = await api.get_modules(token)
            except APIError:
                pass
            return _local_panel(modules, lang=lang, flash_text=e.detail or str(e), flash_error=True)
        return _restarting_panel(lang)

    @app.get("/modules/marketplace-panel")
    async def marketplace_panel(request: Request):
        """HTMX fragment: fetch and render available marketplace modules."""
        import httpx
        from ui.config import RELAY_URL

        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="marketplace-panel")
        lang = get_lang(request)
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{RELAY_URL}/marketplace/modules")
                modules_list = (r.json().get("items") or []) if r.status_code == 200 else []
        except Exception:
            return Div(
                P(t("settings.could_not_reach_the_celerp_marketplace_check_your", lang), cls="text-muted"),
                id="marketplace-panel",
            )

        installed = set()
        try:
            installed = {m["name"] for m in await api.get_modules(token)}
        except Exception:
            pass

        if not modules_list:
            return Div(
                P(t("settings.no_modules_available_in_the_marketplace_yet", lang), cls="text-muted"),
                id="marketplace-panel",
            )

        rows = []
        for m in modules_list:
            slug = m.get("slug", "")
            price = m.get("price_monthly")
            already = slug in installed
            install_btn = (
                Span(t("settings.installed", lang), cls="badge badge--green")
                if already
                else A(t("settings.view_install", lang),
                    href=f"https://celerp.com/marketplace/{slug}",
                    target="_blank",
                    cls="btn btn--sm btn--primary",
                )
            )
            rows.append(Tr(
                Td(Div(Strong(m.get("display_name", slug)), Div(m.get("description", ""), cls="text-muted small"), cls="module-name-cell")),
                Td(f"v{m.get('latest_version', '')}"),
                Td(m.get("author", "")),
                Td(f"${price:.2f}/mo" if price else "Free"),
                Td(install_btn),
            ))
        return Div(
            Table(
                Thead(Tr(Th(t("th.module", lang)), Th(t("th.version", lang)), Th(t("th.author", lang)), Th(t("th.price", lang)), Th(""))),
                Tbody(*rows),
                cls="data-table",
            ),
            id="marketplace-panel",
        )
