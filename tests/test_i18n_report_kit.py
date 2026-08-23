# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the report_kit presentation primitives.

report_kit builds the period subtitle, the print-view report header, the
print-shell footer, and the plain-text export/print error body by calling
``t()`` at render time, so a request in a non-English language gets translated
output. These tests prove that by registering a sentinel language ``xx`` (via
the module i18n seam) and asserting the sentinel text reaches the rendered
output while ``xx`` is the active language. They are red against a tree that
resolves any of these strings at import time or hardcodes English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.api_client import APIError
from ui.components.report_kit import (
    period_subtitle,
    plain_error_response,
    print_shell,
    report_header,
)

# Sentinel catalog: one unmistakable value per key report_kit renders.
_XX = {
    "reports.period_from": "XX_FROM {date}",
    "reports.period_to": "XX_TO {date}",
    "reports.all_periods": "XX_ALL_PERIODS",
    "doc.tax_id": "XX_TAXID",
    "reports.printed": "XX_PRINTED {date}",
    "reports.powered_by": "XX_POWERED",
    "shell.error_prefix": "XX_ERROR",
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


def test_period_subtitle_translates_from_and_to():
    out = period_subtitle("2026-01-01", "2026-01-31")
    assert "XX_FROM 2026-01-01" in out
    assert "XX_TO 2026-01-31" in out


def test_period_subtitle_translates_all_periods():
    assert period_subtitle("", "") == "XX_ALL_PERIODS"


def test_report_header_translates_tax_id_and_printed():
    company = {"name": "Acme", "address": "", "tax_id": "TX-1"}
    out = to_xml(report_header(company, "Trial Balance"))
    assert "XX_TAXID TX-1" in out
    assert "XX_PRINTED" in out


def test_print_shell_translates_powered_by():
    company = {"name": "Acme"}
    out = to_xml(print_shell(company, "Trial Balance", "", "<div></div>"))
    assert "XX_POWERED" in out


def test_plain_error_response_translates_error_prefix():
    response = plain_error_response(APIError(400, "bad request"))
    assert response.body == b"XX_ERROR bad request"
