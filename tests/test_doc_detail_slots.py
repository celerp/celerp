# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The doc_detail_actions and doc_detail_badges slots on a document's detail page.

A module contribution names a render callable. It shows only when the company has
the module switched on and the viewing role holds the contribution's permission,
the same gate item_action and pricing_action use. Hiding it is presentation: the
module's own route must still check the permission. With nothing to show, the
document page renders exactly as it does with no contribution at all.
"""
from __future__ import annotations

import re

import pytest
from fasthtml.common import Button, Span, to_xml

from celerp.modules import slots

_DOC_TYPES = ("invoice", "quotation", "purchase_order", "bill", "memo")
# Each render mints fresh idempotency keys; mask them so two renders compare equal.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def render_action(doc: dict):
    return Button(f"Module action for {doc['entity_id']}", type="button")


def render_badge(doc: dict):
    return Span("Module badge", cls="badge")


def _contribute(**over) -> None:
    for slot, fn in (("doc_detail_actions", "render_action"), ("doc_detail_badges", "render_badge")):
        slots.register(slot, {"render": f"{__name__}:{fn}", "_module": "celerp-ship", **over})


def _page(doc_type="invoice", role="owner", settings=None) -> str:
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": f"doc:{doc_type}-1", "doc_type": doc_type, "status": "draft",
           "ref_id": "D-1", "line_items": []}
    return _UUID.sub("UUID", to_xml(_doc_detail(doc, role=role, settings=settings)))


@pytest.mark.parametrize("doc_type", _DOC_TYPES)
def test_doc_detail_slot_from_disabled_module_contributes_nothing(doc_type):
    plain = _page(doc_type)
    _contribute()
    assert _page(doc_type, settings={"enabled_modules": ["celerp-other"]}) == plain


@pytest.mark.parametrize("doc_type", _DOC_TYPES)
def test_doc_detail_slot_hidden_from_a_role_without_its_permission(doc_type):
    plain = _page(doc_type, role="viewer")
    _contribute(permission="finalize_documents")
    assert _page(doc_type, role="viewer") == plain


def test_doc_detail_slot_shown_to_a_permitted_role():
    _contribute(permission="finalize_documents")
    for role, settings in (("owner", None), ("operator", {"enabled_modules": ["celerp-ship"]}),
                           ("viewer", {"role_grants": {"finalize_documents": ["viewer", "operator",
                                                                             "manager", "admin", "owner"]}})):
        html = _page(role=role, settings=settings)
        assert "Module action for doc:invoice-1" in html, role
        assert "Module badge" in html, role
