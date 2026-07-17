# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Modules — top-level admin surface (owner/admin only).

Two tabs:
  - Local Modules: what is on this machine — enable/disable, Import Module
    (zip upload or desktop folder picker), Open Modules Folder, a working
    Restart button, load-error surfacing, and a build-your-own card.
  - Marketplace: the catalog (community-modules index.json, public data),
    served via the relay with repo-direct and local-cache fallbacks; see
    ui.marketplace_catalog for why the relay endpoint is the one baked-in URL.
    Official and verified modules show first; community listings sit behind a
    one-time trust acknowledgment and always carry the grey unverified icon.

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
import ui.marketplace_catalog as catalog
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
    """Derived, not transient: a restart is pending whenever a module's desired
    state (enabled) differs from its actual state (running). This covers both
    directions - a newly enabled module not yet running, AND a just-disabled
    module still loaded until the next restart (the disable case the old
    enabled-and-not-running check missed).

    Core-folded default modules (ai/backup/connectors) report running=True
    regardless of the enabled flag, so an admin toggling one would otherwise
    pin a false banner forever; they are excluded."""
    return any(
        not m.get("is_default") and bool(m.get("enabled")) != bool(m.get("running"))
        for m in modules
    )


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
            }).then(function (r) { return r.text(); }).then(function (html) {
              var panel = document.getElementById('local-modules-panel');
              if (panel) { panel.outerHTML = html; }  // fragment swap, no reload
            });
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
        P(t("modules.restart_timeout", lang), cls="flash flash--warning",
          id="restart-timeout", style="display:none;"),
        Script("""
        (function () {
          var tries = 0, MAX = 40;  // ~60s, then stop and tell the user
          setTimeout(function poll() {
            if (tries++ >= MAX) {
              document.getElementById('restart-timeout').style.display = '';
              return;
            }
            fetch('/', {cache: 'no-store'}).then(function (r) {
              if (r.ok) { window.location = '/modules'; } else { setTimeout(poll, 1500); }
            }).catch(function () { setTimeout(poll, 1500); });
          }, 2500);
        })();
        """),
        id="local-modules-panel",
        cls="settings-card",
    )


# ── routes ─────────────────────────────────────────────────────────────────────

# One trust icon, two states (decided: as lean as possible). Black = a review
# happened (verified/official), grey = it did not (community). The tooltip and
# aria-label carry the words; the luminance difference carries the glance.
_SHIELD_SVG = (
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" '
    'aria-hidden="true"><path d="M12 2l8 3v6c0 5-3.4 9.2-8 11-4.6-1.8-8-6-8-11V5l8-3z"/></svg>'
)


def _trust_icon(tier: str, lang: str):
    if tier == "community":
        tip, state = t("marketplace.unverified_tip", lang), "unverified"
    elif tier == "official":
        tip, state = t("marketplace.official_tip", lang), "trusted"
    else:
        tip, state = t("marketplace.verified_tip", lang), "trusted"
    return Span(NotStr(_SHIELD_SVG), cls=f"trust-icon trust-icon--{state}",
                title=tip, aria_label=tip, role="img")


def _catalog_price(m: dict, lang: str) -> str:
    if m.get("price_monthly"):
        return f"${m['price_monthly']:g}/mo"
    if m.get("price_once"):
        return f"${m['price_once']:g}"
    return t("marketplace.free", lang)


def _catalog_card(m: dict, lang: str, installed: set[str]):
    """One catalog entry: summary row, detail drawer with disclosures first."""
    tier = m["tier"]
    body = []
    body.append(P(m["description"], cls="text-muted small", style="margin-top:8px;"))
    # Disclosures first (what it touches, what it calls), labeled self-declared
    # for community listings - the index copy is the author's own statement.
    declared = f' ({t("marketplace.self_declared", lang)})' if tier == "community" else ""
    if m.get("data_access"):
        body.append(P(Strong(t("marketplace.data_access", lang)), f"{declared}: ", m["data_access"], cls="small"))
    if m.get("network_calls"):
        body.append(P(Strong(t("marketplace.network_calls", lang)), f"{declared}: ", m["network_calls"], cls="small"))
    body.append(P(Strong(t("th.license", lang)), ": ", m["license"], cls="small"))
    # Catalog URLs are author-controlled (community listings); every external
    # link opens with rel=noopener noreferrer so the opened tab cannot reach
    # back through window.opener.
    links = []
    if m.get("repo"):
        links.append(A(t("marketplace.view_source", lang), href=m["repo"], target="_blank", rel="noopener noreferrer"))
        links.append(A(t("marketplace.report_bug", lang), href=m["repo"].rstrip("/") + "/issues", target="_blank", rel="noopener noreferrer"))
    links.append(A(t("marketplace.feedback", lang),
                   href=m.get("feedback") or "https://github.com/celerp/community-modules/discussions",
                   target="_blank", rel="noopener noreferrer"))
    body.append(Div(*links, cls="marketplace-card__links"))
    # CTA per tier. One-click install of vaulted artifacts is the verified
    # milestone; today official links out and community hands off to Import.
    if m["id"] in installed:
        body.append(Span(t("settings.installed", lang), cls="badge badge--green"))
    elif tier == "community":
        body.append(Div(
            A(t("marketplace.get_from_author", lang), href=m.get("repo", "#"),
              target="_blank", rel="noopener noreferrer", cls="btn btn--sm btn--secondary"),
            A(t("btn.import_module", lang), href="/modules?import=1",
              cls="btn btn--sm btn--secondary"),
            style="display:flex;gap:8px;",
        ))
    else:
        body.append(A(t("settings.view_install", lang),
                      href=m.get("homepage") or f"https://celerp.com/marketplace/{m['id']}",
                      target="_blank", rel="noopener noreferrer", cls="btn btn--sm btn--primary"))
    return Details(
        Summary(
            _trust_icon(tier, lang),
            Strong(m["name"]),
            Span(m["author"], cls="text-muted small"),
            Span(_catalog_price(m, lang), cls="marketplace-card__price"),
        ),
        Div(*body, cls="marketplace-card__body"),
        cls="marketplace-card",
    )


