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
    price: str = "USD $29/mo",
    plan: str = "",
    lang: str = "en",
) -> FT:
    """Full-width banner shown when a cloud feature is not available.

    Args:
        feature: Short feature name, e.g. "Encrypted Backup"
        description: One-line description of what the user gets.
        price: Price string shown on the CTA button.
        plan: Plan key for the /subscribe CTA, e.g. "cloud" or "ai".
        lang: UI language code.
    """
    href = _subscribe_url(plan)
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
            f"{t('cloud.start_trial', lang)} - {price}",
            href=href,
            target="_blank",
            cls="btn btn--primary upgrade-banner__cta",
        ),
        cls="upgrade-banner",
    )


def cloud_gate(
    is_connected: bool,
    feature: str,
    description: str,
    price: str = "USD $29/mo",
    plan: str = "cloud",
    content: FT | None = None,
    lang: str = "en",
) -> FT:
    """Conditionally show upgrade_banner OR the actual feature content.

    Args:
        is_connected: True if the gateway session is active (subscription valid).
        feature: Feature name for the banner.
        description: Banner description.
        price: Price string.
        plan: Plan key for the /subscribe CTA.
        content: The real UI to show when connected. If None, returns only banner.
        lang: UI language code.
    """
    if not is_connected:
        return upgrade_banner(feature, description, price, plan, lang=lang)
    return content if content is not None else Div()
