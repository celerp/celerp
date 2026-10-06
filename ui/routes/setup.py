# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company setup: the business type, currency and timezone a company needs.

Flow:
    /setup              -> the workspace form (ui/routes/auth.py); one POST creates the
                           owner and company and applies everything below
    /setup/company      -> the same choices for a company that has no business type yet:
                           a new company, or a first-run setup whose last step failed
    /setup/activating   -> waits for the business type's modules, then opens the dashboard
    /setup/cloud        -> optional cloud offer, reachable by direct link
"""

from __future__ import annotations

import json
import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import auth_shell, client_scripts, flash, page_title
from ui.components.currency import currency_options
from ui.components.table import searchable_select
from celerp.services.business_time import business_timezone
from celerp.services.currencies import CURRENCY_CODES
from ui.config import COOKIE_NAME
from ui.i18n import t, get_lang
from celerp.services.vertical_presets import list_presets, load_preset

logger = logging.getLogger(__name__)


def _preset_label(preset: dict) -> str:
    """Resolved through t() at render time: first-party presets carry a ``label_key``;
    a preset without one renders its ``display_name``."""
    if preset.get("label_key"):
        return t(preset["label_key"])
    return preset.get("display_name") or preset["name"]


def business_type_label(value: str) -> str | None:
    """The label of a stored business type, hidden presets included (a company may
    hold one from before it was hidden). None when no such preset exists."""
    preset = load_preset(value, allow_hidden=True) if value else None
    return _preset_label(preset) if preset else None


def business_type_options() -> list[tuple[str, str]]:
    """The offered business types as [(value, label), ...].

    Built from the shared visible preset catalog (hidden presets are never offered).
    'blank' sorts last; all others sort alphabetically by label.
    """
    options = [(p["name"], _preset_label(p)) for p in list_presets()]
    return (sorted((o for o in options if o[0] != "blank"), key=lambda o: o[1])
            + [o for o in options if o[0] == "blank"])


def setup_timezone(value: str) -> str:
    """The timezone the browser reported when it is a real IANA zone (the same check
    the company settings API makes), otherwise the default business timezone."""
    try:
        return business_timezone(value).key
    except ValueError:
        return business_timezone(None).key


def company_setup_error(currency: str, vertical: str) -> tuple[str, str] | None:
    """(field, message) for the first invalid choice, or None when both are valid."""
    if not currency:
        return "currency", t("setup.currency_required")
    if currency not in CURRENCY_CODES:
        return "currency", t("setup.invalid_currency", value=repr(currency))
    if not vertical:
        return "vertical", t("setup.business_type_required")
    if vertical not in {value for value, _ in business_type_options()}:
        return "vertical", t("setup.unknown_business_type", value=repr(vertical))
    return None


async def apply_company_setup(token: str, currency: str, timezone: str, vertical: str) -> str:
    """Store the currency and timezone, then apply the business type, and return
    where the user goes next. Raises APIError when a step fails; the business type
    is applied last, so a company without one has not finished setup, and applying
    it again is safe."""
    await api.patch_company(token, {"currency": currency, "timezone": setup_timezone(timezone)})
    result = await api.set_business_type(token, vertical)
    if not result.get("restart_required"):
        return "/dashboard"
    # The type's modules load on restart; the activating page waits for them. The
    # server may drop this request as it goes down, so its outcome is not a failure
    # of setup.
    try:
        await api.restart_system(token)
    except Exception:
        pass
    return "/setup/activating"


def has_business_type(company: dict) -> bool:
    return bool(company.get("vertical") or (company.get("settings") or {}).get("vertical"))


def company_choice_fields(currency: str = "", vertical: str = "") -> list:
    """Business type and currency, both searchable and required, plus the timezone
    the browser fills in. Shared by the workspace form and /setup/company."""
    options = business_type_options()
    offered = {val for val, _ in options}
    return [
        Div(
            Label(t("label.business_type"), cls="form-label"),
            # Searchable: the catalog holds more than ten types (GDR 2i). There is no
            # default on purpose; an empty choice is refused by the server with a
            # message, never by the browser.
            searchable_select("vertical", options, value=vertical if vertical in offered else "",
                              placeholder=t("setup.choose_business_type"),
                              aria_label=t("label.business_type")),
            P(t("setup.business_type_hint"), cls="form-hint"),
            cls="form-group",
        ),
        Div(
            Label(t("th.currency"), cls="form-label"),
            searchable_select("currency", currency_options(),
                              value=currency if currency in CURRENCY_CODES else "",
                              placeholder=t("setup.currency_search_placeholder"),
                              aria_label=t("th.currency")),
            P(t("setup.currency_hint"), cls="form-hint"),
            cls="form-group",
        ),
        Input(type="hidden", name="timezone", value=""),
    ]


def company_choice_script() -> FT:
    """Fills the hidden timezone from the browser and suggests a currency from where
    the browser is, before the dropdowns initialise so the suggestion is the shown
    choice. A choice already made is never replaced. Without JavaScript the server
    default timezone applies and the currency is simply required."""
    return (
        Script(src="/static/currency-guess.js"),
        Script("""
