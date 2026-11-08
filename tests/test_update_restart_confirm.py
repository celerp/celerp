# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The question Restart to install asks before it closes Celerp."""

import html
import re
import sys

import pytest
from fasthtml.common import to_xml

from ui.components.shell import _shell_js_i18n, _topbar
from ui.i18n import t
from ui.routes.settings import _company_tab


def _confirm() -> str:
    rendered = to_xml(_topbar([], lang="en"))
    button = re.search(r'<button[^>]*class="update-card__restart-btn"[^>]*>', rendered).group(0)
    return html.unescape(re.search(r'data-confirm="([^"]*)"', button).group(1))


def _runtime(monkeypatch, channel: str | None, platform: str) -> None:
    if channel is None:
        monkeypatch.delenv("CELERP_INSTALL_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("CELERP_INSTALL_CHANNEL", channel)
    monkeypatch.setattr(sys, "platform", platform)


def test_restart_confirm_names_what_comes_next(monkeypatch):
    """On the Windows desktop the user knows Celerp closes, setup shows its window, and Celerp opens again by itself."""
    _runtime(monkeypatch, "electron", "win32")
    assert _confirm() == (
        "Celerp will close for everyone signed in, install the update in its setup window, "
        "and open again by itself in about two minutes. Install the update now?"
    )
    # the notice shown while setup runs gives the same time
    assert _shell_js_i18n("en")["updateInstalling"] == "Installing v{version}. Celerp will be back in about two minutes."


@pytest.mark.parametrize("channel", ["electron", "pypi", None])
@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_restart_confirm_text_matches_platform(monkeypatch, channel, platform):
    """Only the Windows desktop app updates in a setup window; every other runtime keeps today's text."""
    _runtime(monkeypatch, channel, platform)
    windows = channel == "electron" and platform == "win32"
    suffix = "_windows" if windows else ""
    assert _confirm() == t("shell.update_restart_confirm" + suffix, "en")
    assert _shell_js_i18n("en")["updateInstalling"] == t("shell.update_installing" + suffix, "en")
    assert t("shell.update_restart_confirm_windows", "en") != t("shell.update_restart_confirm", "en")
    assert t("shell.update_installing_windows", "en") != t("shell.update_installing", "en")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_uninstall_cards_show_on_the_macos_desktop_only(monkeypatch, platform):
    """The uninstall cards give the macOS steps, so only the owner on the macOS desktop app sees them."""
    def cards(is_owner: bool) -> str:
        return to_xml(_company_tab({"name": "Acme"}, "en", is_owner=is_owner))

    keep, delete = html.escape(t("settings.uninstall_keep_data", "en")), html.escape(t("settings.uninstall_delete_data", "en"))
    _runtime(monkeypatch, "electron", platform)
    owner = cards(True)
    if platform == "darwin":
        assert keep in owner and delete in owner
        button = re.search(r'<button[^>]*data-confirm="[^"]*"[^>]*uninstallDeleteData[^>]*>', owner).group(0)
        assert html.unescape(re.search(r'data-confirm="([^"]*)"', button).group(1)) == t("settings.uninstall_confirm_js", "en")
    else:
        assert keep not in owner and delete not in owner
    non_owner = cards(False)
    assert keep not in non_owner and delete not in non_owner
    _runtime(monkeypatch, None, platform)
    owner = cards(True)
    assert keep not in owner and delete not in owner


def test_update_card_says_the_update_stopped():
    """A Celerp reopened by a setup that stopped says so on its update card, not that a check failed."""
    assert _shell_js_i18n("en")["updateFailed"]["stopped"] == t("shell.update_stopped_failed", "en")
