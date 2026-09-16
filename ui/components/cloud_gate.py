# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared upgrade / cloud-gate UI components.

Used by any settings tab that requires a Connect subscription.
Keeps all subscribe CTA copy and styling in one place (DRY).
"""

from __future__ import annotations

from fasthtml.common import *
from ui.i18n import t, get_lang


def _commercial_route(intent: str, sku: str = "") -> str:
    """The in-app mint route that resolves this CTA's destination at click time.

    Every subscribe/top-up CTA points here rather than straight at celerp.com: the
    route runs the same commercial policy (``build_commercial_handoff``), and for a
    direct celerp_direct checkout it mints a single-use handoff token on the relay
    and 302-bounces the browser to the checkout URL with that token appended. The
    token clock therefore starts at click, and no relay round-trip happens on
    render. ``intent`` and ``sku`` ride as query params so the route can rebuild
    the destination for the authenticated instance server-side.
    """
    from urllib.parse import urlencode
    params = {"intent": intent}
    if sku:
        params["sku"] = sku
    return f"/commercial/checkout?{urlencode(params)}"


def subscribe_url(plan: str = "") -> str:
    """Build the in-app subscribe CTA URL.

    Points at the commercial mint route (not celerp.com directly): the route
    resolves the destination for the authenticated instance and mints a handoff
    token at click. ``plan`` rides as the ``sku`` query param so the route can
    resolve the correct plan destination.
    """
    return _commercial_route("subscribe", plan or "")


def topup_url() -> str:
    """Build the credit top-up CTA URL.

    Mirrors ``subscribe_url`` for the top-up intent: it points at the commercial
    mint route, which on a celerp_direct install mints a handoff token and bounces
    to the direct /subscribe/topup checkout, and on a partner-managed install
    routes to the partner support or Enterprise destination with no token minted.
    """
    return _commercial_route("topup", "ai")


def commercial_cta(
    intent: str,
    sku: str,
    direct_label: str,
    lang: str,
) -> tuple[str, str]:
    """Resolve an authenticated commercial CTA to its (href, label) pair, keeping
    the visible label in lockstep with the destination so a partner-managed
    surface never reads as a direct Celerp price CTA.

    - celerp_direct: the in-app /commercial/checkout mint route (via
      subscribe_url/topup_url, which already point there). A subscribe CTA keeps
      the caller's ``direct_label``; a top-up uses the standard top-up label.
    - partner_managed: the partner support URL, then a mailto: to the support
      email, then the Enterprise route, labelled "Contact partner support" while
      a real partner destination exists and "Contact Celerp" on the Enterprise
      fallback.
    - any unknown mode: the Enterprise route labelled "Contact Celerp" (fails
      closed, never a direct checkout).

    The single semantic resolver every surface uses where the label must match
    the destination. ``subscribe_url``/``topup_url`` stay thin direct-route
    helpers for callers whose label is already correct.
    """
    from celerp.gateway.state import (
        enterprise_url,
        get_commercial_mode,
        get_partner_identity,
        safe_support_email,
        safe_support_url,
    )

    mode = get_commercial_mode()
    if mode == "celerp_direct":
        if intent == "topup":
            return topup_url(), t("ai.top_up_credits", lang)
        return subscribe_url(sku), direct_label
    if mode == "partner_managed":
        identity = get_partner_identity() or {}
        support_url = safe_support_url(identity.get("support_url"))
        if support_url:
            return support_url, t("cloud.partner_support", lang)
        support_email = safe_support_email(identity.get("support_email"))
        if support_email:
            return f"mailto:{support_email}", t("cloud.partner_support", lang)
        return enterprise_url(), t("cloud.contact_celerp", lang)
    # Unknown mode: fail closed to Enterprise, never a direct checkout.
    return enterprise_url(), t("cloud.contact_celerp", lang)


def is_partner_managed() -> bool:
    """Whether this install is partner-managed.

    Single predicate every presentation surface uses to decide whether to
    suppress direct Celerp pricing: a partner-managed install must never show a
    direct price, because the partner sets and bills its own price.
    """
    from celerp.gateway.state import get_commercial_mode
    return get_commercial_mode() == "partner_managed"


def direct_price(text: str) -> str:
    """Return direct-pricing copy on a celerp_direct install, or the empty string
    when partner-managed.

    Presentation-side suppressor for any string that names a direct Celerp price
    ("$29", "USD $49/mo", the see-all-plans price). Callers render the returned
    value directly; an empty string renders as nothing, so a partner-managed
    surface simply omits the price rather than showing a wrong one.
    """
    return "" if is_partner_managed() else text


def upgrade_banner(
    feature: str,
    description: str,
    price: str | None = None,
    plan: str = "",
    lang: str = "en",
) -> FT:
    """Full-width banner shown when a cloud feature is not available.

    Args:
        feature: Short feature name, e.g. "Encrypted Backup"
        description: One-line description of what the user gets.
        price: Price string shown on the CTA button. Defaults to the
            standard Connect price, resolved at render time so it
            translates with the request language.
        plan: Plan key for the /subscribe CTA, e.g. "cloud" or "ai".
        lang: UI language code.
    """
    # Compose the direct-install label (trial + price); commercial_cta keeps the
    # visible label in lockstep with the destination, so on a partner_managed or
    # unknown-mode install it returns a partner-support / Contact-Celerp label and
    # href instead of this direct label, and no surface funnelling through here can
    # show a direct-price CTA that opens partner support. direct_price still
    # suppresses the figure on the direct label for the same-mode belt-and-braces.
    price_text = direct_price(price if price is not None else t("msg.29mo", lang))
    direct_label = f"{t('cloud.start_trial', lang)} - {price_text}" if price_text \
        else t("cloud.start_trial", lang)
    href, cta_label = commercial_cta("subscribe", plan, direct_label, lang)
    return Div(
        Div(
            Span(t("msg.u0001f512", lang), cls="upgrade-banner__icon"),
            Div(
                Strong(f"{feature} {t('cloud.requires_celerp_cloud', lang)}", cls="upgrade-banner__title"),
                P(description, cls="upgrade-banner__desc"),
                cls="upgrade-banner__text",
            ),
            cls="upgrade-banner__left",
        ),
        A(
            cta_label,
            href=href,
            target="_blank",
            cls="btn btn--primary upgrade-banner__cta",
        ),
        cls="upgrade-banner",
    )


def digest_upsell_modal(lang: str = "en") -> FT:
    """Upsell nudge shown after a non-paid user turns the low-stock digest on.

    The digest already sends through the local SMTP fallback, so this is a
    nudge and not a gate: the setting is already saved when the modal opens.
    It promotes hands-off Connect delivery and can be dismissed to stay on the
    current plan (explicit Cancel plus native Esc). Reuses the shared subscribe
    CTA from ``upgrade_banner`` and the account gate's modal shell and dismiss
    pattern so the CTA copy and modal styling live in one place (DRY).
    """
    dismiss = ("var d=document.getElementById('digest-upsell-modal');"
               "if(d){d.close();d.remove();}")
    return Div(
        Dialog(
            upgrade_banner(
                t("cloud.digest_upsell_feature", lang),
                t("cloud.digest_upsell_desc", lang),
                plan="cloud",
                lang=lang,
            ),
            Div(
                Button(t("btn.continue_on_own_plan", lang), type="button", onclick=dismiss,
                       cls="btn btn--sm btn--secondary"),
                cls="account-panel__cancel",
            ),
            id="digest-upsell-modal",
            cls="modal-dialog account-gate-modal",
        ),
        Script("(function(){"
               "var d=document.getElementById('digest-upsell-modal');"
               "d.addEventListener('cancel',function(){d.remove();});"
               "d.showModal();})();"),
        id="digest-upsell-host",
    )


def cloud_gate(
    is_connected: bool,
    feature: str,
    description: str,
    price: str | None = None,
    plan: str = "cloud",
    content: FT | None = None,
    lang: str = "en",
) -> FT:
    """Conditionally show upgrade_banner OR the actual feature content.

    Args:
        is_connected: True if the gateway session is active (subscription valid).
        feature: Feature name for the banner.
        description: Banner description.
        price: Price string. Defaults to the standard Connect price,
            resolved at render time by ``upgrade_banner``.
        plan: Plan key for the /subscribe CTA.
        content: The real UI to show when connected. If None, returns only banner.
        lang: UI language code.
    """
    if not is_connected:
        return upgrade_banner(feature, description, price, plan, lang=lang)
    return content if content is not None else Div()