def _community_zone(n: int, lang: str):
    """The collapsed community row. The grey world stays one click away."""
    if not n:
        return Div(id="community-zone")
    return Div(
        Button(t("marketplace.show_community", lang, n=n),
            hx_get="/modules/community-panel",
            hx_target="#community-zone", hx_swap="outerHTML",
            cls="btn btn--sm btn--secondary", style="margin-top:12px;"),
        id="community-zone",
    )


def _cached_community() -> list[dict]:
    cached = catalog.read_cached() or []
    return [m for m in cached if m["tier"] == "community"]


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
                Div(
                    Span(t("modules.loading_catalog", lang), cls="text-muted small"),
                    hx_get="/modules/marketplace-panel",
                    hx_trigger="load",
                    hx_swap="outerHTML",
                    id="marketplace-panel",
                ),
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
            from celerp.modules.importer import MAX_ARCHIVE_BYTES
            # Cap the read: don't buffer an oversize upload whole before the
            # backend gets to reject it (the backend caps again independently).
            data = await asyncio.to_thread(file_field.file.read, MAX_ARCHIVE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_BYTES:
                flash_text, flash_error = t("modules.import_too_large", lang), True
            else:
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
        lang = get_lang(request)
        body = await request.json()
        flash_text, flash_error = None, False
        try:
            info = await api.import_module_path(token, str(body.get("path", "")))
            flash_text = t("modules.import_success", lang,
                           name=info.get("display_name") or info.get("name", ""))
        except APIError as e:
            flash_text, flash_error = e.detail or str(e), True
        try:
            modules = await api.get_modules(token)
        except APIError:
            modules = []
        # Return the panel fragment (not JSON) so the desktop folder-pick swaps
        # in place, matching the zip upload path - no full page reload.
        return _local_panel(modules, lang=lang, flash_text=flash_text, flash_error=flash_error)

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
        """HTMX fragment: the catalog, fetched repo-direct and cached."""
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="marketplace-panel")
        lang = get_lang(request)
        try:
            modules_list, from_cache = await catalog.fetch_catalog()
        except Exception:
            return Div(
                P(t("marketplace.could_not_load", lang), cls="text-muted"),
                id="marketplace-panel",
            )

        installed = set()
        try:
            installed = {m["name"] for m in await api.get_modules(token)}
        except APIError:
            pass

        trusted = [m for m in modules_list if m["tier"] in ("official", "verified")]
        community = [m for m in modules_list if m["tier"] == "community"]

        children = []
        if from_cache:
            children.append(Div(t("marketplace.from_cache", lang), cls="flash flash--warning"))
        if not modules_list:
            children.append(P(t("settings.no_modules_available_in_the_marketplace_yet", lang), cls="text-muted"))
        children.extend(_catalog_card(m, lang, installed) for m in trusted)
        children.append(_community_zone(len(community), lang))
        children.append(P(t("marketplace.footer_disclaimer", lang), cls="text-muted small", style="margin-top:12px;"))
        return Div(*children, id="marketplace-panel")

    @app.get("/modules/community-panel")
    async def community_panel(request: Request):
        """HTMX fragment: reveal community listings (after the one-time ack)."""
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="community-zone")
        lang = get_lang(request)
        community = _cached_community()
        if request.query_params.get("hide"):
            return _community_zone(len(community), lang)
        if not catalog.community_acked():
            # First reveal: the trust warning, acknowledged explicitly.
            return Div(
                P(t("modules.import_warning", lang), cls="small"),
                Label(
                    Input(type="checkbox", id="community-ack-box",
                        onchange="document.getElementById('community-continue').disabled=!this.checked;"),
                    " " + t("marketplace.ack_label", lang),
                    cls="small",
                ),
                Div(
                    Button(t("btn.continue", lang), id="community-continue", disabled=True,
                        hx_post="/modules/community-ack",
                        hx_target="#community-zone", hx_swap="outerHTML",
                        cls="btn btn--sm btn--primary"),
                    Button(t("btn.cancel", lang),
                        hx_get="/modules/community-panel?hide=1",
                        hx_target="#community-zone", hx_swap="outerHTML",
                        cls="btn btn--sm btn--secondary"),
                    style="display:flex;gap:8px;margin-top:8px;",
                ),
                cls="marketplace-community-warning",
                id="community-zone",
            )
        return await _community_open(request, lang)

    @app.post("/modules/community-ack")
    async def community_ack(request: Request):
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="community-zone")
        catalog.set_community_ack()
        return await _community_open(request, get_lang(request))

    async def _community_open(request: Request, lang: str):
        token = _token(request)
        installed = set()
        try:
            installed = {m["name"] for m in await api.get_modules(token)}
        except APIError:
            pass
        community = _cached_community()
        return Div(
            Button(t("btn.hide", lang),
                hx_get="/modules/community-panel?hide=1",
                hx_target="#community-zone", hx_swap="outerHTML",
                cls="btn btn--sm btn--secondary"),
            *(_catalog_card(m, lang, installed) for m in community),
            id="community-zone",
        )
