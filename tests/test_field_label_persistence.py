# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Rehydration of built-in item-field metadata on the effective schema.

The item PATCH model drops ``label_key`` and ``tooltip_key`` when a company
customizes its schema, so a stored built-in field survives as a bare
``{key, label, ...}`` dict. ``get_effective_field_schema`` must reattach the
canonical ``label_key``/``tooltip_key`` for built-in keys when building the
effective schema, so a customized company still renders translated built-in
labels and tooltips. Reattachment is guarded: ``label_key`` is restored only
when the stored label still equals the canonical English label (a user-renamed
built-in keeps its literal label and is never translated); ``tooltip_key`` is
restored for every built-in key. Custom fields and dynamic price columns are
never touched.

These tests drive the service with an AsyncMock session (no database) and a
sentinel locale to prove the reattached ``label_key`` reaches the rendered
label. They are red against a tree that returns the stored, stripped dict
unchanged.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from celerp.services.field_schema import get_effective_field_schema

# Sentinel catalog: a built-in whose reattached label_key resolves to an
# unmistakable value while the sentinel language is active.
_XX = {"field.label.sku": "XX_SKU"}


@pytest.fixture(autouse=True)
def _xx_lang():
    from ui import i18n
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _session_for(settings: dict):
    session = AsyncMock()
    company = MagicMock()
    company.settings = settings
    session.get.return_value = company
    return session


@pytest.mark.asyncio
async def test_rehydrate_builtin_label_key_guarded():
    """An untouched built-in regains its label_key; a user-renamed one keeps its
    literal label and gets no label_key."""
    session = _session_for({"item_schema": [
        {"key": "sku", "label": "SKU"},         # untouched default label
        {"key": "name", "label": "Item Code"},  # user-renamed built-in
    ]})
    result = await get_effective_field_schema(session, uuid.uuid4())
    by_key = {f["key"]: f for f in result}
    assert by_key["sku"].get("label_key") == "field.label.sku"
    assert "label_key" not in by_key["name"]
    assert by_key["name"]["label"] == "Item Code"


@pytest.mark.asyncio
async def test_item_schema_patch_round_trip_preserves_label_key():
    """A GET (effective) -> PATCH (model strips metadata) -> GET round trip
    restores label_key and tooltip_key on built-in fields."""
    session = _session_for({})
    first = await get_effective_field_schema(session, uuid.uuid4())
    # Emulate the PATCH model dropping label_key/tooltip_key on every field.
    stripped = [
        {k: v for k, v in f.items() if k not in ("label_key", "tooltip_key")}
        for f in first
    ]
    session = _session_for({"item_schema": stripped})
    second = await get_effective_field_schema(session, uuid.uuid4())
    by_key = {f["key"]: f for f in second}
    assert by_key["sku"].get("label_key") == "field.label.sku"
    assert by_key["quantity"].get("tooltip_key") == "field.tooltip.quantity"


@pytest.mark.asyncio
async def test_saved_schema_renders_translated_label_non_english():
    """A stored, stripped built-in renders its translated label under a
    non-English language after rehydration."""
    from ui.i18n import field_label
    session = _session_for({"item_schema": [
        {"key": "sku", "label": "SKU", "type": "text", "show_in_table": True},
    ]})
    result = await get_effective_field_schema(session, uuid.uuid4())
    sku = next(f for f in result if f["key"] == "sku")
    assert field_label(sku) == "XX_SKU"


@pytest.mark.asyncio
async def test_rehydrate_builtin_tooltip_key():
    """tooltip_key is reattached for a built-in key even when its label was
    renamed (there is no custom-tooltip edit path), while a renamed label still
    gets no label_key."""
    session = _session_for({"item_schema": [
        {"key": "quantity", "label": "Amount on hand"},  # renamed label
    ]})
    result = await get_effective_field_schema(session, uuid.uuid4())
    quantity = next(f for f in result if f["key"] == "quantity")
    assert quantity.get("tooltip_key") == "field.tooltip.quantity"
    assert "label_key" not in quantity


@pytest.mark.asyncio
async def test_category_shadowed_field_keeps_label_key():
    """A built-in shadowed by a category field is re-added raw, then rehydrated,
    so its label_key survives the category overlay path."""
    session = _session_for({
        "item_schema": [{"key": "sku", "label": "SKU"}],
        "category_schemas": {"Widgets": [{"key": "sku", "label": "SKU", "type": "text"}]},
    })
    result = await get_effective_field_schema(session, uuid.uuid4(), category="Widgets")
    sku = next(f for f in result if f["key"] == "sku")
    assert sku.get("label_key") == "field.label.sku"
