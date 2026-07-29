# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Modules - top-level admin surface (owner/admin only).

Three tabs:
  - Installed Modules: what is on this machine - Import Module (zip upload or
    desktop folder picker) and the modules-folder row lead, above a sortable,
    filterable table (enabled first, disabled last) with enable/disable, a
    working Restart button, and load-error surfacing.
  - Community Modules: leads with the build-your-own card, then the community
    listings from the catalog. They sit behind a one-step trust acknowledgment
    and carry no badge; the table uses the same schema as Installed Modules.
  - Marketplace: the official and verified catalog (community-modules
    index.json, public data), served via the relay with repo-direct and
    local-cache fallbacks; see ui.marketplace_catalog for why the relay
    endpoint is the one baked-in URL. Carries the List Your Modules entry point.

Restart is ONE endpoint for both modes: POST /system/restart writes the
sentinel and SIGTERMs. In desktop mode Electron's restart manager respawns
the servers and reloads the window; in server mode `celerp start` respawns
and the page's poll-and-reload script recovers the browser.
"""

from __future__ import annotations

import json
from pathlib import Path

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

import ui.api_client as api
import ui.marketplace_catalog as catalog
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import sortable_th, filter_th, table_search, COLUMN_FILTER_JS, ENHANCED_TABLE_JS
from ui.config import get_role as _get_role
from ui.i18n import t, get_lang
from ui.routes.account import GATE_UNREACHABLE, account_gate, gate_modal_response
from celerp.services.auth import ROLE_LEVELS as _ROLE_LEVELS

from ui.routes.settings import _token

_TEMPLATE_REPO = "https://github.com/celerp/celerp-module-template"
_DOCS_URL = "https://celerp.com/docs/modules.html"
# Where a seller lists a PAID module: the author dashboard (GitHub sign-in,
# Stripe Connect, publish with a price). Distinct from the free community
# registry, which only takes an index.json PR and carries no price.
_AUTHORS_URL = "https://www.celerp.com/authors"


def _build_card(lang: str) -> FT:
    """Build-your-own card: how to make a module and where the template lives.
    Sits at the top of the Community Modules tab (community modules are the
    author-built ones, so 'build your own' belongs with them)."""
    return Div(
        H3(t("modules.build_title", lang), cls="section-title"),
        P(t("modules.build_body", lang), cls="text-muted mb-sm"),
        Div(
            A(t("modules.build_template_link", lang), href=_TEMPLATE_REPO, target="_blank", rel="noopener noreferrer", cls="btn btn--sm btn--secondary"),
            A(t("modules.build_docs_link", lang), href=_DOCS_URL, target="_blank", rel="noopener noreferrer", cls="btn btn--sm btn--secondary", style="margin-left:8px;"),
            cls="mf-btns",
        ),
        cls="module-build-card",
    )


def _is_admin(request: Request) -> bool:
    return _ROLE_LEVELS.get(_get_role(request), 0) >= _ROLE_LEVELS["admin"]


def _modules_dir_display() -> str:
    import os
    raw = os.environ.get("MODULE_DIR", "")
    return raw.split(",")[0].strip()


def _license_upsell(lang: str) -> FT:
    """Moment-of-need Connect upsell shown when a paid module is licensed to
    another computer. Frames it additively (your module still works there;
    Connect brings it here + across devices/team), not as a punitive error."""
    from celerp.config import ensure_instance_id
    from celerp.gateway.state import build_subscribe_url
    url = build_subscribe_url(ensure_instance_id(), extra="plan=cloud")
    return Div(
        Strong(t("modules.license_move_title", lang), cls="small"),
        P(t("modules.license_move_body", lang), cls="text-muted small", style="margin:4px 0 8px;"),
        A(t("btn.get_connect", lang), href=url, target="_blank", rel="noopener noreferrer",
          cls="btn btn--sm btn--primary"),
        cls="module-license-upsell",
    )


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
    # The marketplace tab carries a shield badge on the left: its listings are
    # vetted (official and verified only), so the same trust mark used on the
    # rows also labels the tab.
    badge = Span(NotStr(_SHIELD_SVG), cls="trust-icon trust-icon--trusted tab-badge",
                 aria_hidden="true")
    items = [
        ("local", (t("modules.tab_local", lang),)),
        ("community", (t("modules.tab_community", lang),)),
        ("marketplace", (badge, t("modules.tab_marketplace", lang))),
    ]
    return Div(
        *[A(*children, href=f"/modules?tab={key}",
            cls=f"tab {'tab--active' if key == active else ''}")
          for key, children in items],
        cls="settings-tabs",
    )


# ── local modules panel ────────────────────────────────────────────────────────

def _deps_cell(m: dict, name_map: dict[str, str] | None = None) -> FT:
    """The Module Dependencies cell: the other modules a listing needs, so nothing
    is bought or enabled without its prerequisites. `name_map` translates internal
    module names to their display labels where known (local modules); catalog
    listings show the dependency ids as declared. Empty shows "--" (rule 2k.i)."""
    deps = m.get("depends_on") or []
    if name_map:
        deps = [name_map.get(d, d) for d in deps]
    txt = ", ".join(deps)
    return Td(txt or "--", data_filter_value=txt)


def _local_panel(modules: list[dict], lang: str = "en",
                 flash_text: str | None = None, flash_error: bool = False) -> FT:
    enabled_names = {m["name"] for m in modules if m.get("enabled") or m.get("running")}
    required_by: dict[str, list[str]] = {}
    for m in modules:
        if not (m.get("enabled") or m.get("running")):
            continue
        for dep in (m.get("depends_on") or []):
            required_by.setdefault(dep, []).append(m.get("label") or m["name"])

    # Order: imported modules on top, newest install first; bundled defaults
    # below, live ones before off ones. Built with three stable passes, least
    # significant first (Python's sort is stable, so each pass preserves the
    # previous order among ties):
    #   A. enabled/running first - the primary order among defaults and the
    #      tiebreaker among imports that share (or lack) an install time.
    #   B. newest installed_at first - ISO 8601 strings sort chronologically, so
    #      reverse gives newest first; defaults have no install time and tie here,
    #      keeping pass A's order.
    #   C. imports (non-default) before defaults - defaults re-seed on every
    #      desktop version bump, so their folder times are meaningless for "newest".
    # The shared table JS keeps this order until a header is clicked to sort.
    modules = sorted(modules, key=lambda m: 0 if (m.get("enabled") or m.get("running")) else 1)
    modules = sorted(modules, key=lambda m: m.get("installed_at") or "", reverse=True)
    modules = sorted(modules, key=lambda m: 1 if m.get("is_default") else 0)
    name_to_label = {m["name"]: (m.get("label") or m["name"]) for m in modules}

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
            status_filter = t("modules.badge_running", lang)
            status_parts.append(Span(status_filter, cls="badge badge--active"))
        elif enabled and load_error and "license" in load_error.lower():
            # A paid module present but not licensed on THIS computer (e.g. moved
            # from another machine): reframe the failure as the Connect upsell
            # rather than a dead red error - the moment-of-need conversion point.
            status_filter = t("settings.restart_needed", lang)
            status_parts.append(Span(status_filter, cls="badge badge--warning"))
            status_parts.append(_license_upsell(lang))
        elif enabled and load_error:
            # A broken module fails loudly, not silently.
            status_filter = t("modules.badge_failed", lang)
            status_parts.append(Span(status_filter, cls="badge badge--danger"))
            status_parts.append(Div(load_error, cls="text-muted small module-load-error"))
        elif enabled:
            # Enabled but not yet loaded: surface the restart as a control, not a
            # passive label, so the change can be applied from the row itself. A
            # restart is impactful (it reloads the app), so it confirms first -
            # this is the deliberate-action exception to on-page editing, not
            # routine data entry.
            status_filter = t("settings.restart_needed", lang)
            status_parts.append(Button(status_filter,
                hx_post="/modules/restart",
                hx_target="#local-modules-panel",
                hx_swap="outerHTML",
                hx_confirm=t("modules.restart_confirm", lang),
                hx_disabled_elt="this",
                cls="badge badge--warning badge-btn",
            ))
        else:
            status_filter = t("modules.badge_disabled", lang)
            status_parts.append(Span(status_filter, cls="badge badge--inactive"))

        dependents = required_by.get(name, [])
        if effectively_enabled:
            if dependents:
                toggle_btn = Button(t("btn.disable", lang),
                    title=t("modules.required_by", lang, names=", ".join(dependents)),
                    disabled=True,
                    cls="btn btn--sm btn--danger btn--disabled",
                )
            else:
                toggle_btn = Button(t("btn.disable", lang),
                    hx_post=f"/modules/{name}/disable",
                    hx_target="#local-modules-panel",
                    hx_swap="outerHTML",
                    hx_disabled_elt="this",
                    cls="btn btn--sm btn--danger",
                )
        else:
            toggle_btn = Button(t("btn.enable", lang),
                hx_post=f"/modules/{name}/enable",
                hx_target="#local-modules-panel",
                hx_swap="outerHTML",
                hx_disabled_elt="this",
                cls="btn btn--sm btn--primary",
            )

        # Provenance shield to the LEFT of the name (tags-left), gold for
        # bundled defaults, trusted/community for their sources, nothing for a
        # plain sideload or unknown origin (never a fabricated trust claim).
        source_icon = _source_icon(m.get("source"), bool(m.get("is_default")), lang)

        # A disabled, non-default module can be removed to free its name. The X
        # sits to the RIGHT of Enable (destructive control on the right) and only
        # appears once the module is off, so nothing that depends on it is live.
        action_parts = [toggle_btn]
        if not effectively_enabled and not m.get("is_default"):
            action_parts.append(Button("×",
                hx_post=f"/modules/{name}/delete",
                hx_target="#local-modules-panel",
                hx_swap="outerHTML",
                hx_confirm=t("modules.delete_confirm", lang, name=label),
                hx_disabled_elt="this",
                title=t("modules.delete_module", lang),
                aria_label=t("modules.delete_module", lang),
                cls="btn btn--sm btn--danger btn--icon module-delete-btn",
            ))

        rows.append(Tr(
            Td(Div(source_icon or "", Strong(label),
                   Div(description, cls="text-muted small") if description else "",
                   cls="module-name-cell"),
               data_filter_value=label),
            _deps_cell(m, name_to_label),
            Td(f"v{version}" if version and version != "unknown" else "--"),
            Td(author or "--"),
            Td(*status_parts, data_filter_value=status_filter),
            Td(*action_parts),
            # Disabled rows are shaded so their off state reads at a glance.
            cls="data-row" if effectively_enabled else "data-row module-row--disabled",
        ))

    # Derived restart banner, with a button that actually restarts.
    banner = Div(
        Span(t("settings._a_restart_is_required_for_module_changes_to_take", lang)),
        Button(t("btn.restart_now", lang),
            hx_post="/modules/restart",
            hx_target="#local-modules-panel",
            hx_swap="outerHTML",
            hx_disabled_elt="this",
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

    # Warning and file chooser are always visible (no drawer). Selecting a file
    # imports it immediately - the change event submits the form, so there is no
    # separate confirm button.
    import_section = Div(
        H3(t("btn.import_module", lang), cls="section-title"),
        Form(
            Input(type="file", name="file", accept=".zip", required=True),
            hx_post="/modules/import",
            hx_encoding="multipart/form-data",
            hx_trigger="change",
            hx_target="#local-modules-panel",
            hx_swap="outerHTML",
            hx_disabled_elt="find input[type=file]",
            cls="module-import-form",
        ),
        Button(t("btn.choose_folder", lang),
            id="import-folder-btn",
            type="button",
            cls="btn btn--sm btn--secondary",
            style="display:none;margin-top:6px;",
        ),
        P(t("modules.import_warning", lang), cls="text-muted small"),
        id="module-import",
        cls="module-import",
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
        cls="mb-md",
    ) if mdir else Div()

    if not rows:
        content = Div(
            P(t("modules.empty_state", lang), cls="text-muted"),
            cls="modules-empty",
        )
    else:
        content = Div(
            Div(table_search("local-modules-table", t("modules.search_placeholder", lang)),
                cls="table-toolbar"),
            Div(
                Table(
                    Thead(Tr(
                        sortable_th(t("th.module", lang), 0),
                        Th(t("th.dependencies", lang)),
                        sortable_th(t("th.version", lang), 2),
                        filter_th(t("th.author", lang), 3, sortable=True),
                        filter_th(t("th.status", lang), 4, sortable=True),
                        Th(""),
                    )),
                    Tbody(*rows),
                    id="local-modules-table",
                    cls="data-table js-table",
                ),
                Script(COLUMN_FILTER_JS),
                Script(ENHANCED_TABLE_JS),
                cls="table-scroll-wrap",
            ),
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
    })();
    """)

    return Div(
        folder_row,
        banner,
        flash_div,
        import_section,
        content,
        desktop_js,
        id="local-modules-panel",
        cls="settings-card",
    )


