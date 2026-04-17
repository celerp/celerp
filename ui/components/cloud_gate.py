# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared upgrade / cloud-gate UI components.

Used by any settings tab that requires a Cloud subscription.
Keeps all subscribe CTA copy and styling in one place (DRY).
"""

from __future__ import annotations

from urllib.parse import urlencode

from fasthtml.common import *
from ui.i18n import t, get_lang

_SUBSCRIBE_BASE = "https://celerp.com/subscribe"


def _subscribe_url(anchor: str = "") -> str:
    """Build subscribe URL with instance_id passthrough if available."""
    from celerp.config import ensure_instance_id
    base = _SUBSCRIBE_BASE
    iid = ensure_instance_id()
    base += "?" + urlencode({"instance_id": iid})
    if anchor:
        base += f"#{anchor}"
    return base


def upgrade_banner(
    feature: str,
    description: str,
    price: str = "USD $29/mo",
    anchor: str = "",
    lang: str = "en",
) -> FT:
    """Full-width banner shown when a cloud feature is not available.

    Args:
        feature: Short feature name, e.g. "Cloud Connectors"
        description: One-line description of what the user gets.
        price: Price string shown on the CTA button.
        anchor: URL fragment to append to /subscribe, e.g. "cloud" or "ai"
        lang: UI language code.
    """
    href = _subscribe_url(anchor)
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
            f"Subscribe - {price}",
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
    anchor: str = "cloud",
    content: FT | None = None,
    lang: str = "en",
) -> FT:
    """Conditionally show upgrade_banner OR the actual feature content.

    Args:
        is_connected: True if the gateway session is active (subscription valid).
        feature: Feature name for the banner.
        description: Banner description.
        price: Price string.
        anchor: /subscribe URL anchor.
        content: The real UI to show when connected. If None, returns only banner.
        lang: UI language code.
    """
    if not is_connected:
        return upgrade_banner(feature, description, price, anchor, lang=lang)
    return content if content is not None else Div()
