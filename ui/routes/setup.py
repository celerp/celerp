# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company setup wizard - step 2 after bootstrap registration.

Flow:
    /setup           → step 1: create first admin + company name
    /setup/company   → step 2: company details + business type (vertical)
    /setup/cloud     → step 3: cloud upsell (optional)
    /onboarding      → data integration landing
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import json
import logging
from pathlib import Path

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import auth_shell, flash, page_title
from ui.components.currency import CURRENCIES, CURRENCY_CODES
from ui.config import COOKIE_NAME
from ui.i18n import t, get_lang
from celerp.config import set_enabled_modules as _set_enabled_modules

logger = logging.getLogger(__name__)

_PRESETS_DIR = (
    Path(__file__).parent.parent.parent
    / "default_modules" / "celerp-verticals" / "celerp_verticals" / "presets"
)
_CATEGORIES_DIR = (
    Path(__file__).parent.parent.parent
    / "default_modules" / "celerp-verticals" / "celerp_verticals" / "categories"
)


async def _seed_vertical_categories(token: str, vertical: str) -> int:
    """Seed category schemas for a vertical directly via core API (no verticals module needed).

    Reads the preset JSON, loads each category definition, and patches the schema
    into company settings via the always-available /me/category-schema/{category} endpoint.
    Returns the number of categories applied.
    """
    preset_file = _PRESETS_DIR / f"{vertical}.json"
    if not preset_file.exists():
        return 0
    preset = json.loads(preset_file.read_text())
    applied = 0
    for cat_name in (preset.get("categories") or []):
        cat_file = _CATEGORIES_DIR / f"{cat_name}.json"
        if not cat_file.exists():
            continue
        cat = json.loads(cat_file.read_text())
        display_name = cat.get("display_name", cat_name)
        fields = cat.get("fields") or []
        try:
            await api.patch_category_schema(token, display_name, fields)
            applied += 1
        except Exception:
            pass
    return applied


