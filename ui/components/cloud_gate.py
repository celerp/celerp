# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared upgrade / cloud-gate UI components.

Used by any settings tab that requires a Connect subscription.
Keeps all subscribe CTA copy and styling in one place (DRY).
"""

from __future__ import annotations

from fasthtml.common import *
from ui.i18n import t, get_lang


def _subscribe_url(plan: str = "") -> str:
    """Build subscribe URL with instance_id passthrough if available.

    ``plan`` is passed as a query param (not a fragment) so the website can
    attribute which in-app CTA drove the click server-side; its JS also uses
    it to scroll to the matching plan card.
    """
    from celerp.config import ensure_instance_id
    from celerp.gateway.state import build_subscribe_url
    return build_subscribe_url(ensure_instance_id(), extra=f"plan={plan}" if plan else "")


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
    href = _subscribe_url(plan)
    price_text = price if price is not None else t("msg.29mo", lang)
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
            f"{t('cloud.start_trial', lang)} - {price_text}",
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
