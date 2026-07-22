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

import json
import re

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.config import get_role as _get_role
from ui.i18n import t, get_lang
from ui.routes.settings import _token
from celerp.services.auth import ROLE_LEVELS as _ROLE_LEVELS

POLL_MAX = 100   # 100 x 3s = 5 minutes, then stop polling

# Sentinel returned by account_gate when the account state could not be
# determined (relay unreachable). Distinct from None (verified, proceed) and
# from a panel (sign in first) so each caller picks its own fail-open or
# fail-closed behavior.
GATE_UNREACHABLE = object()

# The staged continuation a sign-in panel carries so the interrupted action
# resumes once the account verifies. Closed grammar - anything else is dropped.
_NEXT_RES = (
    re.compile(r"^buy:[a-z0-9_-]+:(monthly|once)$"),
    re.compile(r"^community:[a-z0-9_-]+$"),
    re.compile(r"^doc-send$"),
)

# Where the gate's Cancel returns to, per host panel. The settings-page panels
# are embedded in their page and need no way-back button.
_CANCEL_URLS = {
    "marketplace-panel": "/modules/marketplace-panel",
    "community-zone": "/modules/community-panel",
}

_GATE_BENEFIT_KEYS = {
    "buy": "account.gate_buy_benefit",
    "community": "account.gate_community_benefit",
    "doc-send": "account.gate_doc_send_benefit",
}


def _next_from(value: str | None) -> str | None:
    """Validate a staged continuation; invalid or absent -> None (the sign-in
    still works, it just resumes nothing)."""
    if not value:
        return None
    return value if any(rx.match(value) for rx in _NEXT_RES) else None


def _account_allowed(request: Request) -> bool:
    """Binding the relay account email is a billing-identity action (it decides
    who owns purchases and where subscription control lives) - admin and owner
    only, same gate as the Settings Connect page and the marketplace."""
    return _ROLE_LEVELS.get(_get_role(request), 0) >= _ROLE_LEVELS["admin"]


def _panel_target(panel_id: str) -> dict:
    return {"hx_target": f"#{panel_id}", "hx_swap": "outerHTML"}


def _with_next(url: str, next_action: str | None) -> str:
    return f"{url}&next={next_action}" if next_action else url


_GATE_ESC_SCRIPT = """
(function () {
  if (window.__celerpGateEsc) {
    document.removeEventListener('keydown', window.__celerpGateEsc);
  }
  window.__celerpGateEsc = function (e) {
    if (e.key === 'Escape') {
      var b = document.getElementById('account-gate-cancel');
      if (b) { b.click(); }
      document.removeEventListener('keydown', window.__celerpGateEsc);
      window.__celerpGateEsc = null;
    }
  };
  document.addEventListener('keydown', window.__celerpGateEsc);
})();
"""


def _cancel_parts(lang: str, panel_id: str, cancel_url: str | None) -> list:
    """The gate's way back: a Cancel button returning to the host panel, or -
    for the inline document-send offer, which has no panel to return to - a
    button that collapses the offer slot. Esc triggers the same button."""
    if cancel_url:
        btn = Button(t("btn.cancel", lang), id="account-gate-cancel", type="button",
                     hx_get=cancel_url, **_panel_target(panel_id),
                     cls="btn btn--sm btn--secondary")
    elif panel_id == "doc-send-offer":
        btn = Button(t("btn.cancel", lang), id="account-gate-cancel", type="button",
                     onclick=("var p=document.getElementById('doc-send-offer');"
                              "if(p){p.outerHTML='<div id=\"doc-send-offer\"></div>';}"),
                     cls="btn btn--sm btn--secondary")
    else:
        return []
    return [Div(btn, style="margin-top:8px;"), Script(_GATE_ESC_SCRIPT)]


