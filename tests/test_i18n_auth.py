# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the auth / onboarding routes.

The auth pages (login, setup, backup restore, onboarding, password reset) build
every user-facing label, heading, button, placeholder, and status message by
calling ``t()`` at render time. These tests prove that by registering a sentinel
language ``xx`` and asserting its unmistakable values reach the rendered output
while ``xx`` is active. They are red against a tree that hardcodes the English
strings or resolves them at import time.

Mechanisms covered: a render-time label in a component, a translated value
handed to inline JavaScript via a ``data-*`` attribute (R2), and an interpolated
status message built in a plain helper.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.auth import (
    _setup_import_form,
    _restore_notice_message,
    _direct_connection_gate,
)

# Sentinel catalog: one unmistakable value per auth key under test.
_XX = {
    "page.restore_from_backup": "XX_RESTORE_TITLE",
    "auth.upload_backup_desc": "XX_UPLOAD_BACKUP_DESC",
    "auth.backup_file_label": "XX_BACKUP_FILE_LABEL",
    "auth.restore_backup_btn": "XX_RESTORE_BTN",
    "auth.restoring": "XX_RESTORING",
    "auth.restore_notice_message": "XX_RESTORED {company}",
    "auth.direct_connection_gate_body": "XX_GATE_BODY",
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


def test_setup_import_form_labels_translate():
    # Render-time labels: heading, description, field label, and button.
    html = to_xml(_setup_import_form())
    assert "XX_RESTORE_TITLE" in html
    assert "XX_UPLOAD_BACKUP_DESC" in html
    assert "XX_BACKUP_FILE_LABEL" in html
    assert "XX_RESTORE_BTN" in html


def test_restore_button_loading_label_via_data_attr():
    # R2: the JS loading label is passed through a data-* attribute, not spliced
    # into the script source.
    html = to_xml(_setup_import_form())
    assert 'data-loading-label="XX_RESTORING"' in html


def test_restore_notice_message_interpolates():
    # Interpolation: the company name fills the {company} placeholder at render.
    out = _restore_notice_message({"company_name": "Acme"})
    assert "XX_RESTORED" in out
    assert "Acme" in out


def test_direct_connection_gate_body_translates():
    html = to_xml(_direct_connection_gate("user@example.com", "pw"))
    assert "XX_GATE_BODY" in html
