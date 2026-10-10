"""Line actions on a finalized document are offered only when a line holds stock.

A document whose lines are all services has nothing to ship, hold or take back, so its line
action menu carries none of those options. A document with at least one stocked line keeps
them all; a service line among them is refused by the action itself, with its message.
"""
from __future__ import annotations

from fasthtml.common import to_xml

_STOCK = {"line_id": "11111111-1111-4111-8111-111111111111", "sku": "S-1", "item_id": "item:1",
          "entity_id": "item:1", "quantity": 1, "unit_price": 10}
_SERVICE = {"line_id": "22222222-2222-4222-8222-222222222222", "name": "Consulting",
            "quantity": 3, "unit_price": 100, "sell_by": "hour"}
_SHIPPING = ("value=\"li-fulfill\"", "value=\"li-reserve\"", "value=\"li-revert\"")


def _page(lines, doc_type="invoice", status="final"):
    from ui.routes.documents import _doc_detail
    doc = {"entity_id": "doc:1", "doc_type": doc_type, "status": status, "ref_id": "I-1",
           "line_items": lines}
    return to_xml(_doc_detail(doc, item_status_map={"item:1": "available"}))


def test_a_service_only_invoice_offers_no_shipping_actions():
    html = _page([dict(_SERVICE)])
    for opt in _SHIPPING:
        assert opt not in html, opt
    assert 'id="li-bulk-fulfill-btn"' not in html


def test_a_service_only_memo_offers_no_shipping_actions():
    html = _page([dict(_SERVICE)], doc_type="memo", status="sent")
    for opt in _SHIPPING:
        assert opt not in html, opt


def test_a_stocked_invoice_keeps_every_shipping_action():
    html = _page([dict(_STOCK)])
    for opt in _SHIPPING:
        assert opt in html, opt
    assert 'id="li-bulk-fulfill-btn"' in html


def test_a_mixed_invoice_keeps_every_shipping_action():
    html = _page([dict(_STOCK), dict(_SERVICE)])
    for opt in _SHIPPING:
        assert opt in html, opt


def test_a_draft_invoice_is_unchanged():
    assert 'value="li-fulfill"' not in _page([dict(_STOCK)], status="draft")
