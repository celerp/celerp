# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Web Access (Celerp Connect) settings page.

The cloud/Connect settings helpers build their user-facing chrome (plan-card price
suffix, backup-schedule countdown, and the infrastructure form's confirm prompt) by
calling ``t()`` at render time, so a request in a non-English language gets translated
output. These tests register a sentinel language ``xx`` and assert the sentinel text
reaches the rendered output while ``xx`` is active. They are red against a tree that
hardcodes English (``"/mo"``, ``f"in {hours}h {mins}m"``, the ``confirm(...)`` string).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_cloud import (
    _plan_card,
    _backup_summary_card,
    _infra_db_section,
)

# Sentinel catalog: one unmistakable value per new key exercised below.
_XX = {
    "settings_cloud.per_mo": "XX_PERMO",
    "settings_cloud.in_hours_mins": "XX_INHM {hours}h {mins}m",
    "settings_cloud.restart_server_confirm": "XX_RESTART_CONFIRM",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_plan_card_price_suffix_translates():
    """The "/mo" price suffix resolves at render time (module-render label)."""
    html = to_xml(_plan_card("Connect", "USD $29", "desc", ["a", "b"],
                             "https://example.test/subscribe", lang="xx"))
    assert "XX_PERMO" in html


def test_backup_countdown_translates():
    """The next-backup countdown value in the detail table resolves at render time
    (interpolated table-cell value)."""
    future = (datetime.now(timezone.utc) + timedelta(hours=2, minutes=5)).isoformat()
    backup_data = {"db": {"last_run": None, "ok": None}, "next_db_utc": future}
    html = to_xml(_backup_summary_card(gw_ok=True, backup_data=backup_data))
    assert "XX_INHM" in html


def test_infra_confirm_prompt_translates():
    """The Save & restart confirm prompt is carried as an hx-confirm attribute value
    resolved at render time (HTML attribute mechanism)."""
    html = to_xml(_infra_db_section())
    assert "XX_RESTART_CONFIRM" in html
