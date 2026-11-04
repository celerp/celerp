# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The question Restart to install asks before it closes Celerp."""

import html
import re

from fasthtml.common import to_xml

from ui.components.shell import _shell_js_i18n, _topbar
from ui.i18n import t


def test_restart_confirm_names_what_comes_next():
    """The user knows Celerp closes, setup shows its window, and Celerp opens again by itself."""
    rendered = to_xml(_topbar([], lang="en"))
    button = re.search(r'<button[^>]*class="update-card__restart-btn"[^>]*>', rendered).group(0)
    confirm = html.unescape(re.search(r'data-confirm="([^"]*)"', button).group(1))
    assert confirm == (
        "Celerp will close for everyone signed in, install the update in its setup window, "
        "and open again by itself in about two minutes. Install the update now?"
    )
    # the notice shown while setup runs gives the same time
    assert _shell_js_i18n("en")["updateInstalling"] == "Installing v{version}. Celerp will be back in about two minutes."


def test_update_card_says_the_update_stopped():
    """A Celerp reopened by a setup that stopped says so on its update card, not that a check failed."""
    assert _shell_js_i18n("en")["updateFailed"]["stopped"] == t("shell.update_stopped_failed", "en")
