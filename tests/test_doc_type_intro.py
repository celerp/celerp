# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A doc-type list whose purpose is not obvious opens with a short banner saying what it is for."""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.i18n import t


def test_production_orders_open_with_their_banner():
    from ui.routes.documents import _doc_type_intro

    html = to_xml(_doc_type_intro("production_order"))
    assert 'class="info-banner"' in html
    assert t("documents.intro_production_order").split(" - ")[0] in html


def test_a_doc_type_without_a_banner_shows_nothing():
    from ui.routes.documents import _doc_type_intro

    assert _doc_type_intro("invoice") == ""
