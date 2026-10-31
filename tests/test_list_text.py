# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Names listed in copy take the locale's separator and conjunction, never English."""

from __future__ import annotations

from pathlib import Path

from ui.i18n import list_text

ROOT = Path(__file__).resolve().parents[1]


def test_list_text_follows_the_locale():
    assert list_text(["A"], "en") == "A"
    assert list_text(["A", "B", "C"], "en") == "A, B and C"
    assert list_text(["A", "B"], "de") == "A und B"
    assert list_text(["A", "B", "C"], "ja") == "A、BとC"


def test_reconciliation_refusal_lists_names_through_the_locale():
    guard = (ROOT / "default_modules/celerp-accounting/celerp_accounting/recon_guard.py").read_text()
    assert '" and ".join' not in guard and "list_text(" in guard
