# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The canonical server pager: range text, current page, disabled edges, and the page
size carried in every target URL."""

from __future__ import annotations

import re

from fasthtml.common import to_xml

from ui.components.table import server_pager


def _href(o: int, l: int) -> str:
    return f"/x?offset={o}&limit={l}"


def _html(offset: int, limit: int, total: int, **kw) -> str:
    return to_xml(server_pager(offset, limit, total, _href, **kw))


def test_range_text_first_middle_last_page():
    assert "1-100 of 250" in _html(0, 100, 250)
    assert "101-200 of 250" in _html(100, 100, 250)
    assert "201-250 of 250" in _html(200, 100, 250)


def test_current_page_marked():
    html = _html(100, 100, 250)
    current = re.findall(r'<a[^>]*aria-current="page"[^>]*>(\d+)</a>', html)
    assert current == ["2"], html


def test_edges_disabled_at_boundaries():
    first = _html(0, 100, 250)
    last = _html(200, 100, 250)
    assert first.count('aria-disabled="true"') == 1
    assert last.count('aria-disabled="true"') == 1
    # Page 1: Prev is the disabled span and Next links to page 2 (offset 100).
    assert re.search(r'<span[^>]*page-btn--disabled[^>]*>Prev</span>', first), first
    assert re.search(r'<a href="/x\?offset=100&amp;limit=100"[^>]*>Next</a>', first), first
    assert re.search(r'<span[^>]*page-btn--disabled[^>]*>Next</span>', last), last


def test_page_size_kept_in_every_target():
    html = _html(50, 50, 250)
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs and all("limit=50" in h for h in hrefs), hrefs
    # The per-page select offers each size from offset 0 with its own limit.
    values = re.findall(r'<option value="([^"]+)"', html)
    assert "/x?offset=0&amp;limit=25" in values and "/x?offset=0&amp;limit=100" in values


def test_navigation_modes():
    swap = _html(0, 100, 250, hx_target="#sec")
    assert 'hx-target="#sec"' in swap and 'hx-select="#sec"' in swap and "onclick" not in swap
    js = _html(0, 100, 250, nav_js="goTo")
    assert "goTo(this.getAttribute('href'))" in js and "hx-get" not in js
    plain = _html(0, 100, 250)
    assert "hx-get" not in plain and "window.location=this.value" in plain


def test_past_the_end_marks_no_page_and_counts_no_rows():
    """page=10 of 3: the body is empty, so no page is current and the label shows none of the
    total; Prev leads back to the last real page."""
    html = _html(900, 100, 250)
    assert 'aria-current="page"' not in html, html
    assert "0 of 250" in html and "201-250" not in html, html
    assert re.search(r'<a href="/x\?offset=200&amp;limit=100"[^>]*>Prev</a>', html), html
    assert re.search(r'<span[^>]*page-btn--disabled[^>]*>Next</span>', html), html


def test_unaligned_offset_labels_the_rows_shown():
    html = _html(30, 25, 250)
    assert "31-55 of 250" in html, html
    assert 'aria-current="page"' not in html, html


def test_no_rows_renders_no_pager():
    assert _html(0, 50, 0) == ""
