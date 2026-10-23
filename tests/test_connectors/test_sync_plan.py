# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A connector runs the syncs each kind of record is set to, and nothing for one set to Off."""
from __future__ import annotations

import pytest

from celerp.connectors.base import DirectionError, entity_allowed, validate_directions
from celerp.connectors.registry import get
from celerp.connectors.sync_runner import sync_plan


def test_products_out_orders_in():
    woo = get("woocommerce")
    directions = {"products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off"}
    assert sync_plan(woo, directions) == ["orders", "products_out", "inventory_out"]
    assert entity_allowed(woo, "orders", directions)
    assert entity_allowed(woo, "products_out", directions)
    assert not entity_allowed(woo, "products", directions)


def test_off_entities_are_skipped():
    woo = get("woocommerce")
    directions = {"products": "off", "stock": "off", "orders": "inbound", "customers": "off"}
    assert sync_plan(woo, directions) == ["orders"]


def test_new_woocommerce_connection_defaults():
    woo = get("woocommerce")
    assert woo.default_directions == {
        "products": "outbound", "stock": "outbound", "orders": "inbound", "customers": "off",
    }


def test_each_entity_offers_only_what_the_connector_supports():
    woo = get("woocommerce")
    assert [d.value for d in woo.entity_flows["customers"].allowed()] == ["off"]
    assert [d.value for d in woo.entity_flows["stock"].allowed()] == ["outbound", "off"]
    assert [d.value for d in woo.entity_flows["products"].allowed()] == [
        "inbound", "outbound", "both", "off"]


def test_a_value_the_connector_cannot_sync_is_refused_by_name():
    woo = get("woocommerce")
    with pytest.raises(DirectionError) as exc:
        validate_directions(woo, {"products": "outbound", "stock": "inbound",
                                  "orders": "inbound", "customers": "off"})
    assert "Stock" in str(exc.value) and "WooCommerce to Celerp" in str(exc.value)


def test_every_flow_names_a_sync_the_connector_implements():
    from celerp.connectors import all_connectors
    from celerp.connectors.sync_runner import implements

    for connector in all_connectors():
        for flow in connector.entity_flows.values():
            for entity in (*flow.inbound, *flow.outbound):
                assert implements(connector, entity), (connector.name, entity)
