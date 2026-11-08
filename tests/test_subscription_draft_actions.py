# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every state-changing control a draft agreement's page shows in its header saves the page's
pending line edits first, through the same save-then-post path as Finalize, so the action runs on
the lines the page shows."""
from __future__ import annotations

import json

from ui.routes.documents import save_then_post
from ui.routes.subscriptions import _sub_action_controls

_EID = "doc:SUB-1"
_DIRECT = ("hx-post", "hx-put", "hx-patch", "hx-delete", "hx_post", "hx_put", "hx_patch", "hx_delete")


def _nodes(items):
    for node in items:
        if hasattr(node, "attrs"):
            yield node
            yield from _nodes(node.children)


def _drafts():
    yield {"status": "draft"}
    yield {"status": "draft", "next_bill_date": "2027-03-01", "frequency": "monthly"}


def test_every_draft_header_action_saves_the_lines_first():
    prefix, _, suffix = save_then_post("").partition('""')
    for sub in _drafts():
        left, right = _sub_action_controls(_EID, sub, sub.get("next_bill_date"))
        actions = [n for n in _nodes([*left, *right]) if n.tag in ("button", "form")]
        assert any("onclick" in n.attrs for n in actions), actions
        for node in actions:
            assert not set(node.attrs) & set(_DIRECT), node
            js = node.attrs.get("onclick", "")
            assert js.startswith(prefix) and js.endswith(suffix), node
            url = json.loads(js[len(prefix):len(js) - len(suffix)])
            assert url.startswith(f"/subscriptions/{_EID}/") and js == save_then_post(url), node
