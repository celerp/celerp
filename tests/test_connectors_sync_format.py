# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure-render tests for the connector sync summary. Confirms it uses the canonical
relative_time() formatter and thousands-separated counts (no bespoke time math), and
handles never-synced / in-progress / naive-datetime cases. No DB or HTTP."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fasthtml.common import to_xml

from ui.routes.settings_connectors import _last_sync_info


def _run(**kw):
    base = dict(finished_at=None, status="success", created_count=0, updated_count=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_never_synced():
    assert "Never synced" in to_xml(_last_sync_info(None))


def test_in_progress_shows_syncing():
    out = to_xml(_last_sync_info(_run(finished_at=None, status="running")))
    assert "Syncing" in out  # i18n key resolved (not the raw key)


def test_terminal_uses_relative_time_and_separators():
    r = _run(finished_at=datetime.now(timezone.utc) - timedelta(seconds=300),
             status="success", created_count=1234, updated_count=12)
    out = to_xml(_last_sync_info(r))
    assert "5m ago" in out       # canonical relative_time(), not hand-rolled math
    assert "+1,234 ~12" in out   # thousands-separated counts
    assert "success" in out


def test_naive_datetime_treated_as_utc():
    r = _run(finished_at=(datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None),
             status="partial", created_count=5, updated_count=0)
    out = to_xml(_last_sync_info(r))
    assert "2h ago" in out and "partial" in out
