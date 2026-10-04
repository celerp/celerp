# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The slots a module may fill are exactly the slots Celerp reads.

Every read of the slot registry in Celerp and its bundled modules is found from
the source, and each slot read has its contract (slots.SLOT_NAMES and the
loader's entry rules). A name outside the contract is not a slot: a module
filling one changes nothing, and the loader says the name is unknown. The
accounting module's chart is not a slot at all; only the bundled module can
provide it.
"""
from __future__ import annotations

import ast
import logging
import sys
import uuid
from pathlib import Path

import pytest

from celerp.modules import loader, slots

_ROOT = Path(__file__).resolve().parents[2]
# Readers that take slot names: the registry's own, and the page helpers that
# pass their first (visible_slot_contributions) or every (required_connectors)
# argument on to it.
_DIRECT = {"get", "get_slot", "fire_lifecycle", "fire_lifecycle_strict"}
_INDIRECT = {"visible_slot_contributions": False, "required_connectors": True}


def _sources() -> list[Path]:
    files = [*(_ROOT / "celerp").rglob("*.py"), *(_ROOT / "ui").rglob("*.py"),
             *(_ROOT / "default_modules").glob("*/*/**/*.py")]
    return sorted(f for f in files if "tests" not in f.relative_to(_ROOT).parts
                  and f != _ROOT / "celerp" / "modules" / "slots.py")


def _slot_reads() -> tuple[dict[str, list[str]], list[str]]:
    """Slot name -> where it is read, and every read whose name the source does not
    show (outside the indirect readers, which pass their caller's names on)."""
    reads: dict[str, list[str]] = {}
    unnamed: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text())
        constants = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                     and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                     for t in n.targets if isinstance(t, ast.Name)}
        readers, registries = {}, set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "celerp.modules.slots":
                readers |= {(a.asname or a.name): False for a in node.names if a.name in _DIRECT}
            elif isinstance(node, ast.ImportFrom) and node.module == "celerp.modules":
                registries |= {a.asname or a.name for a in node.names if a.name == "slots"}
                readers |= {(a.asname or a.name): False for a in node.names if a.name == "get_slot"}
            elif isinstance(node, ast.ImportFrom) and node.module == "ui.module_slots":
                readers |= {(a.asname or a.name): _INDIRECT[a.name]
                            for a in node.names if a.name in _INDIRECT}
            elif isinstance(node, ast.Import):
                registries |= {a.asname for a in node.names
                               if a.name == "celerp.modules.slots" and a.asname}
        helpers = {n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name in _INDIRECT and path.name == "module_slots.py"}
        inside = {id(c) for h in helpers for c in ast.walk(h)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in readers:
                every = readers[fn.id]
            elif (isinstance(fn, ast.Attribute) and fn.attr in _DIRECT
                    and isinstance(fn.value, ast.Name) and fn.value.id in registries):
                every = False
            else:
                continue
            where = f"{path.relative_to(_ROOT)}:{node.lineno}"
            for arg in node.args if every else node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    reads.setdefault(arg.value, []).append(where)
                elif isinstance(arg, ast.Name) and arg.id in constants:
                    reads.setdefault(constants[arg.id], []).append(where)
                elif id(node) not in inside:
                    unnamed.append(where)
    return reads, unnamed


def test_every_slot_celerp_reads_has_its_contract():
    reads, unnamed = _slot_reads()
    assert unnamed == []
    assert set(reads) <= slots.SLOT_NAMES, {s: reads[s] for s in set(reads) - slots.SLOT_NAMES}
    assert set(loader._SLOT_ENTRY_KEYS) == slots.SLOT_NAMES


def test_manufacturing_slots_are_public_with_their_keyword_contracts():
    assert {"inventory_in_production", "item_lineage_guard"} <= slots.SLOT_NAMES
    assert loader._HANDLER_KEYWORDS == {
        "inventory_in_production": ("session", "company_id"),
        "item_lineage_guard": ("session", "entry", "transition"),
    }
    assert set(loader._HANDLER_KEYWORDS) <= set(loader._SLOT_VALIDATORS)
    assert all(loader._CALLABLE_SLOTS[s] == ("handler", True) for s in loader._HANDLER_KEYWORDS)


def test_the_chart_of_accounts_is_not_a_slot():
    assert not {"journal_accounts", "chart_accounts", "add_chart_account"} & slots.SLOT_NAMES


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    before_path, before_mods = list(sys.path), set(sys.modules)
    slots.clear()
    yield base
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    sys.path[:] = before_path
    for key in set(sys.modules) - before_mods:
        sys.modules.pop(key, None)


def test_a_name_celerp_does_not_read_is_reported_unknown_and_not_registered(module_dir, caplog):
    name, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
    pkg = module_dir / name
    (pkg / inner).mkdir(parents=True)
    (pkg / inner / "__init__.py").write_text("")
    (pkg / inner / "books.py").write_text("async def lock(session, company_id, codes):\n    return {}\n")
    manifest = {"name": name, "version": "1.0.0", "slots": {
        "journal_accounts": {"handler": f"{inner}.books:lock"},
        "nav": [{"key": "acme", "label": "Acme", "href": "/acme"}]}}
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")

    admission = loader.admit_modules(str(module_dir), {name})
    with caplog.at_level(logging.WARNING, logger="celerp.modules.loader"):
        loader.load_all(str(module_dir), {name}, admission=admission)

    assert loader.is_running(name), loader.load_errors()
    assert slots.get("journal_accounts") == []
    assert [e["key"] for e in slots.get("nav")] == ["acme"]
    assert any("journal_accounts" in r.getMessage() and "unknown" in r.getMessage()
               for r in caplog.records)