(function () {
  var tz = '';
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
  if (window.celerpCurrentZone) tz = window.celerpCurrentZone(tz);
  document.querySelectorAll('input[name="timezone"]').forEach(function (el) { el.value = tz; });
  var hidden = document.querySelector('input[type="hidden"][name="currency"]');
  if (!hidden || hidden.value || !window.celerpGuessCurrency) return;
  var code = window.celerpGuessCurrency(tz, navigator.languages || [navigator.language]);
  var wrap = hidden.closest('.combobox-wrap');
  var opt = code && wrap && wrap.querySelector('.combobox-option[data-value="' + code + '"]');
  if (!opt) return;
  hidden.value = code;
  wrap.querySelector('.combobox-input').value = opt.textContent;
})();
"""),
    )


def setup_routes(app):

    def _company_page(request: Request, values: dict, error: str | None = None):
        return auth_shell(
            *client_scripts(get_lang(request)),
            _company_setup_form(values, error=error),
            title=page_title("setup.finish_title"),
        )

    @app.get("/setup/company")
    async def company_setup_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        # Safe to reopen: a company that already has a business type is set up.
        if has_business_type(company):
            return RedirectResponse("/dashboard", status_code=302)
        error = t("setup.finish_failed") if request.query_params.get("failed") else None
        return _company_page(request, {"currency": company.get("currency") or ""}, error=error)

    @app.post("/setup/company")
    async def company_setup_submit(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        if has_business_type(company):
            return RedirectResponse("/dashboard", status_code=302)
        form = await request.form()
        # Every failure rerenders from what the user submitted, so no choice is lost.
        values = {
            "currency": str(form.get("currency", "")).strip(),
            "vertical": str(form.get("vertical", "")).strip(),
        }
        if invalid := company_setup_error(values["currency"], values["vertical"]):
            return _company_page(request, values, error=invalid[1])
        try:
            nxt = await apply_company_setup(token, values["currency"], str(form.get("timezone", "")),
                                            values["vertical"])
        except APIError as e:
            return _company_page(request, values, error=e.detail)
        return RedirectResponse(nxt, status_code=302)

    @app.get("/setup/activating")
    async def activating_page(request: Request):
        """Shown while server restarts to load newly enabled modules."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return auth_shell(
            _activating_form(lang=get_lang(request)),
            title=page_title("page.activating_modules"),
        )

    @app.get("/setup/activating-status")
    async def activating_status(request: Request):
        """JSON endpoint polled by the activating page.

        Reads requested modules from config.toml, then queries the API for
        which are currently running.  Responses:
            phase=down    - API unreachable (restarting)
            phase=loading - API up but not all requested modules running yet
            phase=ready   - all requested modules are running
        """
        from starlette.responses import JSONResponse as _JSON
        from celerp.config import read_config as _read_config
        token = request.cookies.get(COOKIE_NAME)
        try:
            cfg = _read_config()
            requested: list[str] = list(cfg.get("modules", {}).get("enabled") or [])
        except Exception:
            requested = []

        if not token:
            return _JSON({"phase": "down", "requested": len(requested), "loaded": 0, "modules": []})

        try:
            async with api._client(token) as c:
                r = await c.get("/companies/me/modules")
        except Exception:
            return _JSON({"phase": "down", "requested": len(requested), "loaded": 0, "modules": []})

        if r.status_code != 200:
            return _JSON({"phase": "down", "requested": len(requested), "loaded": 0, "modules": []})

        all_modules: list[dict] = r.json()
        requested_set = set(requested)
        relevant = [m for m in all_modules if m["name"] in requested_set]
        loaded_count = sum(1 for m in relevant if m.get("running"))

        # Compare against modules actually found on disk (relevant), not all
        # requested names.  A requested name that doesn't exist as a directory
        # should never block the activation page.
        phase = "ready" if (requested and loaded_count >= len(relevant)) else "loading"
        return _JSON({
            "phase": phase,
            "requested": len(requested),
            "loaded": loaded_count,
            "modules": [
                {"name": m["name"], "label": m.get("label") or m["name"], "running": m.get("running", False)}
                for m in relevant
            ],
        })

    @app.get("/setup/new-company")
    async def new_company_page(request: Request):
        """Entry point for adding a second (or nth) company workspace: start
        fresh, move a company in from another system, restore a company backup, or
        try the sample company."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        from ui.routes.company_backup import NEW_COMPANY
        from ui.routes.migrations import COMPANY, chooser, choice_card
        deactivated = request.query_params.get("reason", "") == "deactivated"
        # A deactivated company leaves nothing to go back to.
        back = "" if deactivated else P(
            A(t("btn.back"), href="/settings/general?tab=company", cls="auth-link"),
            cls="auth-alt-action",
        )
        return auth_shell(
            flash(t("setup.company_deactivated_notice"), kind="info") if deactivated else "",
            chooser(
                t("setup.new_company_title"),
                t("setup.new_company_choose_subtitle"),
                [
                    choice_card(t("setup.card_fresh"), t("setup.card_new_desc"), href="/setup/new-company/fresh"),
                    choice_card(t("setup.card_move"), t("setup.card_move_desc"), href=COMPANY.base),
                    choice_card(t("setup.card_restore_from_backup"), t("setup.card_restore_desc"),
                                href=NEW_COMPANY.base),
                    choice_card(t("setup.card_sample"), t("setup.card_sample_desc"), post_to=f"{COMPANY.base}/sample"),
                ],
                back,
            ),
            title=page_title("setup.new_company_title"),
        )

    @app.get("/setup/new-company/fresh")
    async def new_company_fresh_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return auth_shell(
            _new_company_form(error=request.query_params.get("error", ""), lang=get_lang(request)),
            title=page_title("setup.new_company_title"),
        )

    @app.post("/setup/new-company")
    async def new_company_submit(request: Request):
        """Create company, switch token, drop into the setup wizard."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        company_name = str(form.get("company_name", "")).strip()
        if not company_name:
            return RedirectResponse("/setup/new-company/fresh?error=Company+name+required", status_code=302)
        from ui.api_client import create_company as api_create
        try:
            new_access, new_refresh = await api_create(token, company_name)
        except APIError as e:
            import urllib.parse
            return RedirectResponse(f"/setup/new-company/fresh?error={urllib.parse.quote(e.detail)}", status_code=302)
        from ui.config import set_session_cookies
        resp = RedirectResponse("/setup/company", status_code=302)
        set_session_cookies(resp, new_access, new_refresh, request)
        return resp

    @app.get("/setup/cloud")
    async def cloud_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return auth_shell(
            _cloud_form(),
            title=page_title("page.connect_to_celerp_connect"),
        )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _company_setup_form(values: dict, error: str | None = None) -> FT:
    """The retry page: only the choices setup still needs. Reached when the last
    step of first-run setup failed, from the dashboard banner, or after adding a
    company."""
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("setup.finish_title"), cls="auth-title"),
            cls="auth-header",
        ),
        flash(error) if error else "",
        Form(
            *company_choice_fields(values.get("currency", ""), values.get("vertical", "")),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            P(A(t("setup.finish_later"), href="/dashboard", cls="auth-link"), cls="auth-alt-action"),
            method="post", action="/setup/company", cls="auth-form",
        ),
        company_choice_script(),
        cls="auth-card",
    )