def _signup_body(lang: str, panel_id: str, google: bool,
                 next_action: str | None = None, cancel_url: str | None = None,
                 free_quota: int = 0) -> list:
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
        # The privacy/marketing notice beside the account-creation action
        # (counsel review 2026-07-19, D.6.3): states the service use, the
        # intended future updates, and the free opt-out channel.
        P(t("account.signup_notice", lang), cls="settings-hint",
          style="margin:0 0 12px;font-size:12px;"),
    ]
    if next_action:
        parts[2].children = (Input(type="hidden", name="next", value=next_action),
                             *parts[2].children)
        # State the concrete reason the sign-in stands between the user and the
        # action they clicked, above the generic hint.
        kind = next_action.split(":", 1)[0]
        benefit = (t(_GATE_BENEFIT_KEYS[kind], lang, n=free_quota)
                   if kind == "doc-send" else t(_GATE_BENEFIT_KEYS[kind], lang))
        parts.insert(1, P(benefit, cls="settings-hint", style="margin-bottom:8px;"))
    if google:
        parts.append(Div(
            Button(t("btn.continue_with_google", lang),
                   hx_get=_with_next(f"/account/google?panel={panel_id}", next_action),
                   **_panel_target(panel_id),
                   hx_disabled_elt="this",
                   cls="btn btn--sm btn--secondary"),
            style="margin-bottom:12px;",
        ))
    parts.append(P(
        A(t("page.already_subscribed", lang),
          hx_get=_with_next(f"/account/panel?intent=claim&panel={panel_id}", next_action),
          **_panel_target(panel_id), href="#", cls="settings-hint"),
        style="margin-top:4px;",
    ))
    parts.extend(_cancel_parts(lang, panel_id, cancel_url))
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
    ]


def account_panel(lang: str, *, intent: str = "signup",
                  panel_id: str = "celerp-account-panel",
                  google: bool = False, error: str | None = None,
                  next_action: str | None = None, cancel_url: str | None = None,
                  free_quota: int = 0) -> FT:
    """The one account surface. `panel_id` lets a host page keep its own swap
    target (the Settings Connect page uses cloud-relay-tab so the shipped
    claim endpoints keep replacing the same element)."""
    body = _claim_body(lang, panel_id) if intent == "claim" \
        else _signup_body(lang, panel_id, google, next_action=next_action,
                          cancel_url=cancel_url, free_quota=free_quota)
    if error:
        body.insert(1, Div(error, cls="flash flash--error"))
    return Div(*body, id=panel_id, cls="cloud-connect-section")


async def account_gate(token: str, lang: str, next_action: str | None,
                       panel_id: str, cancel_url: str | None) -> FT | object | None:
    """The account check in front of an action that needs a verified email.

    Returns None when the bound account is verified (proceed with the action),
    GATE_UNREACHABLE when the state cannot be determined (caller decides
    fail-open or fail-closed), or the signup panel staging *next_action* for
    resume after sign-in. One live relay round-trip per call - the account
    state at the moment of the action is what gates it."""
    try:
        status = await api.account_status(token)
    except APIError:
        return GATE_UNREACHABLE
    if status.get("error"):
        return GATE_UNREACHABLE
    if status.get("email_verified"):
        return None
    google, quota = False, 0
    try:
        methods = await api.account_methods(token)
        google = bool(methods.get("google"))
        quota = int(methods.get("free_email_quota") or 0)
    except APIError:
        pass
    return account_panel(lang, intent="signup", panel_id=panel_id, google=google,
                         next_action=_next_from(next_action),
                         cancel_url=cancel_url, free_quota=quota)


def _waiting_panel(lang: str, panel_id: str, mode: str, n: int = 0,
                   next_action: str | None = None) -> FT:
    """Bounded-poll waiting state while the browser round-trip is pending."""
    text_key = "account.waiting_google" if mode == "google" else "account.waiting_email"
    parts = [
        H4(t("account.title", lang), style="margin:0 0 4px;"),
        P(t(text_key, lang), cls="text-muted"),
        Div(
            Button(t("btn.cancel", lang),
                   hx_get=_with_next(f"/account/panel?intent=signup&panel={panel_id}",
                                     next_action),
                   **_panel_target(panel_id),
                   cls="btn btn--sm btn--secondary"),
            style="display:flex;gap:8px;",
        ),
    ]
    if n < POLL_MAX:
        parts.append(Div(
            hx_get=_with_next(
                f"/account/poll?panel={panel_id}&mode={mode}&n={n + 1}", next_action),
            hx_trigger="every 3s",
            **_panel_target(panel_id),
            id="account-poll"))
    else:
        parts.append(P(t("account.waiting_timeout", lang), cls="flash flash--warning"))
    return Div(*parts, id=panel_id, cls="cloud-connect-section")


