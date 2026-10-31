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


def _dependencies() -> list[tuple[str, str, str]]:
    from celerp.connectors import all_connectors

    return [(connector.name, entity, needed) for connector in all_connectors()
            for entity, needs in sorted(connector.outbound_dependencies.items()) for needed in sorted(needs)]


@pytest.mark.parametrize("name, entity, needed", _dependencies())
def test_a_record_cannot_be_sent_without_a_record_it_needs(name, entity, needed):
    """Every sync that waits for another (Accountant Sync journals wait for contacts): a map
    that sends it while the one it needs is Off is refused, naming both, in the person's words."""
    from celerp.connectors.base import dependency_message, flow_of
    from ui.i18n import t

    connector = get(name)
    directions = {key: flow.widest().value for key, flow in connector.entity_flows.items()}
    directions[flow_of(connector, needed)] = "off"
    with pytest.raises(DirectionError) as exc:
        validate_directions(connector, directions)
    message = str(exc.value)
    assert t("connectors.flow_" + flow_of(connector, needed)) in message
    assert message in {dependency_message(connector, waiting, needed)
                       for waiting, needs in connector.outbound_dependencies.items() if needed in needs}


def test_every_starting_map_keeps_what_each_sync_needs():
    from celerp.connectors import all_connectors
    from celerp.connectors.base import restored_directions

    for connector in all_connectors():
        validate_directions(connector, connector.default_directions)
        validate_directions(connector, restored_directions(connector))
