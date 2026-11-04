# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The question Restart to install asks before it closes Celerp."""

import html
import re

from fasthtml.common import to_xml

from ui.components.shell import _topbar


def test_restart_confirm_names_what_comes_next():
    """The user knows Celerp closes, setup shows its window, and Celerp opens again by itself."""
    rendered = to_xml(_topbar([], lang="en"))
    button = re.search(r'<button[^>]*class="update-card__restart-btn"[^>]*>', rendered).group(0)
    confirm = html.unescape(re.search(r'data-confirm="([^"]*)"', button).group(1))
    assert confirm == (
        "Celerp will close for everyone signed in, install the update in its setup window, "
        "and open again by itself in about two minutes. Install the update now?"
    )