def _restarting_panel(lang: str, panel_id: str = "local-modules-panel") -> FT:
    """Shown right after POST /system/restart. Desktop: Electron reloads the
    window itself once the servers are back. Server mode: this script polls
    the same origin and reloads when the UI answers again.

    *panel_id* is the id of the panel the restart was triggered from, so the
    outerHTML swap keeps that element's identity (the marketplace flow and the
    local-modules flow each target their own panel)."""
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
        id=panel_id,
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
    tip = t("marketplace.official_tip", lang) if tier == "official" else t("marketplace.verified_tip", lang)
    return Span(NotStr(_SHIELD_SVG), cls="trust-icon trust-icon--trusted",
                title=tip, aria_label=tip, role="img")


def _source_icon(source: str | None, is_default: bool, lang: str):
    """The provenance shield shown to the left of a module name, or None.

    Defaults are trusted by name (gold), regardless of any sidecar. Marketplace
    and community modules carry their own shield. A plain sideload or an unknown
    origin shows nothing - never a fabricated trust claim.
    """
    if is_default:
        tier, key = "default", "modules.source_default"
    elif source == "marketplace":
        tier, key = "trusted", "modules.source_marketplace"
    elif source == "community":
        tier, key = "community", "modules.source_community"
    else:
        return None
    tip = t(key, lang)
    return Span(NotStr(_SHIELD_SVG),
                cls=f"module-source-icon trust-icon trust-icon--{tier}",
                title=tip, aria_label=tip, role="img")


