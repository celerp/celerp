# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from celerp.connectors.base import SyncDirection
from celerp.connectors.woocommerce import (
    _deleted_variation_requires_import,
    _direction_allows_remote_product_create,
    _link_matches_deleted_product,
    _link_needs_rediscovery,
)
from celerp_inventory.services import (
    deleted_external_link_may_relink,
    external_link_intentionally_disabled,
    relinked_external_sync_enabled,
    external_identity_key,
    _same_external_identity,
    build_channel_states,
    catalog_family_rows,
)


def test_woocommerce_remote_create_respects_global_direction():
    assert _direction_allows_remote_product_create(SyncDirection.OUTBOUND)
    assert _direction_allows_remote_product_create("both")
    assert not _direction_allows_remote_product_create("inbound")
    assert not _direction_allows_remote_product_create(None)


def test_deleted_parent_matches_all_variations_but_exact_variation_is_scoped():
    red = {"product_id": "20", "variation_id": "201"}
    blue = {"product_id": "20", "variation_id": "202"}
    other = {"product_id": "21", "variation_id": "203"}

    assert _link_matches_deleted_product(red, "20", None)
    assert _link_matches_deleted_product(blue, "20", None)
    assert not _link_matches_deleted_product(other, "20", None)

    assert _link_matches_deleted_product(red, "20", "201")
    assert not _link_matches_deleted_product(blue, "20", "201")


def test_only_remote_deleted_links_are_rediscovered():
    assert _link_needs_rediscovery({
        "product_id": "20", "sync_enabled": False, "remote_deleted": True,
    })
    assert not _link_needs_rediscovery({
        "product_id": "20", "sync_enabled": False, "remote_deleted": False,
    })
    assert not _link_needs_rediscovery({})


def test_remote_delete_allows_relink_but_manual_disable_does_not():
    assert deleted_external_link_may_relink({
        "product_id": "20", "sync_enabled": False, "remote_deleted": True,
    })
    assert not deleted_external_link_may_relink({
        "product_id": "20", "sync_enabled": False, "remote_deleted": False,
    })


def test_deleted_variation_requires_exact_reimport():
    assert _deleted_variation_requires_import({
        "product_id": "20", "variation_id": "201",
        "sync_enabled": False, "remote_deleted": True,
    })
    assert not _deleted_variation_requires_import({
        "product_id": "20", "sync_enabled": False, "remote_deleted": True,
    })


def test_relink_preserves_manual_sync_preference():
    assert relinked_external_sync_enabled({
        "product_id": "20", "sync_enabled": True, "remote_deleted": True,
    })
    assert not relinked_external_sync_enabled({
        "product_id": "20", "sync_enabled": False, "remote_deleted": True,
    })


def test_manual_disable_blocks_product_inbound_but_remote_delete_does_not():
    assert external_link_intentionally_disabled({
        "product_id": "20", "sync_enabled": False, "remote_deleted": False,
    })
    assert not external_link_intentionally_disabled({
        "product_id": "20", "sync_enabled": False, "remote_deleted": True,
    })
    assert not external_link_intentionally_disabled({
        "product_id": "20", "sync_enabled": True, "remote_deleted": False,
    })


def test_platform_identity_uses_the_platform_specific_variant_key():
    shopify = {"product_id": "20", "variant_id": "201"}
    woo = {"product_id": "20", "variation_id": "201"}
    assert external_identity_key("shopify", shopify) == ("20", "201")
    assert external_identity_key("woocommerce", woo) == ("20", "201")
    assert _same_external_identity("shopify", shopify, "20", "201") is True
    assert _same_external_identity("woocommerce", woo, "20", "201") is True


def test_detached_external_link_suppresses_legacy_identity_fallback():
    from celerp_inventory.services import external_link_for_state

    state = {
        "idempotency_key": "woocommerce:55",
        "external_links": {"woocommerce": {"detached": True}},
    }
    assert external_link_for_state(state, "woocommerce") == {}