def _load_verticals() -> list[tuple[str, str]]:
    """Load vertical options from preset files. Returns [(value, label), ...].

    'blank' preset sorts last. All others sort alphabetically by display_name.
    Presets flagged "hidden": true are skipped — used to stage a vertical that the
    product can't yet honestly support (see the preset's "hidden_reason").
    """
    pinned_last: list[tuple[str, str]] = []
    options: list[tuple[str, str]] = []
    if _PRESETS_DIR.exists():
        for p in sorted(_PRESETS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                if data.get("hidden"):
                    continue
                entry = (data["name"], data["display_name"])
                if data["name"] == "blank":
                    pinned_last.append(entry)
                else:
                    options.append(entry)
            except Exception:
                pass
    options.sort(key=lambda x: x[1])
    return options + pinned_last

_TIMEZONES = [
    "Asia/Bangkok", "Asia/Singapore", "Asia/Tokyo", "Asia/Hong_Kong",
    "Asia/Kolkata", "Europe/London", "Europe/Paris", "America/New_York",
    "America/Los_Angeles", "UTC",
]
_VERTICALS = _load_verticals()


def setup_routes(app):

    @app.get("/setup/company")
    async def company_details_page(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            company = await api.get_company(token)
        except APIError:
            company = {}
        return auth_shell(
            _company_details_form(company, lang=get_lang(request)),
            title=page_title("page.company_setup"),
        )

    @app.post("/setup/company")
    async def company_details_submit(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()

        data = {
            "currency": str(form.get("currency", "THB")),
            "timezone": str(form.get("timezone", "Asia/Bangkok")),
            "tax_id": str(form.get("tax_id", "")).strip(),
            "phone": str(form.get("phone", "")).strip(),
            "address": str(form.get("address", "")).strip(),
        }

        if data["currency"] not in CURRENCY_CODES:
            try:
                company = await api.get_company(token)
            except APIError:
                company = {}
            return auth_shell(
                _company_details_form(company, error=t("setup.invalid_currency", value=repr(data["currency"])), lang=get_lang(request)),
                title=page_title("page.company_setup"),
            )

        try:
            await api.patch_company(token, data)
        except APIError as e:
            try:
                company = await api.get_company(token)
            except APIError:
                company = {}
            return auth_shell(
                _company_details_form(company, error=e.detail, lang=get_lang(request)),
                title=page_title("page.company_setup"),
            )

        # Mirror the company identity onto the self-contact - the Company Details page and the document
        # letterhead read the self-contact, not company settings / the Location. address -> Head Office
        # location + the self-contact billing address; tax_id / phone -> the self-contact fields.
        address_text = data.get("address", "").strip()
        try:
            company = await api.get_company(token)
            sid = (company.get("settings") or {}).get("self_contact_id")
            if address_text:
                locs_resp = await api.get_locations(token)
                locs = locs_resp.get("items") or locs_resp.get("locations") or (locs_resp if isinstance(locs_resp, list) else [])
                default_loc = next((l for l in locs if l.get("is_default")), None)
                if default_loc:
                    await api.patch_location(token, str(default_loc["id"]), {"address": {"text": address_text}})
            if sid:
                self_contact = await api.get_contact(token, sid)
                if address_text and not (self_contact.get("addresses") or []):
                    await api.add_contact_address(token, sid, {"address_type": "billing",
                                                               "line1": address_text, "is_default": True})
                identity = {k: data.get(k) for k in ("tax_id", "phone")
                            if str(data.get(k) or "").strip() and not str(self_contact.get(k) or "").strip()}
                if identity:
                    await api.patch_contact(token, sid, identity)
        except Exception:
            pass

        # Apply vertical preset if one was chosen (blank = no preset, no-op)
        vertical = str(form.get("vertical", "blank"))
        if vertical != "blank":
            preset_file = _PRESETS_DIR / f"{vertical}.json"
            if preset_file.exists():
                preset = json.loads(preset_file.read_text())
                preset_modules: list[str] = preset.get("modules") or []
                if preset_modules:
                    _set_enabled_modules(preset_modules)
                    # Sync enabled state into company settings (DB) so the modules tab
                    # shows the correct enabled/disabled badge without a manual toggle.
                    for mod_name in preset_modules:
                        try:
                            async with api._client(token) as c:
                                await c.post(f"/companies/me/modules/{mod_name}/enable")
                        except Exception:
                            pass
            # Seed category schemas for this vertical directly (bypasses verticals module
            # which may not be loaded yet — core patch_category_schema is always available)
            try:
                await _seed_vertical_categories(token, vertical)
            except Exception:
                pass
            # Store the vertical name in company settings so the dashboard can use it
            try:
                async with api._client(token) as c:
                    raw = api._raise(await c.get("/companies/me")).json()
                    settings = dict(raw.get("settings") or {})
                    settings["vertical"] = vertical
                    api._raise(await c.patch("/companies/me", json={"name": raw.get("name", ""), "settings": settings}))
            except Exception as exc:
                logger.warning("storing vertical failed during setup wizard: %s", exc)
            # Re-seed demo items with vertical-aware examples now that vertical is set
            try:
                async with api._client(token) as c:
                    r = await c.post(f"/companies/me/demo/reseed?vertical={vertical}")
                    logger.info("demo reseed response: status=%s body=%s", r.status_code, r.text[:200])
            except Exception as exc:
                logger.warning("demo reseed failed during setup wizard: %s", exc)
            # Trigger graceful API restart so new modules load (sentinel written by /system/restart)
            try:
                async with api._client(token) as c:
                    await c.post("/system/restart")
            except Exception:
                pass
            return RedirectResponse("/setup/activating", status_code=302)

        return RedirectResponse("/setup/cloud", status_code=302)

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
            phase=down    — API unreachable (restarting)
            phase=loading — API up but not all requested modules running yet
            phase=ready   — all requested modules are running
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

    # Redirect legacy setup steps to the correct current step
    @app.get("/setup/users")
    async def users_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    @app.post("/setup/users")
    async def users_post_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    @app.post("/setup/users/done")
    async def users_done_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    @app.get("/setup/vertical")
    async def vertical_redirect(request: Request):
        return RedirectResponse("/setup/company", status_code=302)

    @app.post("/setup/vertical")
    async def vertical_post_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    @app.get("/setup/modules")
    async def modules_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    @app.post("/setup/modules")
    async def modules_post_redirect(request: Request):
        return RedirectResponse("/setup/cloud", status_code=302)

    # ------------------------------------------------------------------
    # Step 3: cloud upsell (optional)
    # ------------------------------------------------------------------

    @app.get("/setup/new-company")
    async def new_company_page(request: Request):
        """Entry point for adding a second (or nth) company workspace."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        error = request.query_params.get("error", "")
        reason = request.query_params.get("reason", "")
        notice = flash(t("setup.company_deactivated_notice"), kind="info") if reason == "deactivated" else ""
        return auth_shell(
            Div(notice, _new_company_form(error=error, lang=lang, hide_back=reason == "deactivated")) if notice else _new_company_form(error=error, lang=lang),
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
            return RedirectResponse("/setup/new-company?error=Company+name+required", status_code=302)
        from ui.api_client import create_company as api_create
        try:
            new_token = await api_create(token, company_name)
        except APIError as e:
            import urllib.parse
            return RedirectResponse(f"/setup/new-company?error={urllib.parse.quote(e.detail)}", status_code=302)
        from celerp.config import settings as _s
        from ui.config import cookie_domain
        resp = RedirectResponse("/setup/company", status_code=302)
        resp.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax", max_age=900, secure=_s.cookie_secure, domain=cookie_domain(request))
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

def _wizard_steps(current: int, lang: str = "en") -> FT:
    steps = [t("setup.welcome", lang), t("setup.company_details", lang), t("setup.cloud", lang)]
    return Div(
        *[
            Div(
                Span(str(i + 1), cls=f"step-num {'step-num--active' if i + 1 == current else 'step-num--done' if i + 1 < current else ''}"),
                Span(label, cls=f"step-label {'step-label--active' if i + 1 == current else ''}"),
                cls="wizard-step",
            )
            for i, label in enumerate(steps)
        ],
        cls="wizard-steps",
    )


def _company_details_form(company: dict, error: str | None = None, lang: str = "en") -> FT:
    # company is already flattened by api.get_company (_flatten_company); fall back to settings sub-dict too
    s = {**(company.get("settings") or {}), **company}
    return Div(
        Form(
            _wizard_steps(2, lang=lang),
            Div(
                H1(t("page.company_details"), cls="auth-title"),
                P(t("setup.tell_us_a_bit_more_about_your_company"), cls="auth-subtitle"),
                cls="auth-header",
            ),
            flash(error) if error else "",
            Div(
                Label(t("label.tax_id_vat_number"), For="tax_id", cls="form-label"),
                Input(type="text", id="tax_id", name="tax_id",
                      value=s.get("tax_id", ""), placeholder="0123456789012",
                      cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("th.address"), For="address", cls="form-label"),
                Textarea(s.get("address", ""), id="address", name="address",
                         placeholder=t("setup.address_placeholder"),
                         rows="3", cls="form-input form-textarea"),
                cls="form-group",
            ),
            Div(
                Label(t("th.phone"), For="phone", cls="form-label"),
                Input(type="tel", id="phone", name="phone",
                      value=s.get("phone", ""), placeholder="+66 2 123 4567",
                      cls="form-input"),
                cls="form-group",
            ),
            Div(
                Label(t("th.currency"), For="currency", cls="form-label"),
                Input(
                    type="text", id="currency", name="currency",
                    value=s.get("currency", "THB"),
                    placeholder=t("setup.currency_search_placeholder"),
                    list="currency-list",
                    autocomplete="off",
                    cls="form-input",
                ),
                Datalist(
                    *[Option(label, value=code) for code, label in CURRENCIES],
                    id="currency-list",
                ),
                cls="form-group",
            ),
            Div(
                Label(t("label.timezone"), For="timezone", cls="form-label"),
                Select(
                    *[Option(tz, value=tz, selected=(tz == s.get("timezone", "Asia/Bangkok"))) for tz in _TIMEZONES],
                    id="timezone", name="timezone", cls="form-input",
                ),
                cls="form-group",
            ),
            Div(
                Label(t("label.business_type"), For="vertical", cls="form-label"),
                Select(
                    *[Option(label, value=val, selected=(val == s.get("vertical", "general")))
                      for val, label in _VERTICALS],
                    id="vertical", name="vertical", cls="form-input",
                ),
                cls="form-group",
            ),
            Button(t("btn.continue"), type="submit", cls="btn btn--primary btn--full"),
            method="post", action="/setup/company", cls="auth-form",
        ),
        cls="auth-card auth-card--wide",
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
            cls="activating-body",
        ),
        Script(f"""
(function() {{
  var statusEl = document.getElementById('activating-status');
  var modulesEl = document.getElementById('activating-modules');
  var msgActivatingXofY = {json.dumps(t("setup.activating_module_x_of_y", lang))};
  var msgLoadingModules = {json.dumps(t("setup.loading_modules", lang))};
  var msgAllLoaded = {json.dumps(t("setup.all_modules_loaded", lang))};
  var msgTakingLonger = {json.dumps(t("setup.taking_longer", lang))};
  var msgRestarting = {json.dumps(t("setup.restarting_server", lang))};
  var msgApplying = {json.dumps(t("setup.applying_configuration", lang))};
  var msgModulesFailed = {json.dumps(t("setup.modules_failed_to_start", lang))};
  var msgGoBack = {json.dumps(t("setup.go_back_to_setup", lang))};
  var attempts = 0;
  var maxAttempts = 60;
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
    statusEl.innerHTML = '<span style="color:#c0392b;font-weight:600;">' + message + '</span>' +
      ' <a href="/setup" style="color:#2980b9;text-decoration:underline;">' + msgGoBack + '</a>';
    if (modules && modules.length > 0) {{
      var html = '<ul class="activating-module-list">';
      for (var i = 0; i < modules.length; i++) {{
        var m = modules[i];
        var icon = m.running ? '✓' : '✗';
        var cls = m.running ? 'activating-module activating-module--done' : 'activating-module activating-module--error';
        html += '<li class="' + cls + '">' + icon + ' ' + (m.label || m.name) + '</li>';
      }}
      html += '</ul>';
      modulesEl.innerHTML = html;
    }}
  }}

  function renderModules(modules) {{
    if (!modules || modules.length === 0) {{ modulesEl.innerHTML = ''; return; }}
    var html = '<ul class="activating-module-list">';
    for (var i = 0; i < modules.length; i++) {{
      var m = modules[i];
      var icon = m.running ? '✓' : '◌';
      var cls = m.running ? 'activating-module activating-module--done' : 'activating-module activating-module--pending';
      html += '<li class="' + cls + '">' + icon + ' ' + (m.label || m.name) + '</li>';
    }}
    html += '</ul>';
    modulesEl.innerHTML = html;
  }}

  function poll() {{
    attempts++;
    if (attempts > maxAttempts) {{
      statusEl.textContent = msgTakingLonger;
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
          modulesEl.innerHTML = '';
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
        // Network error — either still restarting or not yet down
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
    from celerp.config import settings
    from celerp.gateway.state import build_commercial_handoff
    from ui.components.cloud_gate import is_partner_managed, direct_price
    iid = settings.gateway_instance_id
    subscribe_url = build_commercial_handoff(iid, "subscribe", "cloud")
    partner = is_partner_managed()

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
                    # Partner-managed: the partner sets its own price, so the
                    # setup card shows no direct Celerp figure.
                    (Div(
                        Span("$29", cls="cloud-upsell-price"),
                        Span(t("setup._month"), cls="cloud-upsell-price-unit"),
                    ) if not partner else None),
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
            A(direct_price(t("setup.subscribe_29mo")) or t("btn.get_connect"),
                href=subscribe_url,
                target="_blank",
                cls="btn btn--primary btn--full",
            ),
            A(
                t("setup.skip_for_now"),
                href="/settings?setup=done",
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


def _new_company_form(error: str = "", lang: str = "en", hide_back: bool = False) -> FT:
    """Simple name-entry form for creating a new company workspace."""
    back_link = None if hide_back else P(
        A(t("btn.back_to_settings", lang), href="/settings/general?tab=company", cls="auth-link"),
        cls="auth-footer-text",
    )
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
