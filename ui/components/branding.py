# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The Celerp mark every page shows: the logo and the browser-tab icons.

The only module that names a mark file. A partner-managed install wears the Celerp
Enterprise mark, every other install the standard one; the choice follows the
install's commercial mode (``is_partner_managed``), so it reverts on its own when
the partner link ends.
"""

from __future__ import annotations

from dataclasses import dataclass

from fasthtml.common import FT, Img, Link

from ui.components.cloud_gate import is_partner_managed


@dataclass(frozen=True)
class Mark:
    """A mark's display name and its file names under ``/static/``."""

    name: str
    logo: str
    favicon_ico: str
    favicon_32: str
    apple_touch: str


STANDARD = Mark("Celerp", "logo.png", "favicon.ico", "favicon-32x32.png", "apple-touch-icon.png")
ENTERPRISE = Mark("Celerp Enterprise", "logo-enterprise.png", "favicon-enterprise.ico",
                  "favicon-enterprise-32x32.png", "apple-touch-icon-enterprise.png")


def current_mark() -> Mark:
    """The mark this install shows now."""
    return ENTERPRISE if is_partner_managed() else STANDARD


def logo(cls: str) -> FT:
    """The logo image, styled by ``cls`` (``sidebar-logo`` or ``auth-logo``)."""
    mark = current_mark()
    return Img(src=f"/static/{mark.logo}", alt=mark.name, cls=cls)


def favicon_links() -> tuple[FT, ...]:
    """The shield icon set for the browser tab, made from the same master as the logo."""
    mark = current_mark()
    return (
        Link(rel="icon", type="image/x-icon", href=f"/static/{mark.favicon_ico}"),
        Link(rel="icon", type="image/png", sizes="32x32", href=f"/static/{mark.favicon_32}"),
        Link(rel="apple-touch-icon", sizes="180x180", href=f"/static/{mark.apple_touch}"),
    )
