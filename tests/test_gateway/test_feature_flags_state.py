# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for gateway feature-flags in-memory state."""


def test_set_get_feature_flags():
    from celerp.gateway.state import set_feature_flags, get_feature_flags
    set_feature_flags({"external_db": True, "external_storage": True})
    assert get_feature_flags() == {"external_db": True, "external_storage": True}


def test_feature_flags_returns_copy():
    from celerp.gateway.state import set_feature_flags, get_feature_flags
    set_feature_flags({"external_db": True})
    flags = get_feature_flags()
    flags["mutated"] = True
    assert "mutated" not in get_feature_flags()


def test_feature_flags_empty_by_default():
    """After clear, feature flags are empty."""
    from celerp.gateway.state import set_feature_flags, get_feature_flags
    set_feature_flags({})
    assert get_feature_flags() == {}
