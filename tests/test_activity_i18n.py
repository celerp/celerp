# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared activity feed.

The activity component builds every user-facing label, detail fragment, relative
time, and table chrome by calling ``t()`` at render time, so a request in a
non-English language gets translated output. These tests prove that by
registering a sentinel language ``xx`` (via the module i18n seam) and asserting
the sentinel text reaches the rendered output while ``xx`` is the active
language. They are red against a tree that resolves any of these strings at
import time or hardcodes English.
"""

import pytest
from datetime import datetime, timedelta, timezone

from fasthtml.common import to_xml

from ui import i18n
from ui.components.activity import (
    event_label,
    detail_from_entry,
    relative_time,
    activity_table,
    _fields_changed_summary,
)

# Sentinel catalog: one unmistakable value per key the activity feed renders.
_XX = {
    "event.item.merged": "XX_MERGED",
    "event.doc.paid": "XX_PAID",
    "event.file.attached": "XX_FILE_ATTACHED",
    "activity.qty_to": "XX_QTYTO {qty}",
    "field.status": "XX_STATUS",
    "time.days_ago": "XX {n} DAYS",
    "activity.recent_activity": "XX_RECENT",
    "activity.empty": "XX_EMPTY",
    "activity.showing_last": "XX_SHOWING {n}",
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


def test_event_label_translates_known_type():
    assert event_label("item.merged") == "XX_MERGED"
    assert event_label("doc.paid") == "XX_PAID"


def test_event_label_translates_file_event():
    assert event_label("item.file.attached") == "XX_FILE_ATTACHED"


def test_detail_fragment_translates():
    out = detail_from_entry({"new_qty": 7}, "item.quantity.adjusted")
    assert "XX_QTYTO" in out


def test_fields_changed_summary_translates_label():
    out = _fields_changed_summary({"status": {"old": "draft", "new": "final"}})
    assert "XX_STATUS" in out


def test_relative_time_translates():
    ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert relative_time(ts) == "XX 3 DAYS"


def test_activity_table_chrome_translates():
    empty = to_xml(activity_table([]))
    assert "XX_RECENT" in empty
    assert "XX_EMPTY" in empty


def test_activity_table_footer_translates():
    ledger = [{"event_type": "doc.paid", "ts": "2026-03-25T07:30:01+00:00",
               "actor_name": "Tester", "data": {"amount": 100}} for _ in range(3)]
    html = to_xml(activity_table(ledger, max_display=2, history_url="/docs/history"))
    assert "XX_SHOWING" in html
