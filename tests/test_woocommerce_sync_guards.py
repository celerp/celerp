# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from celerp.connectors.base import SyncDirection
from celerp.connectors.woocommerce import (
    _direction_allows_remote_product_create,
    _link_matches_deleted_product,
    _link_needs_rediscovery,
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