def _catalog_price(m: dict, lang: str) -> str:
    if m.get("price_monthly"):
        return f"${m['price_monthly']:g}/mo"
    if m.get("price_once"):
        return f"${m['price_once']:g}"
    return t("marketplace.free", lang)


def _buy_btn(slug: str, kind: str, label: str, lang: str):
    """A Buy button: POSTs to /modules/buy, which returns the waiting panel that
    opens Stripe Checkout in the browser and polls for the license."""
    return Button(t("btn.buy", lang) + " " + label,
                  hx_post=f"/modules/buy?slug={slug}&kind={kind}",
                  hx_target="#marketplace-panel", hx_swap="outerHTML",
                  hx_disabled_elt="this",
                  cls="btn btn--sm btn--primary")


def _buy_waiting_panel(url: str, slug: str, lang: str, n: int = 0):
    """Shown after starting checkout: open Stripe Checkout in the browser and
    poll the catalog (which re-renders with the module owned once the webhook
    lands the license). Resumable - closing the tab does not lose the purchase.
    Cancel (button or Esc) returns to the catalog; after 10 minutes the poll
    stops and a still-waiting message with a manual refresh takes over.

    *n* is the server-side poll counter (0 on the first render from /modules/buy,
    then incremented by /modules/buy-poll on each re-fetch)."""
    import json as _json
    POLL_MAX = 120   # 120 x 5s ≈ 10 minutes, then stop polling
    actions = [
        Button(t("btn.cancel", lang), id="buy-cancel",
               hx_get="/modules/marketplace-panel",
               hx_target="#marketplace-panel", hx_swap="outerHTML",
               cls="btn btn--sm btn--secondary"),
    ]
    # The Stripe URL is only known on the FIRST render (from /modules/buy). On a
    # re-poll (url is None) we keep waiting without a stale reopen link.
    if url:
        actions.insert(0, A(t("marketplace.open_checkout", lang), href=url, target="_blank",
                            rel="noopener noreferrer", cls="btn btn--sm btn--secondary"))

    parts = [
        P(t("marketplace.waiting_payment", lang), cls="text-muted"),
        Div(*actions, style="display:flex;gap:8px;"),
    ]
    if n < POLL_MAX:
        # Keep the poll element in a SIBLING wrapper that carries no swap target of
        # its own; it re-fetches buy-poll, which re-emits this whole panel (poll
        # intact) until the module is owned, then swaps in the catalog. The counter
        # bounds the wait server-side, so it survives every panel swap.
        parts.append(Div(id="buy-poll",
                         hx_get=f"/modules/buy-poll?slug={slug}&n={n + 1}",
                         hx_trigger="every 5s",
                         hx_target="#marketplace-panel", hx_swap="outerHTML"))
    else:
        parts.append(P(t("marketplace.waiting_timeout", lang),
                       cls="flash flash--warning"))
    if url:
        # First render only: open Checkout in the external browser, and wire Esc to
        # cancel. Attached to document once so it isn't re-added on every re-poll.
        parts.append(Script(
            f"(function(){{var u={_json.dumps(url)};"
            f"if(window.celerp&&window.celerp.openExternal){{window.celerp.openExternal(u);}}"
            f"else{{window.open(u,'_blank');}}}})();"))
        parts.append(Script("""
        (function () {
          if (window.__celerpBuyEsc) return;
          window.__celerpBuyEsc = function (e) {
            if (e.key === 'Escape') {
              var b = document.getElementById('buy-cancel');
              if (b) { b.click(); }
            }
          };
          document.addEventListener('keydown', window.__celerpBuyEsc);
        })();
        """))
    return Div(*parts, id="marketplace-panel", cls="settings-card")