def _signed_in_panel(lang: str, panel_id: str, status: dict,
                     next_action: str | None = None) -> FT:
    email = status.get("email") or ""
    tier = status.get("tier") or "free"
    parts = [
        H4(t("account.title", lang), style="margin:0 0 4px;"),
        P(t("account.signed_in_as", lang, email=email)),
    ]
    claim_url = _with_next(f"/account/panel?intent=claim&panel={panel_id}", next_action)
    if status.get("pending_selection"):
        parts.append(P(t("account.choose_subscription", lang), cls="settings-hint"))
        parts.append(Button(t("btn.link_subscription", lang),
                            hx_get=claim_url,
                            **_panel_target(panel_id),
                            cls="btn btn--sm btn--outline"))
    elif status.get("linked_elsewhere"):
        parts.append(P(t("account.linked_elsewhere", lang), cls="settings-hint"))
        parts.append(Button(t("btn.link_subscription", lang),
                            hx_get=claim_url,
                            **_panel_target(panel_id),
                            cls="btn btn--sm btn--outline"))
    elif tier == "free":
        parts.append(P(t("account.plan_free", lang), cls="settings-hint"))
    else:
        parts.append(P(t("account.plan", lang, tier=tier), cls="settings-hint"))
    return Div(*parts, id=panel_id, cls="cloud-connect-section")


def _resume_guard_script(next_action: str, body: str) -> Script:
    """One-shot guard: the poll can render the signed-in panel more than once
    (slow response, back navigation); the staged action must fire once. The
    marker is per-continuation and expires so a later identical action still
    resumes."""
    return Script(
        f"(function(){{var k='celerp_resume::'+{json.dumps(next_action)};"
        "var last=parseInt(sessionStorage.getItem(k)||'0',10);"
        "if(Date.now()-last<30000){return;}"
        "sessionStorage.setItem(k,String(Date.now()));"
        f"{body}}})();")


def _resume_parts(next_action: str, panel_id: str) -> list:
    """The continuation fired after sign-in completes: re-issue the staged
    action exactly as the original control would have."""
    if next_action == "doc-send":
        # The document page's send affordance only exists on the connected
        # render; reload with a one-shot marker the page reads to auto-open
        # the send dialog.
        return [_resume_guard_script(next_action,
                                     "sessionStorage.setItem('celerp_open_send','1');"
                                     "location.reload();")]
    if next_action.startswith("buy:"):
        _, slug, kind = next_action.split(":")
        fire = Div(id="account-resume",
                   hx_post=f"/modules/buy?slug={slug}&kind={kind}",
                   hx_target="#marketplace-panel", hx_swap="outerHTML",
                   hx_trigger="celerp-resume")
    else:  # community:<id>
        module_id = next_action.split(":", 1)[1]
        fire = Div(id="account-resume",
                   hx_post="/modules/community-download",
                   hx_vals=json.dumps({"id": module_id, "zone": "1"}),
                   hx_target=f"#{panel_id}", hx_swap="outerHTML",
                   hx_trigger="celerp-resume")
    return [fire, _resume_guard_script(
        next_action,
        "var el=document.getElementById('account-resume');"
        "if(el){htmx.trigger(el,'celerp-resume');}")]


def _panel_id_from(request: Request) -> str:
    pid = request.query_params.get("panel", "celerp-account-panel")
    # The id lands in HTML attributes; keep it to known targets.
    allowed = ("celerp-account-panel", "cloud-relay-tab",
               "marketplace-panel", "community-zone", "doc-send-offer")
    return pid if pid in allowed else "celerp-account-panel"


