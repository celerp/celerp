# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Routers reach services only through their public names."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ("celerp/routers", "ui/routes")


def _is_service(dotted: str) -> bool:
    path = ROOT.joinpath(*dotted.split("."))
    return dotted.startswith("celerp.services.") and (
        path.with_suffix(".py").is_file() or (path / "__init__.py").is_file())


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _private_service_uses(source: Path) -> list[str]:
    tree = ast.parse(source.read_text())
    modules: dict[str, str] = {}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if _is_service(node.module) and _private(alias.name):
                    found.append(f"{node.lineno}: {node.module}.{alias.name}")
                if _is_service(f"{node.module}.{alias.name}"):
                    modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and _is_service(alias.name):
                    modules[alias.asname] = alias.name
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in modules and _private(node.attr)):
            found.append(f"{node.lineno}: {modules[node.value.id]}.{node.attr}")
    return found


def test_no_private_cross_module_calls():
    offenders = {
        str(path.relative_to(ROOT)): uses
        for layer in LAYERS for path in sorted((ROOT / layer).rglob("*.py"))
        if (uses := _private_service_uses(path))
    }
    assert offenders == {}


def test_the_check_sees_private_service_access(tmp_path):
    sample = tmp_path / "router.py"
    sample.write_text(
        "from celerp.services import company_handoff_sender as sender\n"
        "from celerp.services.company_backup_files import _sent_record\n"
        "sender._cloud('GET', '/handoffs')\n"
        "sender.company_copies(1)\n"
    )
    assert _private_service_uses(sample) == [
        "2: celerp.services.company_backup_files._sent_record",
        "3: celerp.services.company_handoff_sender._cloud",
    ]