async def _build_marketplace_panel(token: str, lang: str) -> FT:
    """The marketplace catalog fragment (id=marketplace-panel). Shared by the
    tab load, the poll-until-owned target, and every 'back to catalog' path."""
    try:
        modules_list, from_cache = await catalog.fetch_catalog()
    except Exception:
        return Div(P(t("marketplace.could_not_load", lang), cls="text-muted"),
                   id="marketplace-panel")
    installed = set()
    try:
        installed = {m["name"] for m in await api.get_modules(token)}
    except APIError:
        pass
    licensed = set()
    try:
        licensed = set(await api.module_licenses(token))
    except APIError:
        pass
    trusted = [m for m in modules_list if m["tier"] in ("official", "verified")]
    children = [Div(
        A(t("btn.list_your_modules", lang), href=_AUTHORS_URL,
          target="_blank", rel="noopener noreferrer", cls="btn btn--sm btn--secondary"),
        style="margin-bottom:12px;",
    )]
    # Quiet confirmation only: when an account is bound, say whose. No line at
    # all otherwise - the signup ask lives on the Buy action, not the catalog.
    try:
        status = await api.account_status(token)
    except APIError:
        status = {}
    if status.get("email_verified") and status.get("email") and not status.get("error"):
        children.insert(0, P(t("account.signed_in_as", lang, email=status["email"]),
                             cls="text-muted small mb-sm"))
    if from_cache:
        children.append(Div(t("marketplace.from_cache", lang), cls="flash flash--warning"))
    if not trusted:
        children.append(P(t("settings.no_modules_available_in_the_marketplace_yet", lang), cls="text-muted"))
    else:
        children.append(_marketplace_table(trusted, installed, licensed, lang))
    return Div(*children, id="marketplace-panel")


def _marketplace_error_panel(message: str, lang: str):
    """An action failed: keep the message on screen (no auto-reload wiping it)
    with an explicit way back to the catalog."""
    return Div(
        Div(message, cls="flash flash--error"),
        Button(t("btn.back", lang),
               hx_get="/modules/marketplace-panel",
               hx_target="#marketplace-panel", hx_swap="outerHTML",
               cls="btn btn--sm btn--secondary"),
        id="marketplace-panel", cls="settings-card",
    )


def _marketplace_module_cell(m: dict, lang: str) -> FT:
    """Module column for a marketplace listing: name, the vetted disclosures, and
    a link to the module's page. Marketplace modules are distributed through the
    relay and are generally closed-source, so the cell links to the listing page,
    not a public repo. External links open with rel=noopener noreferrer so the
    opened tab cannot reach back through window.opener."""
    parts = [Div(_trust_icon(m["tier"], lang), Strong(m["name"]), cls="module-name-cell")]
    if m.get("description"):
        parts.append(Div(m["description"], cls="text-muted small"))
    if m.get("data_access"):
        parts.append(P(Strong(t("marketplace.data_access", lang)), ": ", m["data_access"], cls="community-disclosure"))
    if m.get("network_calls"):
        parts.append(P(Strong(t("marketplace.network_calls", lang)), ": ", m["network_calls"], cls="community-disclosure"))
    parts.append(P(Strong(t("th.license", lang)), ": ", m["license"], cls="community-disclosure"))
    links = [A(t("marketplace.view_page", lang),
               href=m.get("homepage") or f"https://celerp.com/marketplace/{m['id']}",
               target="_blank", rel="noopener noreferrer")]
    if m.get("feedback"):
        links.append(A(t("marketplace.feedback", lang), href=m["feedback"],
                       target="_blank", rel="noopener noreferrer"))
    parts.append(Div(*links, cls="marketplace-card__links"))
    return Td(*parts, data_filter_value=m["name"])


