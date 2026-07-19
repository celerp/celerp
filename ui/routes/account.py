# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The "Celerp account" surface - ONE component for binding a verified email to
this install (which unlocks marketplace purchases and Connect features).

Distinct from the local company-user login (email + password, who is using this
install): this is the relay account (what this install is entitled to), and it
has no passwords - two doors, one outcome:
  - Continue with email: magic sign-in link (the baseline, always available)
  - Continue with Google: only rendered when the relay reports it configured

The component is intent-aware: the default leads with signup; the claim-led
variant (used by the Settings Connect page, and reached via "Already
subscribed?") leads with linking an existing subscription. Every entry point
shares this one component - no surface grows its own sign-in variant.

While a sign-in is pending, a bounded poll (Cancel + timeout message, same
contract as the marketplace buy panel) watches the account state and, once the
email verifies, chains into activation so an entitled account comes up with its
gateway credentials without another click.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request

import ui.api_client as api
from ui.api_client import APIError
from ui.i18n import t, get_lang
from ui.routes.settings import _token

POLL_MAX = 100   # 100 x 3s = 5 minutes, then stop polling


def _panel_target(panel_id: str) -> dict:
    return {"hx_target": f"#{panel_id}", "hx_swap": "outerHTML"}


def _signup_body(lang: str, panel_id: str, google: bool) -> list:
    parts = [
        H4(t("account.title", lang), style="margin:0 0 4px;"),
        P(t("account.hint", lang), cls="settings-hint", style="margin-bottom:12px;"),
        Form(
            Input(type="email", name="email", required=True,
                  placeholder=t("account.email_placeholder", lang),
                  cls="input input--sm", style="width:260px;"),
            Button(t("btn.continue_with_email", lang), type="submit",
                   cls="btn btn--sm btn--primary", style="margin-left:8px;"),
            hx_post=f"/account/email?panel={panel_id}",
            **_panel_target(panel_id),
            style="display:flex;align-items:center;",
        ),
        P(t("account.email_hint", lang), cls="settings-hint",
          style="margin:6px 0 12px;"),
    ]
    if google:
        parts.append(Div(
            Button(t("btn.continue_with_google", lang),
                   hx_get=f"/account/google?panel={panel_id}",
                   **_panel_target(panel_id),
                   hx_disabled_elt="this",
                   cls="btn btn--sm btn--secondary"),
            style="margin-bottom:12px;",
        ))
    parts.append(P(
        A(t("page.already_subscribed", lang),
          hx_get=f"/account/panel?intent=claim&panel={panel_id}",
          **_panel_target(panel_id), href="#", cls="settings-hint"),
        style="margin-top:4px;",
    ))
    return parts


def _claim_body(lang: str, panel_id: str) -> list:
    """The linking flow for an existing subscriber - the shipped auto-connect +
    email-claim block, with the signup entry de-emphasized beneath it."""
    return [
        H4(t("page.already_subscribed", lang), style="margin:0 0 4px;"),
        P(t("settings.if_you_already_subscribed_on_the_website_we_can_li", lang),
          cls="settings-hint", style="margin-bottom:12px;"),
        # Auto-connect button (tries to match by instance_id)
        Div(
            Button(t("btn.connect_automatically", lang),
                   cls="btn btn--primary",
                   hx_post="/settings/cloud-activate",
                   **_panel_target(panel_id),
                   hx_indicator="#cloud-connecting",
                   id="cloud-connect-btn"),
            Span(t("settings.connecting", lang), id="cloud-connecting",
                 cls="settings-hint htmx-indicator",
                 style="margin-left:12px;display:none;"),
            style="margin-bottom:16px;",
        ),
        Script("""
(function(){
  if (sessionStorage.getItem('cloud_activate_tried')) return;
  sessionStorage.setItem('cloud_activate_tried', '1');
  var btn = document.getElementById('cloud-connect-btn');
  if (btn) htmx.trigger(btn, 'click');
})();
"""),
        # Email claim form (always visible)
        P(t("settings.or_enter_the_email_address_you_used_at_checkout", lang),
          cls="settings-hint"),
        Form(
            Input(type="email", name="claim_email", required=True,
                  placeholder=t("account.claim_email_placeholder", lang),
                  cls="input input--sm", style="width:260px;"),
            Button(t("btn.link_subscription", lang), type="submit",
                   cls="btn btn--sm btn--outline", style="margin-left:8px;"),
            hx_post="/settings/cloud-send-otp",
            **_panel_target(panel_id),
            style="display:flex;align-items:center;margin-top:8px;",
        ),
        P(
            A(t("account.new_here", lang),
              hx_get=f"/account/panel?intent=signup&panel={panel_id}",
              **_panel_target(panel_id), href="#", cls="settings-hint"),
            style="margin-top:12px;",
        ),
    ]


def account_panel(lang: str, *, intent: str = "signup",
                  panel_id: str = "celerp-account-panel",
                  google: bool = False, error: str | None = None) -> FT:
    """The one account surface. `panel_id` lets a host page keep its own swap
    target (the Settings Connect page uses cloud-relay-tab so the shipped
    claim endpoints keep replacing the same element)."""
    body = _claim_body(lang, panel_id) if intent == "claim" \
        else _signup_body(lang, panel_id, google)
    if error:
        body.insert(1, Div(error, cls="flash flash--error"))
    return Div(*body, id=panel_id, cls="cloud-connect-section")


def _waiting_panel(lang: str, panel_id: str, mode: str, n: int = 0) -> FT:
    """Bounded-poll waiting state while the browser round-trip is pending."""
    text_key = "account.waiting_google" if mode == "google" else "account.waiting_email"
    parts = [
        H4(t("account.title", lang), style="margin:0 0 4px;"),
        P(t(text_key, lang), cls="text-muted"),
        Div(
            Button(t("btn.cancel", lang),
                   hx_get=f"/account/panel?intent=signup&panel={panel_id}",
                   **_panel_target(panel_id),
                   cls="btn btn--sm btn--secondary"),
            style="display:flex;gap:8px;",
        ),
    ]
    if n < POLL_MAX:
        parts.append(Div(
            hx_get=f"/account/poll?panel={panel_id}&mode={mode}&n={n + 1}",
            hx_trigger="every 3s",
            **_panel_target(panel_id),
            id="account-poll"))
    else:
        parts.append(P(t("account.waiting_timeout", lang), cls="flash flash--warning"))
    return Div(*parts, id=panel_id, cls="cloud-connect-section")


def _signed_in_panel(lang: str, panel_id: str, status: dict) -> FT:
    email = status.get("email") or ""
    tier = status.get("tier") or "free"
    parts = [
        H4(t("account.title", lang), style="margin:0 0 4px;"),
        P(t("account.signed_in_as", lang, email=email)),
    ]
    if status.get("pending_selection"):
        parts.append(P(t("account.choose_subscription", lang), cls="settings-hint"))
        parts.append(Button(t("btn.link_subscription", lang),
                            hx_get=f"/account/panel?intent=claim&panel={panel_id}",
                            **_panel_target(panel_id),
                            cls="btn btn--sm btn--outline"))
    elif status.get("linked_elsewhere"):
        parts.append(P(t("account.linked_elsewhere", lang), cls="settings-hint"))
        parts.append(Button(t("btn.link_subscription", lang),
                            hx_get=f"/account/panel?intent=claim&panel={panel_id}",
                            **_panel_target(panel_id),
                            cls="btn btn--sm btn--outline"))
    elif tier == "free":
        parts.append(P(t("account.plan_free", lang), cls="settings-hint"))
    else:
        parts.append(P(t("account.plan", lang, tier=tier), cls="settings-hint"))
    return Div(*parts, id=panel_id, cls="cloud-connect-section")


def _panel_id_from(request: Request) -> str:
    pid = request.query_params.get("panel", "celerp-account-panel")
    # The id lands in HTML attributes; keep it to known targets.
    return pid if pid in ("celerp-account-panel", "cloud-relay-tab") else "celerp-account-panel"


def setup_routes(app):
    @app.get("/account/panel")
    async def account_panel_route(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token:
            return Div(id=panel_id)
        intent = request.query_params.get("intent", "signup")
        google = False
        if intent != "claim":
            try:
                google = bool((await api.account_methods(token)).get("google"))
            except APIError:
                pass
        return account_panel(lang, intent=intent, panel_id=panel_id, google=google)

    @app.post("/account/email")
    async def account_email(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token:
            return Div(id=panel_id)
        form = await request.form()
        email = str(form.get("email", "")).strip()
        try:
            res = await api.account_signup(token, email)
        except APIError as e:
            res = {"error": e.detail or str(e)}
        if res.get("error"):
            google = False
            try:
                google = bool((await api.account_methods(token)).get("google"))
            except APIError:
                pass
            return account_panel(lang, panel_id=panel_id, google=google,
                                 error=str(res["error"]))
        return _waiting_panel(lang, panel_id, "email")

    @app.get("/account/google")
    async def account_google(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token:
            return Div(id=panel_id)
        try:
            methods = await api.account_methods(token)
        except APIError:
            methods = {}
        url = methods.get("google_start_url", "")
        if not methods.get("google") or not url:
            return account_panel(lang, panel_id=panel_id, google=False,
                                 error=t("account.google_unavailable", lang))
        import json as _json
        panel = _waiting_panel(lang, panel_id, "google")
        # Open the system browser (Google refuses embedded webviews; same
        # pattern as the marketplace checkout).
        panel.children = (*panel.children, Script(
            f"(function(){{var u={_json.dumps(url)};"
            f"if(window.celerp&&window.celerp.openExternal){{window.celerp.openExternal(u);}}"
            f"else{{window.open(u,'_blank');}}}})();"))
        return panel

    @app.get("/account/poll")
    async def account_poll(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token:
            return Div(id=panel_id)
        mode = request.query_params.get("mode", "email")
        try:
            n = int(request.query_params.get("n", "0"))
        except ValueError:
            n = 0
        try:
            status = await api.account_status(token)
        except APIError:
            status = {}
        # claim_offer = a one-click confirmation is still open in the user's
        # browser tab; keep waiting so the panel lands on the final state.
        if status.get("email_verified") and not status.get("claim_offer"):
            # Chain into activation: a fresh account needs its gateway
            # credentials (an entitled one also gets its tunnel). Best-effort -
            # the account state is already correct on the relay. (The fixed
            # 3s poll interval can, on a slow response, fire twice before the
            # panel swaps - activation is idempotent, so the accepted worst
            # case is one redundant call.)
            try:
                await api.activate_relay(token)
            except Exception:
                pass
            return _signed_in_panel(lang, panel_id, status)
        return _waiting_panel(lang, panel_id, mode, n=n)