def _activating_form(lang: str = "en") -> FT:
    """Spinner page shown while server restarts to load new modules."""
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.activating_your_modules"), cls="auth-title"),
            P(t("setup.your_erp_is_being_configured_this_takes_just_a_mom"), cls="auth-subtitle"),
            cls="auth-header",
        ),
        Div(
            Div(cls="activating-spinner"),
            P(t("setup.applying_configuration"), id="activating-status", cls="activating-status"),
            Div(id="activating-modules", cls="activating-modules"),
            # Shown by the script when the modules do not come up within the wait:
            # an honest failure with a way on, never an endless spinner.
            P(A(t("btn.retry", lang), href="/setup/activating", cls="auth-link"),
              " \u00b7 ",
              A(t("setup.open_dashboard", lang), href="/dashboard", cls="auth-link"),
              id="activating-failed", cls="auth-alt-action", hidden=True),
            cls="activating-body",
        ),
        Script(f"""
(function() {{
  var statusEl = document.getElementById('activating-status');
  var modulesEl = document.getElementById('activating-modules');
  var msgActivatingXofY = {json.dumps(t("setup.activating_module_x_of_y", lang))};
  var msgLoadingModules = {json.dumps(t("setup.loading_modules", lang))};
  var msgAllLoaded = {json.dumps(t("setup.all_modules_loaded", lang))};
  var msgTimedOut = {json.dumps(t("setup.activating_timed_out", lang))};
  var msgRestarting = {json.dumps(t("setup.restarting_server", lang))};
  var msgApplying = {json.dumps(t("setup.applying_configuration", lang))};
  var msgModulesFailed = {json.dumps(t("setup.modules_failed_to_start", lang))};
  var failedEl = document.getElementById('activating-failed');
  var spinnerEl = document.querySelector('.activating-spinner');
  var attempts = 0;
  // About two minutes of polling, then the failure message.
  var maxAttempts = 150;
  var downSeen = false;
  // Track whether we've seen ready, and require a brief stability window
  // before redirecting (the UI server itself restarts alongside the API,
  // so the first 'ready' response may be the last one before the UI goes down).
  var readyAt = null;
  var readyStableMs = 3000;
  // Track consecutive loading responses to detect stuck modules.
  var loadingStreak = 0;
  var maxLoadingStreak = 30;

  function showError(message, modules) {{
    statusEl.textContent = message;
    statusEl.classList.add('activating-status--error');
    if (spinnerEl) spinnerEl.hidden = true;
    failedEl.hidden = false;
    renderModules(modules, true);
  }}

  function renderModules(modules, failed) {{
    modulesEl.textContent = '';
    if (!modules || modules.length === 0) return;
    var list = document.createElement('ul');
    list.className = 'activating-module-list';
    for (var i = 0; i < modules.length; i++) {{
      var m = modules[i];
      var li = document.createElement('li');
      var state = m.running ? 'done' : (failed ? 'error' : 'pending');
      li.className = 'activating-module activating-module--' + state;
      li.textContent = (m.running ? '\u2713' : (failed ? '\u2717' : '\u25cc')) + ' ' + (m.label || m.name);
      list.appendChild(li);
    }}
    modulesEl.appendChild(list);
  }}

  function poll() {{
    attempts++;
    if (attempts > maxAttempts) {{
      showError(msgTimedOut, null);
      return;
    }}
    fetch('/setup/activating-status', {{cache: 'no-store'}})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.phase === 'down') {{
          downSeen = true;
          readyAt = null;
          loadingStreak = 0;
          statusEl.textContent = msgRestarting;
          modulesEl.textContent = '';
          setTimeout(poll, 600);
        }} else if (data.phase === 'loading') {{
          downSeen = true;
          readyAt = null;
          loadingStreak++;
          if (loadingStreak > maxLoadingStreak) {{
            showError(msgModulesFailed, data.modules);
            return;
          }}
          var loaded = data.loaded || 0;
          var total = data.requested || 0;
          statusEl.textContent = total > 0
            ? msgActivatingXofY.replace('{{loaded}}', loaded).replace('{{total}}', total)
            : msgLoadingModules;
          renderModules(data.modules);
          setTimeout(poll, 700);
        }} else if (data.phase === 'ready') {{
          loadingStreak = 0;
          statusEl.textContent = msgAllLoaded;
          renderModules(data.modules);
          if (!readyAt) {{ readyAt = Date.now(); }}
          // Wait for the UI itself to be stable after its own restart
          if (Date.now() - readyAt >= readyStableMs) {{
            window.location.href = '/dashboard';
          }} else {{
            setTimeout(poll, 600);
          }}
        }} else {{
          setTimeout(poll, 800);
        }}
      }})
      .catch(function() {{
        // Network error: either still restarting or not yet down
        readyAt = null;
        loadingStreak = 0;
        if (!downSeen) {{
          statusEl.textContent = msgApplying;
        }} else {{
          statusEl.textContent = msgRestarting;
        }}
        setTimeout(poll, 600);
      }});
  }}

  // Give the /system/restart background task ~600ms to fire before first poll
  setTimeout(poll, 600);
}})();
"""),
        cls="auth-card",
    )


