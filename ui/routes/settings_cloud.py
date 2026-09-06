# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Web Access: Celerp Connect connection, TOS, Team infrastructure."""

from __future__ import annotations

import json

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ui.components.shell import base_shell, page_header, page_title
from ui.i18n import t, get_lang
from ui.config import get_role as _get_role

from ui.routes.settings import (
    _check_permission,
    _token,
    _cloud_relay_tab,
    PAID_TIERS,
)
from ui.routes.settings_general import _section_breadcrumb


def _has_team_features(state: dict) -> bool:
    """Whether Team-tier infrastructure controls should be shown.

    Active entitlement comes from the fetched commercial state's feature flags.
    During grace and after grace those flags are false, so also consult the
    on-disk packaged db-state: infrastructure stays reachable while grace is
    open, and after grace (an external database still configured on a lapsed
    install) so the user can read the fallback notice and restore a backup.
    Fail-closed on a neutral state.
    """
    from celerp.gateway.state import get_packaged_db_state
    flags = state.get("feature_flags") or {}
    if flags.get("external_db") or flags.get("external_storage"):
        return True
    db_state = get_packaged_db_state()
    return bool(
        db_state["in_grace"]
        or (db_state["has_external_url"] and not db_state["external_db_entitled"])
        or db_state["storage_in_grace"]
        or (db_state["has_external_storage"] and not db_state["external_storage_entitled"])
    )


async def _commercial_state(request: Request) -> dict:
    """Fetch the live commercial state from the API once per request, memoized on
    request.state so both the tab decision and any consumer share one call.

    Fails closed to a neutral empty state: a missing token or any fetch error
    yields {} so the page renders with the neutral (no-team) tab set rather than
    fabricating entitlement or 500-ing."""
    cached = getattr(request.state, "commercial_state", None)
    if cached is not None:
        return cached
    from ui.config import get_token
    import ui.api_client as _api
    token = get_token(request)
    if not token:
        request.state.commercial_state = {}
        return {}
    try:
        state = await _api.get_commercial_state(token)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    request.state.commercial_state = state
    return state


