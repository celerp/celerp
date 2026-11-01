"""Every write of the outbound queue names its company. A queue row written by id
alone holds no company lock and names no company to the write observer, so the
write can land in a company being reset or deleted."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A where clause names the company when it holds the column itself or one of the
# condition tuples built with it (identity_rows; _release_rows' prefixed where).
_NAMES_COMPANY = ("OutboundQueue.company_id", "*identity_rows")


def unscoped_queue_writes(source: str) -> list[int]:
    """Lines of sa.update/sa.delete(OutboundQueue) statements whose where clause
    names no company."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("update", "delete") and len(node.args) == 1
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "OutboundQueue"):
            continue
        statement, function = node, None
        while isinstance(parents.get(statement), (ast.Attribute, ast.Call)) and not (
            isinstance(parents[statement], ast.Call) and statement in parents[statement].args
        ):
            statement = parents[statement]
        scope = node
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        function = scope if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        text = ast.unparse(statement)
        if any(name in text for name in _NAMES_COMPANY):
            continue
        if function is not None and function.name == "_release_rows" and "*where" in text:
            continue
        found.append(node.lineno)
    return found


def _sources():
    for base in ("celerp", "default_modules"):
        for path in sorted((ROOT / base).rglob("*.py")):
            if "/tests/" in str(path) or "/migrations/" in str(path):
                continue
            text = path.read_text()
            if "OutboundQueue" in text:
                yield path.relative_to(ROOT), text


def test_every_outbound_queue_write_names_its_company():
    unscoped = {str(path): lines for path, text in _sources() if (lines := unscoped_queue_writes(text))}
    assert unscoped == {}


def test_the_check_finds_a_write_by_id_alone():
    assert unscoped_queue_writes(
        "async def save(self, session):\n"
        "    await session.execute(sa.update(OutboundQueue).where(OutboundQueue.id == self.row_id).values(x=1))\n"
    ) == [2]