def _checkout_consent(m: dict, lang: str) -> str:
    """Purchase disclosures for the Stripe Checkout page, in the buyer's language:
    for a third-party sale, the seller and the platform's data-sharing note; for
    every paid module, the licensing terms. Rendered here and passed to the relay
    to sit as the checkout's custom text, on the screen where the buyer consents
    and pays. The relay holds no translations, so the language is resolved here."""
    lines: list[str] = []
    if m.get("tier") != "official" and m.get("author"):
        lines.append(t("marketplace.sold_by", lang, author=m["author"]) + " "
                     + t("marketplace.third_party_data_note", lang, author=m["author"]))
    lines.append(t("marketplace.buy_note", lang))
    return "\n\n".join(lines)


def _marketplace_row(m: dict, lang: str, installed: set[str], licensed: set[str], *,
                     downloaded_path: str | None = None) -> FT:
    """One marketplace listing. The action cell follows ownership, matching the
    Community tab's Download then Install flow with a Buy step in front of paid
    modules: installed (nothing to do), paid-and-unowned (Buy), then - once free
    or owned - Download, then Install. Each Download/Install click swaps this row
    in place; a failure surfaces as a corner toast. The Price cell always states
    the catalog price."""
    row_id = f"marketplace-row-{m['id']}"
    is_paid = bool(m.get("price_monthly") or m.get("price_once"))
    owned = m["id"] in licensed
    if m["id"] in installed:
        status_td = Td(Span(t("settings.installed", lang), cls="badge badge--active"),
                       data_filter_value=t("settings.installed", lang))
        action_td = Td("--")
    elif is_paid and not owned:
        # Paid, not yet owned: Buy button(s). The purchase disclosures (seller,
        # data-sharing, and licensing terms) sit on the Checkout page, where the
        # buyer consents and pays - see _checkout_consent - not in this table.
        buys = []
        if m.get("price_monthly"):
            buys.append(_buy_btn(m["id"], "monthly", f"${m['price_monthly']:g}/mo", lang))
        if m.get("price_once"):
            buys.append(_buy_btn(m["id"], "once", f"${m['price_once']:g} " + t("marketplace.once", lang), lang))
        status_td = Td("--", data_filter_value="--")
        action_td = Td(Div(*buys, style="display:flex;gap:8px;flex-wrap:wrap;"))
    else:
        # Free, or paid and owned: the same two-step as a community module.
        # Owned paid modules keep an ownership badge so the buyer sees they hold
        # the licence; free modules show no status until installed.
        if owned:
            status_td = Td(Span(t("marketplace.owned", lang), cls="badge badge--active"),
                           data_filter_value=t("marketplace.owned", lang))
        else:
            status_td = Td("--", data_filter_value="--")
        if downloaded_path:
            action_td = Td(Button(t("btn.install", lang),
                hx_post="/modules/marketplace-install",
                hx_vals=json.dumps({"slug": m["id"], "path": downloaded_path}),
                hx_target=f"#{row_id}", hx_swap="outerHTML", hx_disabled_elt="this",
                cls="btn btn--sm btn--primary"))
        else:
            action_td = Td(Button(t("btn.download", lang),
                hx_post="/modules/marketplace-download",
                hx_vals=json.dumps({"slug": m["id"]}),
                hx_target=f"#{row_id}", hx_swap="outerHTML", hx_disabled_elt="this",
                cls="btn btn--sm btn--secondary"))
    version = m.get("version")
    return Tr(
        _marketplace_module_cell(m, lang),
        _deps_cell(m),
        Td(f"v{version}" if version and version != "unknown" else "--"),
        Td(m.get("author") or "--"),
        status_td,
        Td(_catalog_price(m, lang), cls="cell--number"),
        action_td,
        id=row_id,
        cls="data-row",
    )


def _marketplace_table(trusted: list[dict], installed: set[str],
                       licensed: set[str], lang: str) -> FT:
    """Marketplace listings, the same table schema as Installed Modules and the
    Community tab, with a Price column after Status (right-aligned over its
    figures, per the financial-statement convention)."""
    return Div(
        Div(table_search("marketplace-table", t("modules.search_placeholder", lang)),
            cls="table-toolbar"),
        Div(
            Table(
                Thead(Tr(
                    sortable_th(t("th.module", lang), 0),
                    Th(t("th.dependencies", lang)),
                    sortable_th(t("th.version", lang), 2),
                    filter_th(t("th.author", lang), 3, sortable=True),
                    filter_th(t("th.status", lang), 4, sortable=True),
                    sortable_th(t("th.price", lang), 5, right=True),
                    Th(""),
                )),
                Tbody(*(_marketplace_row(m, lang, installed, licensed) for m in trusted)),
                id="marketplace-table",
                cls="data-table js-table",
            ),
            Script(COLUMN_FILTER_JS),
            Script(ENHANCED_TABLE_JS),
            cls="table-scroll-wrap",
        ),
    )


