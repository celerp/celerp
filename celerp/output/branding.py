# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Single source for the "Powered by Celerp" attribution: text and links.

Every outbound surface (PDF footer, document print/share layout, expired-share
page, email footer) imports from here, so copy or campaign changes happen in
one place. The utm_medium is the one per-surface variable: it is what lets
analytics tell the surfaces apart.
"""
from __future__ import annotations

BRAND_TEXT = "Powered by Celerp.com - Downloadable ERP for Serious Businesses"
EMAIL_BRAND_LINE = "Sent with Celerp, the downloadable ERP for serious businesses."


def brand_url(medium: str) -> str:
    """Attribution link for one surface (medium: pdf, doc-print, share-page, email)."""
    return (
        "https://www.celerp.com/?utm_source=powered-by"
        f"&utm_medium={medium}&utm_campaign=attribution"
    )
