"""A conftest module is a plugin: pytest reads each hook from it by name, so a second
definition of a hook replaces the first and only the last one runs. Each hook is
defined once per conftest, whatever condition guards it."""

import ast
import subprocess
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _conftests() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*conftest.py"], cwd=_ROOT, capture_output=True,
                         text=True, check=True).stdout.split()
    return [_ROOT / p for p in out]


def _module_level_hooks(tree: ast.Module) -> list[str]:
    """Hook names bound in the module body, including under if/try/with (which still
    bind module attributes), but not inside functions or classes."""
    names: list[str] = []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("pytest_"):
                names.append(node.name)
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                pending.extend(getattr(node, field, []))
        elif isinstance(node, ast.ExceptHandler):
            pending.extend(node.body)
    return names


def test_every_conftest_defines_each_hook_once():
    conftests = _conftests()
    assert _ROOT / "conftest.py" in conftests
    repeated = {}
    for path in conftests:
        counts = Counter(_module_level_hooks(ast.parse(path.read_text())))
        if twice := sorted(name for name, n in counts.items() if n > 1):
            repeated[str(path.relative_to(_ROOT))] = twice
    assert repeated == {}
