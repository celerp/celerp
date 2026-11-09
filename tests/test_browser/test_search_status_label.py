# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: global search names a List's status as its page and the Lists table do.

An issued quotation reads Issued on its page, in the Lists table and in the search results,
never the stored word Finalized.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import wait_for_topbar
from .test_record_refusals import _finalized_quotation_list

pytestmark = pytest.mark.browser


def test_an_issued_quotation_reads_issued_in_search_as_on_its_page_and_table(page: Page, ui_server, fresh_company):
    from ui.routes.documents import _list_status_label

    lst = _finalized_quotation_list(fresh_company)
    ref, label = lst["ref_id"], _list_status_label(lst)
    assert label == "Issued", label

    page.goto(f"{ui_server}/lists/{lst['id']}")
    wait_for_topbar(page)
    assert label in page.locator("#main-content").text_content()

    page.goto(f"{ui_server}/lists")
    wait_for_topbar(page)
    row = page.locator("tr", has_text=ref).first
    expect(row).to_be_visible()
    assert label in row.text_content()

    page.locator(".global-search-input").press_sequentially(ref)
    result = page.locator(".search-result-item", has_text=ref).first
    expect(result).to_be_visible(timeout=10000)
    text = result.text_content()
    assert text.rstrip().endswith(f"- {label}"), text
    assert "Finalized" not in text, text