def _community_reveal_prompt(n: int, lang: str) -> FT:
    """One-step acknowledgment before community listings are shown: the trust
    warning and the two buttons, no checkbox. 'Show Community Modules' both
    acknowledges and reveals; 'Cancel' leaves the listings closed."""
    return Div(
        P(t("marketplace.community_ack", lang, n=n), cls="small"),
        Div(
            Button(t("btn.show_community", lang),
                hx_post="/modules/community-ack",
                hx_target="#community-zone", hx_swap="outerHTML",
                hx_disabled_elt="this",
                cls="btn btn--sm btn--primary"),
            Button(t("btn.cancel", lang), id="community-cancel",
                hx_get="/modules/community-panel?prompt=1",
                hx_target="#community-zone", hx_swap="outerHTML",
                cls="btn btn--sm btn--secondary"),
            style="display:flex;gap:8px;margin-top:8px;",
        ),
        Script("""
        (function () {
          function esc(e) {
            if (e.key === 'Escape') {
              var b = document.getElementById('community-cancel');
              if (b) { b.click(); }
              document.removeEventListener('keydown', esc);
            }
          }
          document.addEventListener('keydown', esc);
        })();
        """),
        cls="marketplace-community-warning",
        id="community-zone",
    )


def _community_module_cell(m: dict, lang: str) -> FT:
    """Module column for a community listing: name, description, and the
    disclosures and source links, so the trust information sits inline in the
    table rather than behind a drawer. Community rows carry no badge; that the
    disclosures are author self-declared is stated once, above the table, not
    repeated per row."""
    parts = [Div(Strong(m["name"]), cls="module-name-cell")]
    if m.get("description"):
        parts.append(Div(m["description"], cls="text-muted small"))
    if m.get("data_access"):
        parts.append(P(Strong(t("marketplace.data_access", lang)), ": ", m["data_access"], cls="community-disclosure"))
    if m.get("network_calls"):
        parts.append(P(Strong(t("marketplace.network_calls", lang)), ": ", m["network_calls"], cls="community-disclosure"))
    parts.append(P(Strong(t("th.license", lang)), ": ", m["license"], cls="community-disclosure"))
    links = []
    if m.get("repo"):
        links.append(A(t("marketplace.view_source", lang), href=m["repo"], target="_blank", rel="noopener noreferrer"))
        links.append(A(t("marketplace.report_bug", lang), href=m["repo"].rstrip("/") + "/issues", target="_blank", rel="noopener noreferrer"))
    links.append(A(t("marketplace.feedback", lang),
                   href=m.get("feedback") or "https://github.com/celerp/community-modules/discussions",
                   target="_blank", rel="noopener noreferrer"))
    parts.append(Div(*links, cls="marketplace-card__links"))
    return Td(*parts, data_filter_value=m["name"])


def _community_row(m: dict, lang: str, installed: set[str], *,
                   downloaded_path: str | None = None) -> FT:
    """One community listing. Three states drive the Status and action cells:
    installed (nothing to do), downloaded (offer Import), or fresh (offer
    Download). Download fetches the author's repo archive; Import installs it -
    two deliberate clicks, each swapping this row in place. A failed download or
    import surfaces as a corner toast (see the routes), not inside the row."""
    row_id = f"community-row-{m['id']}"
    if m["id"] in installed:
        status_td = Td(Span(t("settings.installed", lang), cls="badge badge--active"),
                       data_filter_value=t("settings.installed", lang))
        action_td = Td("--")
    elif downloaded_path:
        status_td = Td(Span(t("marketplace.downloaded", lang), cls="badge badge--active"),
                       data_filter_value=t("marketplace.downloaded", lang))
        action_td = Td(Button(t("btn.import", lang),
            hx_post="/modules/community-import",
            hx_vals=json.dumps({"id": m["id"], "path": downloaded_path}),
            hx_target=f"#{row_id}", hx_swap="outerHTML", hx_disabled_elt="this",
            cls="btn btn--sm btn--primary"))
    else:
        status_td = Td("--", data_filter_value="--")
        action_td = Td(Button(t("btn.download", lang),
            hx_post="/modules/community-download",
            hx_vals=json.dumps({"id": m["id"]}),
            hx_target=f"#{row_id}", hx_swap="outerHTML", hx_disabled_elt="this",
            cls="btn btn--sm btn--secondary"))
    version = m.get("version")
    return Tr(
        _community_module_cell(m, lang),
        _deps_cell(m),
        Td(f"v{version}" if version and version != "unknown" else "--"),
        Td(m.get("author") or "--"),
        status_td,
        action_td,
        id=row_id,
        cls="data-row",
    )


def _with_error_toast(fragment: FT, message: str) -> HTMLResponse:
    """Swap the fragment back in place and raise the failure as a corner toast,
    the app-wide error surface, rather than crowding the status cell."""
    return HTMLResponse(
        to_xml(fragment),
        headers={"HX-Trigger": json.dumps(
            {"celerpToast": {"message": message, "type": "error"}})},
    )


def _community_table(community: list[dict], installed: set[str], lang: str,
                     downloaded: dict[str, str] | None = None) -> FT:
    """Community listings, same table schema as the Installed Modules table."""
    if not community:
        return Div(P(t("settings.no_modules_available_in_the_marketplace_yet", lang), cls="text-muted"),
                   id="community-zone")
    return Div(
        P(t("marketplace.community_unverified_note", lang), cls="text-muted small community-unverified-note"),
        Div(table_search("community-table", t("modules.search_placeholder", lang)),
            cls="table-toolbar"),
        Div(
            Table(
                Thead(Tr(
                    sortable_th(t("th.module", lang), 0),
                    Th(t("th.dependencies", lang)),
                    sortable_th(t("th.version", lang), 2),
                    filter_th(t("th.author", lang), 3, sortable=True),
                    filter_th(t("th.status", lang), 4, sortable=True),
                    Th(""),
                )),
                Tbody(*(_community_row(m, lang, installed,
                                       downloaded_path=(downloaded or {}).get(m["id"]))
                        for m in community)),
                id="community-table",
                cls="data-table js-table",
            ),
            Script(COLUMN_FILTER_JS),
            Script(ENHANCED_TABLE_JS),
            cls="table-scroll-wrap",
        ),
        id="community-zone",
    )


