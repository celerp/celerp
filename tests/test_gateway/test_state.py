# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for celerp.gateway.state URL builders."""

from __future__ import annotations

from celerp.gateway.state import build_handoff_url, build_subscribe_url


# ── build_handoff_url ─────────────────────────────────────────────────────────

def test_handoff_basic_medium():
    assert build_handoff_url("/github", medium="cli") == \
        "https://celerp.com/github?utm_source=app&utm_medium=cli"


def test_handoff_default_medium():
    assert build_handoff_url("/github") == \
        "https://celerp.com/github?utm_source=app&utm_medium=inapp"


def test_handoff_lead_extra_anchor():
    url = build_handoff_url("/x", medium="m", lead="id=1", extra="?a=b", anchor="sec")
    assert url == "https://celerp.com/x?id=1&utm_source=app&utm_medium=m&a=b#sec"


# ── build_subscribe_url (behavior preserved by the DRY refactor) ───────────────

def test_subscribe_plain():
    assert build_subscribe_url() == \
        "https://celerp.com/subscribe?utm_source=app&utm_medium=inapp"


def test_subscribe_with_instance():
    assert build_subscribe_url("iid") == \
        "https://celerp.com/subscribe?instance_id=iid&utm_source=app&utm_medium=inapp"


def test_subscribe_topup_anchor_extra():
    assert build_subscribe_url("iid", "go", topup=True, extra="?p=1") == \
        "https://celerp.com/subscribe/topup?instance_id=iid&utm_source=app&utm_medium=inapp&p=1#go"
