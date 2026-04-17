# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Phase A tests: field_schema service, references helper, loader dep enforcement."""
from __future__ import annotations

import sys
import textwrap
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from celerp.modules import slots
from celerp.modules.loader import ModuleLoadError, _load_one, load_all
from celerp.modules import loader as _loader_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(tmp_path):
    slots.clear()
    _loader_mod._loaded.clear()
    yield
    slots.clear()
    _loader_mod._loaded.clear()
    for key in list(sys.modules.keys()):
        if key.startswith("test_phaseA_"):
            sys.modules.pop(key, None)


def _make_module(base: Path, name: str, depends_on: list[str] | None = None) -> Path:
    pkg = base / name
    pkg.mkdir(parents=True)
    deps_str = repr(depends_on or [])
    (pkg / "__init__.py").write_text(textwrap.dedent(f"""
        PLUGIN_MANIFEST = {{
            "name": "{name}",
            "version": "1.0.0",
            "depends_on": {deps_str},
            "slots": {{}},
        }}
    """))
    return pkg


# ── A1: field_schema service ──────────────────────────────────────────────────

def test_field_schema_importable():
    from celerp.services.field_schema import get_effective_field_schema
    import asyncio
    assert callable(get_effective_field_schema)


@pytest.mark.asyncio
async def test_field_schema_returns_base_schema():
    from celerp.services.field_schema import get_effective_field_schema

    mock_session = AsyncMock()
    company = MagicMock()
    company.settings = {"item_schema": [{"key": "sku", "label": "SKU"}]}
    mock_session.get.return_value = company

    result = await get_effective_field_schema(mock_session, uuid.uuid4())
    # sku is present (stored) and missing default keys are merged in
    assert any(f["key"] == "sku" for f in result)
    assert any(f["key"] == "cost_price" for f in result)


@pytest.mark.asyncio
async def test_field_schema_no_company_returns_default():
    from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA, get_effective_field_schema

    mock_session = AsyncMock()
    mock_session.get.return_value = None

    result = await get_effective_field_schema(mock_session, uuid.uuid4())
    assert result == DEFAULT_ITEM_SCHEMA


@pytest.mark.asyncio
async def test_field_schema_category_overlay():
    """category_schemas is a dict {category: [fields]}, not a list of dicts.
    Regression: AttributeError: 'str' object has no attribute 'get' when
    iterating dict keys as if they were dicts.
    """
    from celerp.services.field_schema import get_effective_field_schema

    mock_session = AsyncMock()
    company = MagicMock()
    company.settings = {
        "item_schema": [{"key": "sku"}, {"key": "name"}],
        # Correct storage format: dict keyed by category name
        "category_schemas": {
            "Gem": [{"key": "name"}, {"key": "weight_ct"}],
        },
    }
    mock_session.get.return_value = company

    result = await get_effective_field_schema(mock_session, uuid.uuid4(), category="Gem")
    keys = [f["key"] for f in result]
    assert "sku" in keys
    assert "weight_ct" in keys
    # name replaced by category version (no duplicate)
    assert keys.count("name") == 1


@pytest.mark.asyncio
async def test_field_schema_category_overlay_regression_list_of_strings():
    """If category_schemas were stored as a dict, iterating its keys (strings)
    and calling .get() on them raises AttributeError. Verify the fix holds."""
    from celerp.services.field_schema import get_effective_field_schema

    mock_session = AsyncMock()
    company = MagicMock()
    company.settings = {
        "category_schemas": {"Diamond": [{"key": "carat"}, {"key": "cut"}]},
    }
    mock_session.get.return_value = company

    # Must not raise AttributeError
    result = await get_effective_field_schema(mock_session, uuid.uuid4(), category="Diamond")
    keys = [f["key"] for f in result]
    assert "carat" in keys
    assert "cut" in keys


# ── A3: references helper ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_entity_returns_none_for_unknown():
    from celerp.modules.references import resolve_entity

    mock_session = AsyncMock()
    mock_session.get.return_value = None

    result = await resolve_entity("unknown-id", uuid.uuid4(), mock_session)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_entity_returns_state():
    from celerp.modules.references import resolve_entity

    mock_session = AsyncMock()
    mock_row = MagicMock()
    mock_row.state = {"name": "Ruby", "sku": "R001"}
    mock_session.get.return_value = mock_row

    result = await resolve_entity("some-id", uuid.uuid4(), mock_session)
    assert result == {"name": "Ruby", "sku": "R001"}


@pytest.mark.asyncio
async def test_resolve_entity_never_raises():
    from celerp.modules.references import resolve_entity

    mock_session = AsyncMock()
    mock_session.get.side_effect = RuntimeError("DB down")

    result = await resolve_entity("x", uuid.uuid4(), mock_session)
    assert result is None


# ── A2: loader dep enforcement ────────────────────────────────────────────────

def test_loader_rejects_module_with_unmet_hard_dep(tmp_path):
    """Module with depends_on=['missing-dep'] must be skipped."""
    _make_module(tmp_path, "test_phaseA_dependent", depends_on=["test_phaseA_missing"])
    loaded = load_all(tmp_path, {"test_phaseA_dependent"})
    # Module skipped because dep not enabled
    assert not any(m["name"] == "test_phaseA_dependent" for m in loaded)


def test_loader_loads_in_dep_order(tmp_path):
    """dep must load before dependent."""
    _make_module(tmp_path, "test_phaseA_dep")
    _make_module(tmp_path, "test_phaseA_main", depends_on=["test_phaseA_dep"])
    loaded = load_all(tmp_path, {"test_phaseA_dep", "test_phaseA_main"})
    names = [m["name"] for m in loaded]
    assert names.index("test_phaseA_dep") < names.index("test_phaseA_main")


def test_load_one_raises_for_unloaded_dep(tmp_path):
    """_load_one validates dep is in _loaded, raises ModuleLoadError if not."""
    pkg = tmp_path / "test_phaseA_nodep"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        PLUGIN_MANIFEST = {
            "name": "test_phaseA_nodep",
            "version": "1.0.0",
            "depends_on": ["celerp-missing"],
            "slots": {},
        }
    """))
    with pytest.raises(ModuleLoadError):
        _load_one(pkg, "test_phaseA_nodep")