def test_structural_catalog_family_survives_anchor_sku_change():
    from types import SimpleNamespace

    anchor = SimpleNamespace(
        entity_id="item:anchor",
        is_sync_to_shopify=False,
        state={
            "sku": "NEW-SKU",
            "_catalog_sku_aliases": ["OLD-SKU"],
            "external_links": {
                "woocommerce": {"product_id": "42", "sync_enabled": True}
            },
        },
    )
    explicit_lot = SimpleNamespace(
        entity_id="item:explicit",
        is_sync_to_shopify=False,
        state={
            "sku": "OLDER-SKU",
            "catalog_item_id": "item:anchor",
            "quantity": 2,
            "status": "available",
        },
    )
    legacy_lot = SimpleNamespace(
        entity_id="item:legacy",
        is_sync_to_shopify=False,
        state={
            "sku": "OLD-SKU",
            "barcode": "OLD-LOT",
            "quantity": 3,
            "status": "available",
        },
    )
    rows = [anchor, explicit_lot, legacy_lot]
    assert catalog_family_rows(rows, anchor) == rows
    state = build_channel_states(rows)
    assert state["item:explicit"]["woocommerce"]["anchor_id"] == "item:anchor"
    assert state["item:legacy"]["woocommerce"]["anchor_id"] == "item:anchor"


def test_legacy_sku_history_stays_unassigned_when_two_explicit_anchors_claim_it():
    from types import SimpleNamespace

    def anchor(entity_id: str, sku: str, product_id: str):
        return SimpleNamespace(
            entity_id=entity_id,
            is_sync_to_shopify=False,
            state={
                "sku": sku,
                "_catalog_sku_aliases": ["OLD-SKU"],
                "external_links": {
                    "woocommerce": {
                        "product_id": product_id,
                        "sync_enabled": True,
                    }
                },
            },
        )

    left = anchor("item:left", "LEFT", "41")
    right = anchor("item:right", "RIGHT", "42")
    legacy = SimpleNamespace(
        entity_id="item:legacy",
        is_sync_to_shopify=False,
        state={"sku": "OLD-SKU", "quantity": 1, "status": "available"},
    )
    rows = [left, right, legacy]

    assert legacy not in catalog_family_rows(rows, left)
    assert legacy not in catalog_family_rows(rows, right)


def test_reused_historical_sku_stays_a_separate_product_root():
    from types import SimpleNamespace

    anchor = SimpleNamespace(
        entity_id="item:old",
        is_sync_to_shopify=False,
        state={
            "sku": "NEW-SKU",
            "_catalog_sku_aliases": ["OLD-SKU"],
            "external_links": {
                "woocommerce": {"product_id": "42", "sync_enabled": True}
            },
        },
    )
    reused = SimpleNamespace(
        entity_id="item:new",
        is_sync_to_shopify=False,
        state={
            "sku": "OLD-SKU",
            "name": "New product",
            "quantity": 1,
            "status": "available",
        },
    )
    rows = [anchor, reused]
    assert catalog_family_rows(rows, anchor) == [anchor]
    assert catalog_family_rows(rows, reused) == [reused]
    states = build_channel_states(rows)
    assert "woocommerce" not in states[reused.entity_id]


def test_reused_historical_sku_children_follow_current_product_root():
    from types import SimpleNamespace

    old_anchor = SimpleNamespace(
        entity_id="item:old",
        is_sync_to_shopify=False,
        state={
            "sku": "NEW-SKU",
            "_catalog_sku_aliases": ["OLD-SKU"],
            "external_links": {
                "woocommerce": {"product_id": "42", "sync_enabled": True}
            },
        },
    )
    new_anchor = SimpleNamespace(
        entity_id="item:new",
        is_sync_to_shopify=False,
        state={"sku": "OLD-SKU", "name": "New product", "quantity": 0, "status": "available"},
    )
    physical = SimpleNamespace(
        entity_id="item:new-stock",
        is_sync_to_shopify=False,
        state={
            "sku": "OLD-SKU",
            "barcode": "NEW-STOCK-1",
            "quantity": 1,
            "status": "available",
        },
    )
    rows = [old_anchor, new_anchor, physical]

    assert catalog_family_rows(rows, old_anchor) == [old_anchor]
    assert catalog_family_rows(rows, new_anchor) == [new_anchor]
    assert physical not in catalog_family_rows(rows, old_anchor)
    assert physical not in catalog_family_rows(rows, new_anchor)
