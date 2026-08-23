# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the backup button builders.

``cloud_backup_buttons`` and ``local_backup_buttons`` resolve their tooltip text
and the JS-facing "restoring" label by calling ``t()`` at render time, so a
request in a non-English language gets translated output. These tests prove
that by registering a sentinel language ``xx`` (via the module i18n seam) and
asserting the sentinel text reaches the rendered output while ``xx`` is the
active language. They are red against a tree that resolves any of these
strings at import time or hardcodes English.
"""

import re

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.backup import cloud_backup_buttons, local_backup_buttons

# Sentinel catalog: one unmistakable value per key the backup buttons render.
_XX = {
    "settings.backup_snapshot_tooltip": "XX_SNAPSHOT_TOOLTIP",
    "settings.backup_download_tooltip": "XX_DOWNLOAD_TOOLTIP",
    "settings.backup_import_tooltip": "XX_IMPORT_TOOLTIP",
    "auth.restoring": "XX_RESTORING",
    "btn.backup_now": "XX_BACKUP_NOW",
    "settings.download_backup": "XX_DOWNLOAD_BACKUP",
    "btn.import_backup": "XX_IMPORT_BACKUP",
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


def test_cloud_backup_button_translates_label_and_tooltip():
    html = to_xml(cloud_backup_buttons(enc_ok=True, gw_ok=True))
    assert "XX_BACKUP_NOW" in html
    assert "XX_SNAPSHOT_TOOLTIP" in html


def test_local_backup_buttons_translate_labels_and_tooltips():
    html = to_xml(local_backup_buttons())
    assert "XX_DOWNLOAD_BACKUP" in html
    assert "XX_DOWNLOAD_TOOLTIP" in html
    assert "XX_IMPORT_BACKUP" in html
    assert "XX_IMPORT_TOOLTIP" in html


def test_local_backup_buttons_pass_restoring_text_via_data_attr():
    """The JS-embedded 'restoring' label must arrive through a data-* attribute
    (R2), not spliced into the onchange JS source - and it must be translated."""
    html = to_xml(local_backup_buttons())
    assert 'data-restoring-text="XX_RESTORING"' in html
    # The onchange JS source itself carries no hardcoded English/sentinel text;
    # it only reads the value back off the element's dataset at runtime.
    onchange_attr = re.search(r'onchange="([^"]*)"', html).group(1)
    assert "XX_RESTORING" not in onchange_attr
    assert "dataset.restoringText" in onchange_attr