def _cloud_tabs(active: str, has_team_features: bool = False, lang: str = "en") -> FT:
    tabs = [("status", t("settings.team_url", lang))]
    if has_team_features:
        tabs.append(("infrastructure", t("cloud.tab_infrastructure", lang)))
    # One tab per connector category, labelled by what the sync does for the
    # customer (same keys as the old in-tab section headings).
    tabs.append(("website", t("connectors.group_website", lang)))
    tabs.append(("accounting", t("connectors.group_accounting", lang)))
    # Payments lives with Web Access: it only works cloud-connected, and a
    # not-yet-connected visitor lands amid the plans/features pitch instead of
    # a dead Connect button. Links to its own page (needs live status).
    tabs.append(("payments", t("nav.payments", lang)))
    return Div(
        *[
            A(label,
              href="/settings/payments" if key == "payments" else f"/settings/cloud?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _feature_card(icon: str, title: str, desc: str, lang: str = "en") -> FT:
    return Div(
        Div(icon, cls="cloud-feature-card__icon"),
        Div(title, cls="cloud-feature-card__title"),
        Div(desc, cls="cloud-feature-card__desc"),
        cls="cloud-feature-card",
    )


def _plan_card(name: str, price: str, desc: str, bullets: list[str], subscribe_url: str, featured: bool = False, lang: str = "en", cta_label: str | None = None) -> FT:
    card_cls = "cloud-plan-card cloud-plan-card--featured" if featured else "cloud-plan-card"
    return Div(
        Div(name, cls="cloud-plan-card__name"),
        Div(price, Span(t("settings_cloud.per_mo", lang)), cls="cloud-plan-card__price"),
        Div(desc, cls="cloud-plan-card__desc"),
        Ul(*[Li(b) for b in bullets]),
        A(cta_label or t("cloud.start_trial", lang), href=subscribe_url, target="_blank", cls="btn btn--primary btn--sm"),
        cls=card_cls,
    )


def _value_prop_page(iid: str, lang: str = "en", disconnected: bool = False,
                     show_partner_claim: bool = False) -> FT:
    """Full value-proposition landing page shown when not connected to cloud.

    `disconnected` marks a sticky Cloud disconnect: the credential is preserved,
    so the connect section withholds its auto-connect (landing here must not
    silently undo the disconnect) while the Connect button still reconnects in
    one click. `show_partner_claim` adds the owner/admin partner-claim card."""
    return Div(
        # Hero - explain the relay concept simply
        Div(
            H2(t("cloud.hero_title", lang)),
            P(t("cloud.hero_desc1", lang)),
            P(
                t("cloud.hero_desc2", lang),
                style="font-weight:600;margin-top:8px;",
            ),
            cls="cloud-hero",
        ),
        _plans_ad(iid, lang=lang),
        # Already subscribed / connect section
        _connect_section(iid, lang=lang, disconnected=disconnected),
        *([_partner_claim_card(lang=lang)] if show_partner_claim else []),
        cls="content-area",
    )


def _plans_ad(iid: str, lang: str = "en") -> FT:
    """Dispatch the plan area by commercial mode: a partner-managed install sees
    its partner's offer (no direct Celerp price), every other install sees the
    standard direct grid. Signature unchanged, so both call sites (value-prop
    page, status tab) are untouched."""
    from celerp.gateway.state import get_commercial_mode
    if get_commercial_mode() == "partner_managed":
        return _partner_offer(iid, lang=lang)
    return _direct_plans(iid, lang=lang)


def _partner_offer(iid: str, lang: str = "en") -> FT:
    """Partner-managed plan area: the partner's offer rendered from the relay-
    pushed commercial context, with no direct Celerp price, plus a contact line
    pointing at the implementation partner. A missing or malformed offer degrades
    to the contact line alone rather than a broken or fabricated price card."""
    from celerp.gateway.state import (
        build_commercial_handoff, get_offer, get_partner_identity,
    )
    from ui.components.table import fmt_money

    identity = get_partner_identity() or {}
    partner_name = identity.get("display_name") or ""
    partner_url = build_commercial_handoff(iid, "subscribe", "")
    offer = get_offer()

    children: list = []
    if partner_name:
        children.append(Div(partner_name, cls="cloud-partner-offer__partner"))

    amount = offer.get("retail_amount") if offer else None
    currency = offer.get("currency") if offer else None
    # Egress guard: render a priced card only when both amount and currency are
    # well-formed. A stale cache from a pre-validator binary could still hold a
    # non-string currency or a bool amount, so re-check here rather than trust
    # the stored offer, and degrade to the contact line if it fails.
    priced = (
        offer
        and isinstance(amount, int) and not isinstance(amount, bool)
        and isinstance(currency, str)
        and offer.get("display_name")
    )
    if priced:
        bullets = [b for b in (offer.get("service_bullets") or []) if isinstance(b, str)]
        children.append(_plan_card(
            offer["display_name"],
            fmt_money(amount / 100, currency),
            offer.get("service_description") or "",
            bullets,
            partner_url,
            cta_label=t("cloud.partner_support", lang),
            lang=lang,
        ))
    elif partner_url:
        # Degraded branch: no usable offer, but a valid partner destination
        # exists, so give the user a real contact CTA rather than a dead-end
        # text note (BLOCKER 6).
        children.append(A(
            t("cloud.partner_support", lang),
            href=partner_url, target="_blank",
            cls="btn btn--primary btn--sm cloud-partner-offer__contact",
        ))

    children.append(Div(t("cloud.partner_managed_note", lang), cls="cloud-partner-offer__note"))
    return Div(*children, cls="cloud-partner-offer")


def _direct_plans(iid: str, lang: str = "en") -> FT:
    """The direct paid-plan advertisement: feature cards, trial banner, plan
    cards. Shown on the not-connected landing page and, below the status tab, to
    connected free-tier accounts (the plans are what they are missing). Every
    plan CTA resolves through the central handoff policy."""
    from celerp.gateway.state import build_commercial_handoff

    return Div(
        # Feature cards - three platform features on top...
        Div(
            _feature_card(
                "🔗", t("cloud.feature_url_title", lang),
                t("cloud.feature_url_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "💾", t("cloud.feature_backup_title", lang),
                t("cloud.feature_backup_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "🤖", t("cloud.feature_ai_title", lang),
                t("cloud.feature_ai_desc", lang),
                lang=lang,
            ),
            cls="cloud-features",
        ),
        # ...and the sync + payments features below
        Div(
            _feature_card(
                "🛒", t("cloud.feature_website_title", lang),
                t("cloud.feature_website_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "📊", t("cloud.feature_accounting_title", lang),
                t("cloud.feature_accounting_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "💳", t("cloud.feature_payments_title", lang),
                t("cloud.feature_payments_desc", lang),
                lang=lang,
            ),
            cls="cloud-features",
        ),
        # Plans - the centred trial banner introduces them; no left-aligned heading needed
        Div(
            Div(t("cloud.trial_head", lang), cls="cloud-trial-banner__head"),
            Div(t("cloud.trial_sub", lang), cls="cloud-trial-banner__sub"),
            cls="cloud-trial-banner",
        ),
        Div(
            _plan_card(
                t("cloud.plan_cloud_name", lang), "USD $29",
                t("cloud.plan_cloud_desc", lang),
                [
                    t("cloud.plan_cloud_b1", lang),
                    t("cloud.plan_cloud_b2", lang),
                    t("cloud.plan_cloud_b3", lang),
                    t("cloud.plan_cloud_b4", lang),
                ],
                build_commercial_handoff(iid, "subscribe", "cloud"),
                lang=lang,
            ),
            _plan_card(
                t("cloud.plan_ai_name", lang), "USD $49",
                t("cloud.plan_ai_desc", lang),
                [
                    t("cloud.plan_ai_b1", lang),
                    t("cloud.plan_ai_b2", lang),
                    t("cloud.plan_ai_b3", lang),
                ],
                build_commercial_handoff(iid, "subscribe", "ai"),
                featured=True,
                lang=lang,
            ),
            cls="cloud-plans",
        ),
    )


def _connect_section(iid: str, lang: str = "en", disconnected: bool = False) -> FT:
    """The Celerp-account surface in its claim-led variant (this page's context
    is an existing/prospective subscriber). ONE component app-wide - see
    ui/routes/account.py. Keeps id="cloud-relay-tab" so the shipped
    cloud-activate/cloud-claim responses replace the same element."""
    from ui.routes.account import account_panel
    return account_panel(lang, intent="claim", panel_id="cloud-relay-tab",
                         suppress_autoconnect=disconnected)


def _parse_db_url(url: str) -> dict:
    """Parse a postgresql+asyncpg://user:pass@host:port/dbname URL into components."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        return {
            "host": parsed.hostname or "",
            "port": str(parsed.port or 5432),
            "name": parsed.path.lstrip("/") if parsed.path else "",
            "user": parsed.username or "",
            # password intentionally omitted (masked in UI)
        }
    except Exception:
        return {"host": "", "port": "5432", "name": "", "user": ""}


def _infra_db_section() -> FT:
    from celerp.config import settings, read_config
    current_url = settings.database_url
    db = _parse_db_url(current_url)

    masked_url = current_url
    if "@" in current_url:
        try:
            from urllib.parse import urlparse
            p = urlparse(current_url)
            masked_url = current_url.replace(f":{p.password}@", ":****@") if p.password else current_url
        except Exception:
            pass

    # Check if there's a previous URL to restore
    cfg = read_config()
    prev_url = cfg.get("database_backup", {}).get("previous_url", "")

    return Div(
        H3(t("page.database")),
        P(t("settings.current"),
            Code(masked_url, style="font-size:12px;"),
            cls="settings-hint",
        ),
        P(t("settings._changing_the_database_requires_a_restart_and_data"),
            cls="flash flash--warning",
            style="margin-bottom:12px;",
        ),
        Form(
            Div(
                Label(t("label.host"), For="db_host"),
                Input(id="db_host", name="db_host", placeholder="localhost",
                      value=db["host"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.port"), For="db_port"),
                Input(id="db_port", name="db_port", type="number", value=db["port"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.database_name"), For="db_name"),
                Input(id="db_name", name="db_name", placeholder="celerp",
                      value=db["name"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.username"), For="db_user"),
                Input(id="db_user", name="db_user", placeholder="celerp",
                      value=db["user"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.password"), For="db_pass"),
                Input(id="db_pass", name="db_pass", type="password", placeholder="••••••••", cls="input"),
                cls="form-row",
            ),
            Div(
                Button(t("btn.test_connection"),
                    type="button",
                    cls="btn btn--outline btn--sm",
                    hx_post="/settings/cloud/test-db",
                    hx_include="closest form",
                    hx_target="#db-test-result",
                    hx_swap="innerHTML",
                ),
                Button(t("btn.save_restart"), type="submit", cls="btn btn--primary btn--sm", style="margin-left:8px;",
                       hx_confirm=t("settings_cloud.restart_server_confirm")),
                style="display:flex;align-items:center;margin-top:4px;",
            ),
            Div(id="db-test-result", cls="infra-test-result"),
            hx_post="/settings/cloud/save-infra",
            hx_target="#db-test-result",
            cls="infra-form",
        ),
        # Restore previous button (GDR undo support)
        Div(
            Button(t("btn._restore_previous_db_settings"),
                cls="btn btn--outline btn--sm",
                hx_post="/settings/cloud/restore-db",
                hx_target="#restore-db-result",
                hx_swap="innerHTML",
                hx_confirm=t("settings_cloud.restore_db_confirm"),
            ),
            Div(id="restore-db-result", cls="infra-test-result"),
            style="margin-top:8px;",
        ) if prev_url else "",
        cls="infra-section",
    )


def _infra_storage_section() -> FT:
    from celerp.config import settings
    backend = settings.storage_backend or "local"

    return Div(
        H3(t("page.file_storage")),
        Form(
            Div(
                Label(t("label.backend"), For="storage_backend"),
                Select(
                    Option(t("settings.local_filesystem"), value="local", selected=backend == "local"),
                    Option("S3-Compatible", value="s3", selected=backend == "s3"),
                    id="storage_backend",
                    name="storage_backend",
                    cls="input",
                    onchange="document.getElementById('s3-fields').style.display=this.value==='s3'?'block':'none';",
                ),
                cls="form-row",
            ),
            Div(
                Div(
                    Label(t("label.endpoint_url"), For="s3_endpoint"),
                    Input(id="s3_endpoint", name="s3_endpoint",
                          placeholder="https://s3.amazonaws.com", value=settings.storage_s3_endpoint,
                          cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.bucket_name"), For="s3_bucket"),
                    Input(id="s3_bucket", name="s3_bucket", placeholder="my-celerp-bucket",
                          value=settings.storage_s3_bucket, cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.access_key"), For="s3_access_key"),
                    Input(id="s3_access_key", name="s3_access_key", placeholder="AKIAIOSFODNN7EXAMPLE",
                          value=settings.storage_s3_access_key, cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.secret_key"), For="s3_secret_key"),
                    Input(id="s3_secret_key", name="s3_secret_key", type="password",
                          placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", cls="input"),
                    cls="form-row",
                ),
                id="s3-fields",
                style=f"display:{'block' if backend == 's3' else 'none'};",
            ),
            Div(
                Button(t("btn.test_connection"),
                    type="button",
                    cls="btn btn--outline btn--sm",
                    hx_post="/settings/cloud/test-storage",
                    hx_include="closest form",
                    hx_target="#storage-test-result",
                    hx_swap="innerHTML",
                ),
                Button(t("btn.save_restart"), type="submit", cls="btn btn--primary btn--sm", style="margin-left:8px;",
                       hx_confirm=t("settings_cloud.restart_server_confirm")),
                style="display:flex;align-items:center;margin-top:4px;",
            ),
            Div(id="storage-test-result", cls="infra-test-result"),
            hx_post="/settings/cloud/save-infra",
            hx_target="#storage-test-result",
            cls="infra-form",
        ),
        cls="infra-section",
    )


def _format_deadline(value) -> str:
    """Format an ISO-8601 grace deadline as a plain date. An unparseable value
    falls back to its raw string rather than raising."""
    if not value:
        return ""
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(value)


def _append_renewal(children: list, partner: dict | None, lang: str) -> None:
    """Append the renewal affordance: a neutral renewal line always, plus a
    partner support line only when the install is partner-managed. Never
    fabricates a partner."""
    children.append(P(t("grace.renew", lang), cls="settings-hint"))
    if partner:
        name = partner.get("display_name") or ""
        children.append(P(t("grace.partner_support", lang, partner=name), cls="settings-hint"))


def _grace_notice(state: dict, partner: dict | None, lang: str = "en") -> FT | None:
    """Grace-period banner (during grace) or the after-grace persistent notice.

    During grace: the renewal deadline, that the external database stays
    customer-owned, and the renewal affordance. After grace: that the app has
    fallen back to the local database, that the external database is still
    available to reselect, and a warning that reselecting risks divergence.
    Returns None when neither state applies.
    """
    if state.get("in_grace") or state.get("storage_in_grace"):
        children = [
            P(t("grace.deadline", lang, deadline=_format_deadline(state.get("grace_period_ends")))),
            P(t("grace.external_owned", lang), cls="settings-hint"),
        ]
        _append_renewal(children, partner, lang)
        return Div(*children, cls="flash flash--warning", style="margin-bottom:12px;")
    if (state.get("has_external_url") and not state.get("external_db_entitled")) or (
        state.get("has_external_storage") and not state.get("external_storage_entitled")
    ):
        children = [
            P(t("grace.local_now", lang)),
            P(t("grace.external_available", lang), cls="settings-hint"),
            P(t("grace.divergence_warning", lang), cls="settings-hint"),
        ]
        _append_renewal(children, partner, lang)
        return Div(*children, cls="flash flash--warning", style="margin-bottom:12px;")
    return None


def _infrastructure_tab(grace_notice: FT | None = None) -> FT:
    """Team plan infrastructure config: external DB + S3 storage. The grace or
    after-grace notice, when present, sits above the config sections."""
    children: list = []
    if grace_notice is not None:
        children.append(grace_notice)
    children.extend([_infra_db_section(), _infra_storage_section()])
    return Div(*children, cls="settings-card")


def _backup_summary_card(gw_ok: bool = False, backup_data: dict | None = None) -> FT:
    """Compact backup status card for the cloud settings page."""

    if not gw_ok or backup_data is None:
        return Div(cls="settings-card")  # nothing to show when not connected

    def _last_run(entry: dict) -> str:
        last = entry.get("last_run")
        ok = entry.get("ok")
        if last is None:
            return t("settings.pending")
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(last).astimezone(timezone.utc)
            stamp = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            stamp = last
        if ok is False:
            err = entry.get("error") or ""
            return f"{stamp} - {t('settings.failed')}{': ' + err if err else ''}"
        return stamp

    def _time_until(iso: str | None) -> str:
        if iso is None:
            return t("settings.not_scheduled")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        return t("settings_cloud.in_hours_mins", hours=hours, mins=mins) if hours > 0 else t("settings_cloud.in_mins", mins=mins)

    cloud_section = Div(
        Div(
            H4(t("page.backup"), style="margin:0;"),
            A(t("settings.view_full_backup_settings"), href="/settings/general?tab=backup",
              cls="settings-hint", style="font-size:0.82rem;"),
            style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;",
        ),
        Table(
            Tr(Td(t("settings.last_db_backup"), cls="detail-label"), Td(_last_run(backup_data["db"]))),
            Tr(Td(t("settings.next_db_backup"), cls="detail-label"), Td(_time_until(backup_data["next_db_utc"]))),
            cls="detail-table",
        ),
    )

    return Div(
        cloud_section,
        cls="settings-card",
    )


async def _relay_state(token) -> tuple[str, str, str, bool, bool]:
    """Fetch relay state from the API process (the gateway client lives there).

    Returns (relay_status, public_url, tier, disconnected, token_bound); on an
    unreachable API, degrades to the local client's status alone.
    """
    from celerp.gateway.client import get_client as _local_get_client
    import ui.api_client as _api
    from ui.api_client import APIError as _APIError
    relay_status = "inactive"
    public_url = ""
    tier = ""
    disconnected = False
    token_bound = False
    try:
        rs = await _api.get_relay_status(token)
        relay_status = rs.get("relay_status", "inactive")
        public_url = rs.get("public_url", "")
        tier = rs.get("tier") or ""
        disconnected = bool(rs.get("cloud_disconnected"))
        token_bound = bool(rs.get("gateway_token_set"))
    except (_APIError, Exception):
        lc = _local_get_client()
        relay_status = lc.relay_status if lc else "inactive"
    return relay_status, public_url, tier, disconnected, token_bound


def _partner_claim_card(lang: str = "en", error: str | None = None) -> FT:
    """Neutral partner-claim card: a claim-code field and a Review button.

    Distinct from the subscription email-claim flow. Resolving previews the
    partner behind a code without binding anything; nothing is committed until
    the owner accepts on the preview."""
    children = [
        H3(t("settings_cloud.partner_claim_title", lang), cls="settings-section-title"),
        P(t("settings_cloud.partner_claim_desc", lang), cls="settings-hint"),
    ]
    if error:
        children.append(P(error, cls="text-error", style="margin:8px 0;"))
    children.append(
        Form(
            Input(name="claim_token", type="text", autocomplete="off",
                  placeholder=t("settings_cloud.partner_claim_token_placeholder", lang),
                  cls="input", style="max-width:360px;"),
            Button(t("settings_cloud.partner_claim_review", lang),
                   type="submit", cls="btn btn--primary", style="margin-left:8px;"),
            Span(cls="htmx-indicator", id="partner-claim-spinner"),
            hx_post="/settings/partner-claim/resolve",
            hx_target="#partner-claim-card",
            hx_swap="outerHTML",
            hx_indicator="#partner-claim-spinner",
            style="display:flex;align-items:center;gap:8px;margin-top:8px;",
        )
    )
    return Div(*children, id="partner-claim-card", cls="settings-card")


def _partner_managed_note(lang: str = "en") -> FT:
    """Neutral note shown in place of the claim-entry control on a partner_managed
    install: the claim control is intentionally withheld, and its absence is stated
    with the generic managed-by note rather than left silent. No partner name is
    interpolated (no fabricated identity)."""
    return Div(
        P(t("cloud.partner_managed_note", lang), cls="settings-hint"),
        id="partner-managed-note", cls="settings-card",
    )


def _partner_claim_preview(identity: dict, claim_token: str, lang: str = "en") -> FT:
    """Preview of the partner behind a resolved claim, with Accept and Decline.

    Accept is the one deliberate commit; the disable-on-submit posture stops a
    double-click from double-submitting. Decline binds nothing and restores the
    neutral card."""
    support_email = identity.get("support_email") or ""
    support_url = identity.get("support_url") or ""
    support_children: list = []
    if support_email:
        support_children.append(Div(support_email, cls="settings-value", style="margin:4px 0;"))
    if support_url:
        support_children.append(A(
            t("cloud.partner_support", lang),
            href=support_url, target="_blank",
            cls="btn btn--outline btn--sm", style="margin-top:4px;"))
    return Div(
        H3(t("settings_cloud.partner_claim_title", lang), cls="settings-section-title"),
        P(t("settings_cloud.partner_claim_managed_by", lang), cls="settings-hint"),
        Div(identity.get("display_name") or "--", cls="settings-value",
            style="font-weight:600;margin:6px 0;"),
        *([Div(*support_children, style="margin:8px 0;")] if support_children else []),
        Div(
            Button(t("btn.accept", lang),
                   cls="btn btn--primary",
                   hx_post="/settings/partner-claim/accept",
                   hx_vals=json.dumps({"claim_token": claim_token}),
                   hx_target="#partner-claim-card",
                   hx_swap="outerHTML",
                   hx_indicator="#partner-claim-spinner",
                   **{"hx-disabled-elt": "this"}),
            Button(t("btn.decline", lang),
                   cls="btn btn--outline", style="margin-left:8px;",
                   hx_post="/settings/partner-claim/decline",
                   hx_target="#partner-claim-card",
                   hx_swap="outerHTML"),
            Span(cls="htmx-indicator", id="partner-claim-spinner"),
            style="margin-top:12px;",
        ),
        id="partner-claim-card", cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/cloud-relay-tab")
    async def cloud_relay_tab_fragment(request: Request):
        """HTMX fragment: re-render the relay tab with fresh state.

        Polled by the connecting card so the connection outcome (account view
        or the failure card) appears without a manual page reload.
        """
        token = _token(request)
        if not token:
            return Response(status_code=401)
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        relay_status, public_url, tier, _, token_bound = await _relay_state(token)
        return _cloud_relay_tab(relay_status=relay_status, public_url=public_url,
                                tier=tier, token_bound=token_bound)

    @app.get("/settings/cloud")
    async def settings_cloud_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_integrations")):
            return r

        import ui.api_client as _api
        lang = get_lang(request)
        is_owner_admin = _get_role(request) in ("owner", "admin")
        relay_status, public_url, tier, disconnected, token_bound = await _relay_state(token)
        # A free tier is signed in (holds a gateway_token) but never starts the WS
        # client - it has no tunnel to serve - so relay_status stays "inactive".
        # Treat a token-bound instance as connected so a signed-in free account
        # gets the account/disconnect view plus the upgrade ad, not the landing page.
        gw_ok = relay_status in ("active", "tos_required", "connecting", "error") or token_bound

        # If not connected, show value-prop landing. A sticky-disconnected install
        # keeps its preserved credential, so the connect section withholds its
        # auto-connect (a page visit must not silently undo the disconnect) while
        # the Connect button still reconnects in one click.
        # The claim-entry control is offered only to an owner/admin on an install
        # that is not already partner_managed - a managed install already shows the
        # partner offer and managed note (via _partner_offer), so the same gate at
        # both render sites (value-prop landing and status tab) mirrors _plans_ad.
        from celerp.gateway.state import get_commercial_mode
        can_claim = is_owner_admin and get_commercial_mode() != "partner_managed"

        if not gw_ok:
            from celerp.config import ensure_instance_id
            iid = ensure_instance_id()
            return await base_shell(
                _section_breadcrumb(t("settings_cloud.web_access", lang)),
                page_header(t("settings_cloud.web_access", lang)),
                _value_prop_page(iid, lang=lang, disconnected=disconnected,
                                 show_partner_claim=can_claim),
                title=page_title("settings_cloud.web_access"),
                nav_active="web-access",
                lang=lang,
                request=request,
            )

        # Connected or connecting - show tabs
        tab = request.query_params.get("tab", "status")
        has_team = _has_team_features(await _commercial_state(request))
        from celerp.gateway.state import get_packaged_db_state, get_partner_identity
        grace_notice = _grace_notice(get_packaged_db_state(), get_partner_identity(), lang=lang)

        if tab == "infrastructure" and has_team:
            content = _infrastructure_tab(grace_notice=grace_notice)
        elif tab in ("website", "accounting"):
            from ui.routes.settings_connectors import connectors_tab_content
            content = await connectors_tab_content(lang, token=token, category=tab)
        else:
            backup_data: dict | None = None
            try:
                backup_data = await _api.get_backup_status(token)
            except Exception:
                pass
            # Backups are a paid-tier feature (like connectors); a free instance has
            # no public_url and no backup entitlement, so the summary card is omitted
            # entirely rather than showing scheduler/pending state for a plan that
            # never runs backups.
            parts = []
            if grace_notice is not None:
                parts.append(grace_notice)
            parts.extend([_cloud_relay_tab(relay_status=relay_status, public_url=public_url, tier=tier, token_bound=token_bound),
                          _backup_summary_card(gw_ok=gw_ok and bool(public_url), backup_data=backup_data)])
            # A connected free-tier account keeps its free tabs but still sees
            # the paid-plan advertisement the not-connected page carries - the
            # plans are exactly what the free tier is missing. An unknown tier
            # (status round trip failed/pending) degrades to showing the ad,
            # never to silently hiding it - only a confirmed paid tier suppresses it.
            if tier not in PAID_TIERS:
                from celerp.config import ensure_instance_id
                parts.append(_plans_ad(ensure_instance_id(), lang=lang))
            if is_owner_admin:
                parts.append(_partner_claim_card(lang=lang) if can_claim
                             else _partner_managed_note(lang=lang))
            content = Div(*parts)
            tab = "status"

        return await base_shell(
            _section_breadcrumb(t("settings_cloud.web_access", lang)),
            page_header(t("settings_cloud.web_access", lang)),
            _cloud_tabs(tab, has_team_features=has_team, lang=lang),
            content,
            title=page_title("settings_cloud.web_access"),
            nav_active="web-access",
            lang=lang,
            request=request,
        )

    @app.post("/settings/partner-claim/resolve")
    async def partner_claim_resolve_ui(request: Request):
        """HTMX: proxy to the API to preview the partner behind a claim code.
        Owner/admin only; binds nothing."""
        lang = get_lang(request)
        if _get_role(request) not in ("owner", "admin"):
            return _partner_claim_card(lang=lang)
        import ui.api_client as _api
        token = _token(request)
        form = await request.form()
        claim_token = (form.get("claim_token") or "").strip()
        try:
            data = await _api.resolve_partner_claim(token, claim_token)
        except Exception:
            return _partner_claim_card(lang=lang, error=t("settings_cloud.partner_claim_error", lang))
        if err := data.get("error"):
            return _partner_claim_card(lang=lang, error=err)
        return _partner_claim_preview(data, claim_token, lang=lang)

    @app.post("/settings/partner-claim/accept")
    async def partner_claim_accept_ui(request: Request):
        """HTMX: proxy to the API to accept a partner claim. Owner/admin only. On
        success the relay pushes the new commercial context, so the page reloads
        to reflect it."""
        lang = get_lang(request)
        if _get_role(request) not in ("owner", "admin"):
            return _partner_claim_card(lang=lang)
        import ui.api_client as _api
        token = _token(request)
        form = await request.form()
        claim_token = (form.get("claim_token") or "").strip()
        try:
            data = await _api.accept_partner_claim(token, claim_token)
        except Exception:
            return _partner_claim_card(lang=lang, error=t("settings_cloud.partner_claim_error", lang))
        if err := data.get("error"):
            return _partner_claim_card(lang=lang, error=err)
        return Response(status_code=204, headers={"HX-Redirect": "/settings/cloud"})

    @app.post("/settings/partner-claim/decline")
    async def partner_claim_decline_ui(request: Request):
        """HTMX: decline a partner claim. A pure client-side dismissal - no relay
        or API call - that restores the neutral claim card, binding nothing."""
        return _partner_claim_card(lang=get_lang(request))

    @app.post("/settings/cloud/test-db")
    async def cloud_test_db(request: Request):
        """HTMX: test database connectivity with provided credentials."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        host = form.get("db_host", "").strip()
        port = int(form.get("db_port", "5432") or "5432")
        name = form.get("db_name", "").strip()
        user = form.get("db_user", "").strip()
        password = form.get("db_pass", "")

        if not all([host, name, user]):
            return Span(t("settings.please_fill_in_host_database_name_and_username"),
                        cls="infra-test-result--err")

        import asyncio
        try:
            conn = await asyncio.wait_for(
                _try_db_connect(host, port, name, user, password),
                timeout=3.0,
            )
            return Span(t("settings_cloud.connected_to", target=f"{name}@{host}:{port}"), cls="infra-test-result--ok")
        except asyncio.TimeoutError:
            return Span(t("settings.connection_timed_out_3s"), cls="infra-test-result--err")
        except Exception as exc:
            return Span(f"✗ {type(exc).__name__}: {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/test-storage")
    async def cloud_test_storage(request: Request):
        """HTMX: test S3-compatible storage connectivity."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        backend = form.get("storage_backend", "local")
        if backend == "local":
            return Span(t("settings._local_filesystem_no_connection_needed"), cls="infra-test-result--ok")

        endpoint = form.get("s3_endpoint", "").strip()
        bucket = form.get("s3_bucket", "").strip()
        access_key = form.get("s3_access_key", "").strip()
        secret_key = form.get("s3_secret_key", "")

        if not all([endpoint, bucket, access_key, secret_key]):
            return Span(t("settings.please_fill_in_all_s3_fields"), cls="infra-test-result--err")

        import asyncio
        try:
            msg = await asyncio.wait_for(
                _try_s3_connect(endpoint, bucket, access_key, secret_key),
                timeout=3.0,
            )
            return Span(msg, cls="infra-test-result--ok")
        except asyncio.TimeoutError:
            return Span(t("settings.connection_timed_out_3s"), cls="infra-test-result--err")
        except Exception as exc:
            return Span(f"✗ {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/save-infra")
    async def cloud_save_infra(request: Request):
        """Save infrastructure config (DB + storage).

        In the packaged Team build (CELERP_DATA_DIR set) writes go to the
        Electron-owned celerp-config.json, the only store the packaged launcher
        reads, and apply is a full Electron relaunch (no config.toml, no pkill).
        In the self-hosted build writes go to config.toml and apply is a server
        reload via SIGHUP.
        """
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        import os
        if os.environ.get("CELERP_DATA_DIR"):
            return _save_infra_packaged(form)
        return _save_infra_selfhosted(form)

    @app.post("/settings/cloud/restore-db")
    async def cloud_restore_db(request: Request):
        """Restore the previous database URL (GDR undo support).

        Packaged build swaps external_db_url with its backup in celerp-config.json
        and relaunches Electron; self-hosted swaps config.toml and reloads.
        """
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        import os
        if os.environ.get("CELERP_DATA_DIR"):
            return _restore_db_packaged()
        return _restore_db_selfhosted()


def _read_packaged_config() -> dict:
    """Read the Electron-owned celerp-config.json, degrading to {} on any error.
    Reads are only for computing backups and prior state; the atomic writer in
    the gateway client owns every write so the 0600 mode is never widened."""
    import os
    import json
    data_dir = os.environ.get("CELERP_DATA_DIR", "")
    if not data_dir:
        return {}
    config_path = os.path.join(data_dir, "celerp-config.json")
    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        return {}
    return {}


def _packaged_apply_fragment(message: str) -> FT:
    """Success fragment for a packaged save/restore. Carries a neutral factual
    message (shown as-is to a remote admin on a plain browser) plus a
    bridge-guarded script that triggers a full Electron relaunch when the
    window.celerp bridge is present, mirroring the existing openExternal/
    installUpdate presence guards. No OS process control is attempted on either
    path (pkill is dropped for the packaged build)."""
    return Div(
        Span(message, cls="infra-test-result--ok"),
        Script("if(window.celerp&&window.celerp.restartApp){window.celerp.restartApp();}"),
    )


def _save_infra_packaged(form) -> FT:
    """Persist DB/storage config to celerp-config.json via the atomic merge
    writer. Any failed write reports the error and leaves prior config intact;
    the packaged apply is a full Electron relaunch, never pkill."""
    from celerp.gateway import client as gwclient
    current = _read_packaged_config()

    updates: list[tuple[str, object]] = []

    host = form.get("db_host", "").strip()
    name = form.get("db_name", "").strip()
    user = form.get("db_user", "").strip()
    if host and name and user:
        port = form.get("db_port", "5432").strip() or "5432"
        password = form.get("db_pass", "")
        new_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"
        prev_url = current.get("external_db_url", "") or ""
        if new_url != prev_url:
            if prev_url:
                updates.append(("external_db_url_backup", prev_url))
            updates.append(("db_mode", "external"))
            updates.append(("external_db_url", new_url))

    storage_backend = form.get("storage_backend", "")
    if storage_backend:
        updates.append(("storage_mode", storage_backend))
        updates.append(("storage_s3_endpoint", form.get("s3_endpoint", "")))
        updates.append(("storage_s3_bucket", form.get("s3_bucket", "")))
        updates.append(("storage_s3_access_key", form.get("s3_access_key", "")))
        if form.get("s3_secret_key"):
            updates.append(("storage_s3_secret_key", form.get("s3_secret_key")))

    for key, value in updates:
        if not gwclient._merge_config_key(key, value):
            return Span(t("settings_cloud.save_failed", err=t("settings_cloud.config_write_failed")),
                        cls="infra-test-result--err")
    return _packaged_apply_fragment(t("settings_cloud.saved_restart_to_apply"))


def _save_infra_selfhosted(form) -> FT:
    """Persist DB/storage config to config.toml and reload the server via SIGHUP
    (self-hosted POSIX build)."""
    try:
        from celerp.config import read_config, write_config, settings
        cfg = read_config()
        if not cfg:
            return Span(t("settings.no_config_file_found"), cls="infra-test-result--err")

        db_url_changed = False

        # DB settings: compose URL when host+name+user are all present
        host = form.get("db_host", "").strip()
        name = form.get("db_name", "").strip()
        user = form.get("db_user", "").strip()
        if host and name and user:
            port = form.get("db_port", "5432").strip() or "5432"
            password = form.get("db_pass", "")
            new_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"
            previous_url = cfg.get("database", {}).get("url", settings.database_url)
            if new_url != previous_url:
                # Backup previous URL for undo support
                cfg.setdefault("database_backup", {})["previous_url"] = previous_url
                cfg.setdefault("database", {})["url"] = new_url
                db_url_changed = True

        # Storage settings
        storage_backend = form.get("storage_backend", "")
        if storage_backend:
            prev_storage = cfg.get("storage", {})
            cfg.setdefault("storage_backup", {}).update({
                "backend": prev_storage.get("backend", ""),
                "s3_endpoint": prev_storage.get("s3_endpoint", ""),
                "s3_bucket": prev_storage.get("s3_bucket", ""),
                "s3_access_key": prev_storage.get("s3_access_key", ""),
                "s3_secret_key": prev_storage.get("s3_secret_key", ""),
            })
            cfg.setdefault("storage", {})["backend"] = storage_backend
            cfg["storage"]["s3_endpoint"] = form.get("s3_endpoint", "")
            cfg["storage"]["s3_bucket"] = form.get("s3_bucket", "")
            cfg["storage"]["s3_access_key"] = form.get("s3_access_key", "")
            if form.get("s3_secret_key"):
                cfg["storage"]["s3_secret_key"] = form.get("s3_secret_key")

        write_config(cfg)

        if db_url_changed:
            import subprocess
            subprocess.Popen(["pkill", "-HUP", "-f", "uvicorn"])

        return Span(t("settings._saved"), cls="infra-test-result--ok")
    except Exception as exc:
        return Span(t("settings_cloud.save_failed", err=exc), cls="infra-test-result--err")


def _restore_db_packaged() -> FT:
    """Swap external_db_url with its backup in celerp-config.json. Apply is a
    full Electron relaunch, never pkill."""
    from celerp.gateway import client as gwclient
    current = _read_packaged_config()
    prev_url = current.get("external_db_url_backup", "") or ""
    if not prev_url:
        return Span(t("settings.no_previous_database_url_to_restore"), cls="infra-test-result--err")

    current_url = current.get("external_db_url", "") or ""
    if not gwclient._merge_config_key("external_db_url_backup", current_url):
        return Span(t("settings_cloud.restore_failed", err=t("settings_cloud.config_write_failed")),
                    cls="infra-test-result--err")
    if not gwclient._merge_config_key("external_db_url", prev_url):
        return Span(t("settings_cloud.restore_failed", err=t("settings_cloud.config_write_failed")),
                    cls="infra-test-result--err")
    return _packaged_apply_fragment(t("settings_cloud.restored_restart_to_apply"))


def _restore_db_selfhosted() -> FT:
    """Swap config.toml's database URL with its backup and reload via SIGHUP."""
    try:
        from celerp.config import read_config, write_config
        cfg = read_config()
        if not cfg:
            return Span(t("settings.no_config_file_found"), cls="infra-test-result--err")

        prev_url = cfg.get("database_backup", {}).get("previous_url", "")
        if not prev_url:
            return Span(t("settings.no_previous_database_url_to_restore"), cls="infra-test-result--err")

        current_url = cfg.get("database", {}).get("url", "")
        cfg.setdefault("database_backup", {})["previous_url"] = current_url
        cfg.setdefault("database", {})["url"] = prev_url
        write_config(cfg)

        import subprocess
        subprocess.Popen(["pkill", "-HUP", "-f", "uvicorn"])

        return Span(t("settings._restored_previous_db_url_restarting"), cls="infra-test-result--ok")
    except Exception as exc:
        return Span(t("settings_cloud.restore_failed", err=exc), cls="infra-test-result--err")


async def _try_db_connect(host: str, port: int, name: str, user: str, password: str) -> None:
    """Attempt an asyncpg connection to verify credentials."""
    import asyncpg  # type: ignore[import]
    conn = await asyncpg.connect(
        host=host, port=port, database=name, user=user, password=password
    )
    await conn.close()


async def _try_s3_connect(endpoint: str, bucket: str, access_key: str, secret_key: str) -> str:
    """Test S3-compatible storage connectivity with a real SigV4 head_bucket via
    the shared client helper, mapping outcomes to honest translated messages.

    Never leaks credentials or a raw botocore repr: every unmapped error degrades
    to a generic translated "connection failed", so the endpoint/access-key text
    the caller passed can never surface in the result span.
    """
    from celerp.services import attachments

    # botocore's exception classes drive the classification. In a build without
    # botocore they are unreachable, so fall back to a sentinel that never
    # matches - the connect attempt itself raises ImportError first and degrades.
    try:
        from botocore.exceptions import (
            ClientError,
            EndpointConnectionError,
            ConnectTimeoutError,
            ReadTimeoutError,
        )
    except ImportError:
        class _NoMatch(Exception):
            pass
        ClientError = EndpointConnectionError = ConnectTimeoutError = ReadTimeoutError = _NoMatch

    try:
        async with attachments._s3_client(endpoint, access_key, secret_key) as client:
            await client.head_bucket(Bucket=bucket)
    except ImportError:
        raise RuntimeError(t("settings_cloud.s3_support_unavailable"))
    except ClientError as exc:
        response = getattr(exc, "response", None)
        code = ""
        status = None
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code", "") or ""
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in ("AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch") or status == 403:
            raise RuntimeError(t("settings_cloud.invalid_credentials_403"))
        if code in ("NoSuchBucket", "404") or status == 404:
            raise RuntimeError(t("settings_cloud.bucket_not_found_404"))
        raise RuntimeError(t("settings_cloud.s3_connection_failed"))
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError):
        raise RuntimeError(t("settings_cloud.cannot_reach_endpoint"))
    except RuntimeError:
        raise
    except Exception:
        # Never echo the raw error: it can carry the endpoint or access key.
        raise RuntimeError(t("settings_cloud.s3_connection_failed"))

    return t("settings_cloud.connected_to_bucket", bucket=bucket)
