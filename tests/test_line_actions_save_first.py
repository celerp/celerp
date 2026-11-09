# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every action a draft page with editable lines offers whose server side reads the stored lines
(Finalize, Issue, Write off stock, Duplicate, Convert, Send) saves the page's pending line edits
first, through the same path as Finalize (persist_then_post). Every other posting control on such a
page is named here with the reason it does not read the lines, so a new button fails until decided.
Pages render with every optional feature on (sharing, email, online payment), so a control behind
a setting is decided too."""
from __future__ import annotations

from celerp.services.list_behavior import LIST_BEHAVIOR
from ui.routes.documents import _DOC_TYPES, _doc_detail, persist_then_post

# Server actions that read the stored lines.
_ON_LINES = ("/action/finalize", "/action/write-off", "/action/duplicate", "/convert", "/action/send")
# Posting controls that never read the lines: a status change, deleting the record, a note, and the
# header text fields (customer note, terms), which save on their own.
_NOT_ON_LINES = ("/action/mark_sent", "/action/delete", "/notes", "/field/customer_note", "/field/terms_text")
_DIRECT = ("hx_post", "hx-post", "hx_confirm", "hx-confirm")
_LINES = [{"line_id": "l1", "name": "Row", "description": "Row", "quantity": 1, "unit_price": 1, "line_total": 1}]
_WRITE_OFF = LIST_BEHAVIOR["writeoff"].terminal[0]
_FEATURES_ON = {"share_enabled": True, "share_active": True, "payments_on": True, "free_send_offer": True,
                "email_quota": 10}


def _nodes(items):
    for node in items:
        if hasattr(node, "attrs"):
            yield node
            yield from _nodes(node.children)


def _drafts():
    for list_type in LIST_BEHAVIOR:
        yield {"entity_id": "list:L-1", "doc_type": "list", "list_type": list_type, "status": "draft",
               "line_items": _LINES, "currency": "USD"}
    for doc_type in [d for d in _DOC_TYPES if d != "list"] + ["quotation"]:
        yield {"entity_id": "doc:D-1", "doc_type": doc_type, "status": "draft", "line_items": _LINES, "currency": "USD"}


def _posts(doc: dict, role: str = "owner") -> list[tuple[str, object]]:
    """(url, node) for every control on the rendered page that posts."""
    out = []
    for node in _nodes([_doc_detail(doc, role=role, **_FEATURES_ON)]):
        url = node.attrs.get("hx-post") or node.attrs.get("hx_post")
        js = node.attrs.get("onclick") or node.attrs.get("onsubmit") or ""
        if not url and "_celerpPostOnLines(" in js:
            url = js.split("_celerpPostOnLines('", 1)[1].split("'", 1)[0]
        if url:
            out.append((url, node))
    return out


def test_every_action_on_the_lines_saves_them_first():
    found = set()
    for doc in _drafts():
        for url, node in _posts(doc):
            on_lines = url.endswith(_ON_LINES)
            assert on_lines or url.endswith(_NOT_ON_LINES), f"undecided posting control {url} on {doc['doc_type']}"
            if not on_lines:
                continue
            found.add(url.rsplit("/", 1)[-1])
            assert not set(node.attrs) & set(_DIRECT), node
            confirm = _WRITE_OFF.confirm if url.endswith(f"/action/{_WRITE_OFF.key}") else None
            assert persist_then_post(url, confirm) in (node.attrs.get("onclick"), node.attrs.get("onsubmit")), node
    assert found == {u.rsplit("/", 1)[-1] for u in _ON_LINES}


def test_write_off_asks_before_it_saves():
    assert '"' + _WRITE_OFF.confirm + '"' in persist_then_post("/lists/list:L-1/action/write-off", _WRITE_OFF.confirm)


def test_without_editable_lines_the_actions_post_directly():
    writeoff = next(d for d in _drafts() if d.get("list_type") == "writeoff")
    posts = dict(_posts(writeoff, role="viewer"))
    button = posts["/lists/list:L-1/action/write-off"]
    assert button.attrs.get("hx_confirm", button.attrs.get("hx-confirm")) == _WRITE_OFF.confirm
    assert "onclick" not in button.attrs
