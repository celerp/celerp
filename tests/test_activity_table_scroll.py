# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The shared activity table scrolls sideways inside the shared scroll wrapper, so on a
narrow screen it never pushes the page wider than the viewport."""

from __future__ import annotations

import re

from fasthtml.common import to_xml


def test_activity_table_sits_in_the_shared_scroll_wrapper():
    from ui.components.activity import activity_table
    html = to_xml(activity_table([{
        "id": 1, "event_type": "item.created", "entity_id": "item:1", "ts": "2026-10-01T10:00:00Z",
        "data": {"sku": "R-1"},
    }]))
    assert re.search(r'<div class="table-scroll-wrap">\s*<table class="data-table activity-table"', html)


def test_scroll_wrapper_is_an_existing_shared_rule():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css").read_text()
    assert css.count(".table-scroll-wrap {") == 1
