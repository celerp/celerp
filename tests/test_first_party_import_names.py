# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every name a first-party module imports from another first-party module exists there,
including the imports made inside functions, which run only when their path is taken."""
from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("celerp", "ui", "default_modules")


# The packages this tree ships: the app, its UI and every bundled module (an optional module
# installed separately, imported under ``except ImportError``, is not one of them).
PACKAGES = {"celerp", "ui"} | {p.parent.name for p in (ROOT / "default_modules").glob("*/*/__init__.py")}


def _first_party(module: str) -> bool:
    return module.split(".")[0] in PACKAGES


def _imports(source: Path):
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and _first_party(node.module):
            for alias in node.names:
                if alias.name != "*":
                    yield node.lineno, node.module, alias.name


def _missing(module: str, name: str) -> bool:
    found = importlib.import_module(module)
    if hasattr(found, name):
        return False
    return not hasattr(found, "__path__") or importlib.util.find_spec(f"{module}.{name}") is None


def test_every_first_party_import_names_something_that_exists():
    sources = [p for top in SOURCES for p in sorted((ROOT / top).rglob("*.py"))
               if "tests" not in p.relative_to(ROOT).parts]
    broken = [f"{p.relative_to(ROOT)}:{line}: {module}.{name}"
              for p in sources for line, module, name in _imports(p) if _missing(module, name)]
    assert broken == []


def test_the_check_sees_a_lazy_import_of_a_name_that_is_gone(tmp_path):
    sample = tmp_path / "service.py"
    sample.write_text("def pay():\n    from celerp.services.payments import NO_SUCH_KEY, WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY\n")
    assert [(m, n) for _, m, n in _imports(sample) if _missing(m, n)] == [("celerp.services.payments", "NO_SUCH_KEY")]
