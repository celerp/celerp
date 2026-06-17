# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Unit coverage for the contact/company address card.

Two regressions are locked in here:
  1. Address ids contain a ':' (e.g. ``address:self-<uuid>``), which is invalid in a CSS id selector.
     The card id and the Edit button's hx-target must be colon-escaped to the same value, or the Edit
     button silently does nothing (HTMX can't resolve ``#addr-address:self-...``).
  2. The primary (is_default) address must not be deletable - the card omits the delete button on it so
     the contact (incl. the company self-contact) always keeps a primary billing address.
"""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.contacts import _address_card


def test_edit_target_is_colon_safe_and_matches_card_id():
    addr = {"address_id": "address:self-abc123", "address_type": "billing", "line1": "1 Main St", "is_default": False}
    xml = to_xml(_address_card("contact:1", addr))
    # The colon is escaped to '-' so the selector is valid, and the same value is used as the card id.
    assert 'id="addr-address-self-abc123"' in xml
    assert 'hx-target="#addr-address-self-abc123"' in xml
    assert "#addr-address:self-abc123" not in xml  # the invalid (colon) selector must not appear


def test_primary_address_has_no_delete_button():
    primary = {"address_id": "address:x", "address_type": "billing", "line1": "1 Main St", "is_default": True}
    xml = to_xml(_address_card("contact:1", primary))
    assert "hx-delete" not in xml  # primary cannot be removed


def test_non_primary_address_has_delete_button():
    other = {"address_id": "address:y", "address_type": "shipping", "line1": "2 Side Rd", "is_default": False}
    xml = to_xml(_address_card("contact:1", other))
    assert "hx-delete" in xml
