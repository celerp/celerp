# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Unit coverage for the document From-address picker.

The picker is sourced from company Locations. A Location with no address (e.g. a stub the user named
"Location 2") must never appear as a selectable option: offering it let the user set the doc's
company_address to the location's *name*, which then printed a bogus address. These tests lock in that
only address-bearing locations are offered and that the option value is the address text, not the name.
"""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.routes.documents import _company_address_picker


def _picker_xml(current, locations):
    return to_xml(_company_address_picker("doc:1", current, locations))


def test_addressless_locations_are_excluded():
    """A location with no address (only a name) is not offered, and with no usable address the picker
    falls back to a plain display cell rather than a dropdown of names."""
    locs = [
        {"id": "1", "name": "Location 2", "address": None},
        {"id": "2", "name": "Location 3", "address": {"text": ""}},
    ]
    xml = _picker_xml("", locs)
    assert "Location 2" not in xml
    assert "Location 3" not in xml
    assert "<select" not in xml  # no addressed locations -> display cell, not a picker


def test_option_value_is_the_address_not_the_name():
    """An address-bearing location is offered; its option value is the address text so selecting it
    stores a real address, while addressless siblings are still filtered out."""
    locs = [
        {"id": "1", "name": "Head Office", "address": {"text": "1 Main St, Town"}},
        {"id": "2", "name": "Location 2", "address": None},
    ]
    xml = _picker_xml("", locs)
    assert "<select" in xml
    assert 'value="1 Main St, Town"' in xml  # value is the address, never the name
    assert "Location 2" not in xml


def test_current_address_not_matching_any_location_offered_as_custom():
    """A doc whose company_address was corrupted to a location name keeps a selectable 'Custom' option so
    the user can still see it and switch back to a real location address."""
    locs = [{"id": "1", "name": "Head Office", "address": {"text": "1 Main St"}}]
    xml = _picker_xml("Location 2", locs)
    assert "<select" in xml
    assert "Custom: Location 2" in xml
    assert 'value="1 Main St"' in xml  # the real address is still selectable
