# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared upgrade / cloud-gate UI components.

Used by any settings tab that requires a Connect subscription.
Keeps all subscribe CTA copy and styling in one place (DRY).
"""

from __future__ import annotations

from fasthtml.common import *
from ui.i18n import t, get_lang


def subscribe_url(plan: str = "") -> str:
    """Build subscribe URL with instance_id passthrough if available.

    ``plan`` is passed as a query param (not a fragment) so the website can
    attribute which in-app CTA drove the click server-side; its JS also uses
    it to scroll to the matching plan card.
    """
    from celerp.config import ensure_instance_id
    from celerp.gateway.state import build_commercial_handoff
    return build_commercial_handoff(ensure_instance_id(), "subscribe", plan or "")


def topup_url() -> str:
    """Build the credit top-up URL through the commercial policy.

    Mirrors ``subscribe_url`` for the top-up intent: on a celerp_direct install
    it yields the direct /subscribe/topup URL; on a partner-managed install it
    routes to the partner support or Enterprise route, never a direct top-up
    checkout.
    """
    from celerp.config import ensure_instance_id
    from celerp.gateway.state import build_commercial_handoff
    return build_commercial_handoff(ensure_instance_id(), "topup", "ai")


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
    href = subscribe_url(plan)
    # Suppress the direct price under partner_managed even when a caller passes an
    # explicit price string: the partner sets its own price, so the CTA reads as a
    # plain action with no direct figure.
    price_text = direct_price(price if price is not None else t("msg.29mo", lang))
    cta_label = f"{t('cloud.start_trial', lang)} - {price_text}" if price_text \
        else t("cloud.start_trial", lang)
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