def setup_routes(app):
    @app.get("/account/panel")
    async def account_panel_route(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token:
            return Div(id=panel_id)
        if not _account_allowed(request):
            # Below-admin click on an account entry point: say why nothing
            # opens instead of swapping in silence.
            return HTMLResponse(
                to_xml(Div(id=panel_id)),
                headers={"HX-Trigger": json.dumps({"celerpToast": {
                    "message": t("account.admin_only", lang), "type": "info"}})})
        intent = request.query_params.get("intent", "signup")
        next_action = _next_from(request.query_params.get("next"))
        google, quota = False, 0
        if intent != "claim":
            try:
                methods = await api.account_methods(token)
                google = bool(methods.get("google"))
                quota = int(methods.get("free_email_quota") or 0)
            except APIError:
                pass
        return account_panel(lang, intent=intent, panel_id=panel_id, google=google,
                             next_action=next_action,
                             cancel_url=_CANCEL_URLS.get(panel_id),
                             free_quota=quota)

    @app.post("/account/email")
    async def account_email(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token or not _account_allowed(request):
            return Div(id=panel_id)
        form = await request.form()
        email = str(form.get("email", "")).strip()
        next_action = _next_from(str(form.get("next", "")) or None)
        try:
            res = await api.account_signup(token, email)
        except APIError as e:
            res = {"error": e.detail or str(e)}
        if res.get("error"):
            google, quota = False, 0
            try:
                methods = await api.account_methods(token)
                google = bool(methods.get("google"))
                quota = int(methods.get("free_email_quota") or 0)
            except APIError:
                pass
            return account_panel(lang, panel_id=panel_id, google=google,
                                 error=str(res["error"]), next_action=next_action,
                                 cancel_url=_CANCEL_URLS.get(panel_id),
                                 free_quota=quota)
        return _waiting_panel(lang, panel_id, "email", next_action=next_action)

    @app.get("/account/google")
    async def account_google(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token or not _account_allowed(request):
            return Div(id=panel_id)
        next_action = _next_from(request.query_params.get("next"))
        try:
            methods = await api.account_methods(token)
        except APIError:
            methods = {}
        url = methods.get("google_start_url", "")
        if not methods.get("google") or not url:
            return account_panel(lang, panel_id=panel_id, google=False,
                                 error=t("account.google_unavailable", lang),
                                 next_action=next_action,
                                 cancel_url=_CANCEL_URLS.get(panel_id))
        panel = _waiting_panel(lang, panel_id, "google", next_action=next_action)
        # Open the system browser (Google refuses embedded webviews; same
        # pattern as the marketplace checkout).
        panel.children = (*panel.children, Script(
            f"(function(){{var u={json.dumps(url)};"
            f"if(window.celerp&&window.celerp.openExternal){{window.celerp.openExternal(u);}}"
            f"else{{window.open(u,'_blank');}}}})();"))
        return panel

    @app.get("/account/poll")
    async def account_poll(request: Request):
        token = _token(request)
        lang = get_lang(request)
        panel_id = _panel_id_from(request)
        if not token or not _account_allowed(request):
            return Div(id=panel_id)
        mode = request.query_params.get("mode", "email")
        next_action = _next_from(request.query_params.get("next"))
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
            act: dict = {}
            try:
                act = await api.activate_relay(token)
            except Exception:
                pass
            # On the Web Access page the whole chrome changes once the relay
            # comes up (value-prop landing -> connected tabs), so load the
            # connected page instead of swapping only the panel.
            if panel_id == "cloud-relay-tab" and act.get("connected"):
                return Response(status_code=204,
                                headers={"HX-Redirect": "/settings/cloud"})
            if act and not act.get("error"):
                # Activation bound the instance; show the account as the relay
                # sees it now (the pre-bind status is masked for privacy).
                try:
                    status = await api.account_status(token)
                except APIError:
                    pass
            panel = _signed_in_panel(lang, panel_id, status, next_action=next_action)
            if (next_action and not status.get("pending_selection")
                    and not status.get("linked_elsewhere")):
                # The staged action fires only once the account is settled on
                # THIS install; with a claim step still open it stays deferred
                # on the claim links instead.
                panel.children = (*panel.children,
                                  *_resume_parts(next_action, panel_id))
            return panel
        return _waiting_panel(lang, panel_id, mode, n=n, next_action=next_action)