async def _community_and_installed(token: str) -> tuple[list[dict], set[str]]:
    """Fetch the catalog's community listings and the set of installed module
    names. Fetching listing metadata carries no trust risk; only installing a
    community module runs its code, and that stays behind the acknowledgment."""
    modules_list, _ = await catalog.fetch_catalog()
    community = [m for m in modules_list if m["tier"] == "community"]
    installed: set[str] = set()
    try:
        installed = {m["name"] for m in await api.get_modules(token)}
    except APIError:
        pass
    return community, installed


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
        elif tab == "community":
            content = Div(
                _build_card(lang),
                Div(
                    Span(t("modules.loading_catalog", lang), cls="text-muted small"),
                    hx_get="/modules/community-panel",
                    hx_trigger="load",
                    hx_swap="outerHTML",
                    id="community-zone",
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

        return await base_shell(
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

    @app.post("/modules/{module_name}/delete")
    async def module_delete(request: Request, module_name: str):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        flash_text = None
        try:
            await api.delete_module(token, module_name)
            modules = await api.get_modules(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            # Refused (still enabled/running, or default): keep the row and tell
            # the user why, rather than silently doing nothing.
            flash_text = e.detail or t("modules.delete_failed", lang)
            try:
                modules = await api.get_modules(token)
            except APIError:
                modules = []
        return _local_panel(modules, lang=lang, flash_text=flash_text,
                            flash_error=flash_text is not None)

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

    async def _community_entry(token: str, module_id: str) -> tuple[dict, set[str]]:
        """Resolve a community listing by id plus the current installed set.
        Falls back to a minimal record so an unknown id still renders a row."""
        community, installed = await _community_and_installed(token)
        m = next((x for x in community if x["id"] == module_id), None)
        return (m or {"id": module_id, "name": module_id, "license": "-"}), installed

    @app.post("/modules/community-download")
    async def community_download(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        form = await request.form()
        module_id = str(form.get("id", ""))
        # "zone" marks the post-sign-in resume, which re-renders the whole
        # listings zone (the sign-in panel replaced it) instead of one row.
        zone = str(form.get("zone", "")) == "1"
        # Free downloads ask for the free account (the download itself is the
        # moment the account earns its keep), but a relay outage never blocks
        # one - the gate fails open.
        gate = await account_gate(token, lang, f"community:{module_id}")
        if gate is not None and gate is not GATE_UNREACHABLE:
            return gate_modal_response(gate)
        m, installed = await _community_entry(token, module_id)
        try:
            path = await catalog.download_community_archive(m.get("repo", ""), module_id)
        except Exception:
            if zone:
                community, installed = await _community_and_installed(token)
                return _with_error_toast(_community_table(community, installed, lang),
                                         t("marketplace.download_failed", lang))
            return _with_error_toast(_community_row(m, lang, installed),
                                     t("marketplace.download_failed", lang))
        if zone:
            community, installed = await _community_and_installed(token)
            return _community_table(community, installed, lang,
                                    downloaded={module_id: path})
        return _community_row(m, lang, installed, downloaded_path=path)

    @app.post("/modules/community-import")
    async def community_import(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        form = await request.form()
        module_id = str(form.get("id", ""))
        path = str(form.get("path", ""))
        m, installed = await _community_entry(token, module_id)
        try:
            data = catalog.read_staged_archive(path)
            await api.import_module_zip(token, f"{module_id}.zip", data, source="community")
        except APIError as e:
            return _with_error_toast(
                _community_row(m, lang, installed, downloaded_path=path),
                e.detail or str(e))
        except (ValueError, OSError):
            return _with_error_toast(
                _community_row(m, lang, installed, downloaded_path=path),
                t("marketplace.import_failed", lang))
        # Installed: drop the staged archive and re-read the installed set so the
        # row reflects reality (the module now appears in get_modules).
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        _, installed = await _community_and_installed(token)
        return _community_row(m, lang, installed)

    @app.post("/modules/restart")
    async def module_restart(request: Request):
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        # Enabling and restarting a module - however it was installed - happen in
        # the Installed tab, so the restart banner always swaps that panel.
        try:
            await api.restart_system(token)
        except APIError as e:
            modules = []
            try:
                modules = await api.get_modules(token)
            except APIError:
                pass
            return _local_panel(modules, lang=lang, flash_text=e.detail or str(e), flash_error=True)
        return _restarting_panel(lang, panel_id="local-modules-panel")

    @app.get("/modules/marketplace-panel")
    async def marketplace_panel(request: Request):
        """HTMX fragment: the catalog, fetched repo-direct and cached."""
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="marketplace-panel")
        return await _build_marketplace_panel(token, get_lang(request))

    @app.get("/modules/buy-poll")
    async def buy_poll(request: Request):
        """Poll target while a purchase is pending: re-emit the waiting panel
        (keeping the poll alive) until the module is licensed/installed here, then
        flip to the catalog. A server-side counter bounds the wait so the poll
        stops after ~10 minutes even across panel swaps."""
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="marketplace-panel")
        lang = get_lang(request)
        slug = request.query_params.get("slug", "")
        try:
            n = int(request.query_params.get("n", "0"))
        except ValueError:
            n = 0
        licensed = installed = set()
        try:
            licensed = set(await api.module_licenses(token))
        except APIError:
            pass
        try:
            installed = {m["name"] for m in await api.get_modules(token)}
        except APIError:
            pass
        if slug in licensed or slug in installed:
            # The purchase landed - show the catalog (module now Owned/installed).
            return await _build_marketplace_panel(token, lang)
        return _buy_waiting_panel(None, slug, lang, n=n)

    @app.post("/modules/buy")
    async def modules_buy(request: Request):
        """Start a module purchase: get the Stripe Checkout URL from the relay and
        return the waiting panel (opens Checkout in the browser, polls the license)."""
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        slug = request.query_params.get("slug", "")
        kind = request.query_params.get("kind", "monthly")
        # Purchases are held by the account's email; without a verified one the
        # license would be recoverable only on this machine. Sign in first, and
        # fail closed when the account state cannot be checked - checkout must
        # never start against an unknown account.
        gate = await account_gate(token, lang, f"buy:{slug}:{kind}")
        if gate is GATE_UNREACHABLE:
            return _marketplace_error_panel(t("account.status_unreachable", lang), lang)
        if gate is not None:
            return gate_modal_response(gate)
        m, _installed, _licensed = await _marketplace_entry(token, slug)
        try:
            res = await api.buy_module(token, slug, kind, _checkout_consent(m, lang))
        except APIError as e:
            return _marketplace_error_panel(e.detail or str(e), lang)
        return _buy_waiting_panel(res.get("url", ""), slug, lang)

    async def _marketplace_entry(token: str, slug: str):
        """Resolve a marketplace listing by slug plus the current installed and
        licensed sets. Falls back to a minimal record so an unknown slug still
        renders a row."""
        try:
            modules_list, _ = await catalog.fetch_catalog()
        except Exception:
            modules_list = []
        m = next((x for x in modules_list if x["id"] == slug), None) \
            or {"id": slug, "name": slug, "tier": "verified", "license": "-"}
        installed = set()
        try:
            installed = {x["name"] for x in await api.get_modules(token)}
        except APIError:
            pass
        licensed = set()
        try:
            licensed = set(await api.module_licenses(token))
        except APIError:
            pass
        return m, installed, licensed

    @app.post("/modules/marketplace-download")
    async def modules_marketplace_download(request: Request):
        """Step one of installing a marketplace module: the local API fetches it
        from the relay and stages it. On success the row offers Install; a failure
        returns the row with a corner toast, so retrying is always possible."""
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        form = await request.form()
        slug = str(form.get("slug", ""))
        m, installed, licensed = await _marketplace_entry(token, slug)
        try:
            res = await api.marketplace_download(token, slug)
        except APIError as e:
            return _with_error_toast(
                _marketplace_row(m, lang, installed, licensed), e.detail or str(e))
        return _marketplace_row(m, lang, installed, licensed,
                                downloaded_path=res.get("path"))

    @app.post("/modules/marketplace-install")
    async def modules_marketplace_install(request: Request):
        """Step two: install the staged archive. The module lands disabled, the
        same as a community import - enabling and restarting are the deliberate
        steps in the Installed tab. A failure surfaces as a corner toast with the
        row intact."""
        token, redirect = await _guard(request)
        if redirect:
            return redirect
        lang = get_lang(request)
        form = await request.form()
        slug = str(form.get("slug", ""))
        path = str(form.get("path", ""))
        m, installed, licensed = await _marketplace_entry(token, slug)
        try:
            await api.marketplace_install(token, path)
        except APIError as e:
            return _with_error_toast(
                _marketplace_row(m, lang, installed, licensed, downloaded_path=path),
                e.detail or str(e))
        # Installed: re-read the installed set so the row reflects reality (the
        # module now appears in get_modules).
        installed = set()
        try:
            installed = {x["name"] for x in await api.get_modules(token)}
        except APIError:
            pass
        return _marketplace_row(m, lang, installed, licensed)

    @app.get("/modules/community-panel")
    async def community_panel(request: Request):
        """HTMX fragment for the Community tab: the acknowledgment prompt, or the
        listings table once acknowledged. `?prompt=1` forces the prompt (Cancel)."""
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="community-zone")
        lang = get_lang(request)
        try:
            community, installed = await _community_and_installed(token)
        except Exception:
            return Div(P(t("marketplace.could_not_load", lang), cls="text-muted"), id="community-zone")
        if not community:
            return Div(P(t("settings.no_modules_available_in_the_marketplace_yet", lang),
                         cls="text-muted"), id="community-zone")
        if catalog.community_acked() and not request.query_params.get("prompt"):
            return _community_table(community, installed, lang)
        return _community_reveal_prompt(len(community), lang)

    @app.post("/modules/community-ack")
    async def community_ack(request: Request):
        token = _token(request)
        if not token or not _is_admin(request):
            return Div(id="community-zone")
        lang = get_lang(request)
        catalog.set_community_ack()
        try:
            community, installed = await _community_and_installed(token)
        except Exception:
            return Div(P(t("marketplace.could_not_load", lang), cls="text-muted"), id="community-zone")
        return _community_table(community, installed, lang)