def _cloud_form() -> FT:
    from ui.components.cloud_gate import is_partner_managed, direct_price, commercial_cta
    from ui.i18n import current_lang
    partner = is_partner_managed()
    # Keep the CTA label in lockstep with its click destination: the direct
    # price/trial label only on a celerp_direct install, and a partner-support /
    # Contact-Celerp label with the matching href on a partner-managed or unknown
    # install, so this setup card never shows a direct label that opens partner
    # support or Enterprise.
    cloud_href, cloud_cta_label = commercial_cta(
        "subscribe", "cloud",
        t("btn.get_connect"),
        current_lang())

    _features = [
        ("🔗", t("setup.feature_connectors_title"), t("setup.feature_connectors_desc")),
        ("☁", t("setup.feature_backup_title"), t("setup.feature_backup_desc")),
        ("🌐", t("setup.feature_web_title"), t("setup.feature_web_desc")),
        ("✨", t("setup.feature_ai_title"), t("setup.feature_ai_desc")),
    ]
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("page.one_last_thing"), cls="auth-title"),
            P(
                t("setup.cloud_subtitle"),
                cls="auth-subtitle",
            ),
            cls="auth-header",
        ),
        Div(
            Div(
                Div(
                    Span(t("setup.cloud"), cls="cloud-upsell-plan-name"),
                    # Prices are shown only where the app has an authoritative
                    # live catalog; setup never invents a stale direct amount.
                    cls="cloud-upsell-plan-header",
                ),
                Ul(
                    *[
                        Li(
                            Span(icon, cls="cloud-upsell-icon"),
                            Div(
                                Strong(title),
                                Span(f" - {desc}", cls="cloud-upsell-feat-desc"),
                            ),
                            cls="cloud-upsell-feature",
                        )
                        for icon, title, desc in _features
                    ],
                    cls="cloud-upsell-features",
                ),
                cls="cloud-upsell-card",
            ),
            cls="cloud-upsell-wrap",
        ),
        Div(
            A(cloud_cta_label,
                href=cloud_href,
                target="_blank",
                cls="btn btn--primary btn--full",
            ),
            A(
                t("setup.skip_for_now"),
                href="/dashboard",
                cls="cloud-upsell-skip",
            ),
            # The see-all-plans link points at direct Celerp pricing, so it is
            # shown only on a direct install; a partner-managed setup omits it.
            (Div(
                A(t("setup.see_all_plans"), href="https://celerp.com/pricing", target="_blank",
                  cls="cloud-upsell-compare"),
                cls="cloud-upsell-compare-wrap",
            ) if not partner else None),
            cls="cloud-upsell-actions",
        ),
        cls="auth-card",
    )


def _new_company_form(error: str = "", lang: str = "en") -> FT:
    """Simple name-entry form for creating a new company workspace."""
    back_link = P(A(t("btn.back", lang), href="/setup/new-company", cls="auth-link"), cls="auth-footer-text")
    return Div(
        Div(
            Img(src="/static/logo.png", alt="Celerp", cls="auth-logo"),
            H1(t("setup.new_company_title", lang), cls="auth-title"),
            P(t("setup.new_company_subtitle", lang), cls="auth-subtitle"),
            cls="auth-header",
        ),
        flash(error) if error else "",
        Form(
            Div(
                Label(t("label.company_name", lang), fr="company_name", cls="form-label"),
                Input(
                    type="text", id="company_name", name="company_name",
                    placeholder=t("settings.new_company_name_placeholder", lang),
                    required=True, autofocus=True, cls="form-input",
                ),
                cls="form-group",
            ),
            Button(t("btn.continue", lang), type="submit", cls="btn btn--primary btn--full mt-sm"),
            back_link,
            method="post", action="/setup/new-company", cls="auth-form",
        ),
        cls="auth-card",
    )
