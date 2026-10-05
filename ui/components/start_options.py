# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The two other ways to start a company: restore a Celerp backup, or move books in
from another system. One component, shown under the setup form and on the
dashboard's "Bring in your data" card, so both read the same.
"""

from __future__ import annotations

from fasthtml.common import *

import ui.api_client as api
from ui.api_client import APIError
from ui.i18n import t


async def supported_sources() -> list[str]:
    """Names of the systems books can be moved from, from the server's own list.
    Empty when the list cannot be read, so nothing is ever invented."""
    try:
        sources = await api.migration_sources()
    except APIError:
        return []
    return [str(s.get("display_name") or s.get("key")) for s in sources if s.get("display_name") or s.get("key")]


def _option_row(icon: str, title: str, desc: str, href: str, *extra) -> FT:
    # The icon sits on the title's line, so the text below starts at the box's edge.
    return Div(
        Div(
            Span(icon, cls="start-option-icon", aria_hidden="true"),
            A(title, href=href, cls="auth-link start-option-title"),
            cls="start-option-head",
        ),
        P(desc, cls="start-option-desc"),
        *extra,
        cls="start-option",
    )


def start_options(*, restore_href: str | None, move_href: str, sources: list[str]) -> FT:
    """The option rows. ``restore_href`` None leaves the restore row out (a role
    that cannot restore)."""
    move_extra = []
    if sources:
        move_extra.append(P(t("setup.sources_supported", names=", ".join(sources)), cls="start-option-desc"))
        move_extra.append(P(t("setup.more_coming_soon"), cls="start-option-note"))
    rows = []
    if restore_href:
        rows.append(_option_row("↺", t("setup.option_restore_title"), t("setup.option_restore_desc"),
                                restore_href))
    rows.append(_option_row("⇄", t("setup.option_move_title"), t("setup.option_move_desc"), move_href,
                            *move_extra))
    return Div(*rows, cls="start-options")
