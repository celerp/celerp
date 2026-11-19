"""Every keyed handler of the accounting module and the AI routes claims its key before it
commits or does anything another person can see (an email, a notice), so a second request
under the key waits on the claim or is refused before any effect, and a retry finds the key.

Run as a script for the census table: python tests/test_keyed_claim_first.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [*sorted((ROOT / "default_modules/celerp-accounting/celerp_accounting").glob("*.py")),
         ROOT / "default_modules/celerp-ai/celerp_ai/routes.py"]
# The calls that claim a key: the bank replay store and the AI action claims; any write
# that carries an idempotency key claims it through the key's unique index.
CLAIMS = {"_claimed", "_recorded", "_unit_recorded", "claim_tool_call", "claim_continuation"}


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _claims(call: ast.Call) -> bool:
    return _name(call) in CLAIMS or any(k.arg == "idempotency_key" for k in call.keywords)


def _effect(call: ast.Call) -> bool:
    n = _name(call).lower()
    return n == "commit" or "email" in n or n.startswith(("send", "notify"))


def _calls(fn) -> list[ast.Call]:
    return sorted((c for c in ast.walk(fn) if isinstance(c, ast.Call)), key=lambda c: (c.lineno, c.col_offset))


def _functions() -> dict[str, list[tuple[Path, ast.AST]]]:
    found: dict[str, list[tuple[Path, ast.AST]]] = {}
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(node.name, []).append((path, node))
    return found


def _effects(fn, funcs, depth: int = 1) -> list[tuple[int, str]]:
    """The effects ``fn`` performs, by line: its own, and those of a helper it calls that
    claims nothing itself (one level down)."""
    out = []
    for c in _calls(fn):
        if _effect(c):
            out.append((c.lineno, _name(c)))
        elif depth:
            for _, g in funcs.get(_name(c), []):
                if not any(_claims(x) for x in _calls(g)):
                    out += [(c.lineno, f"{_name(c)}->{e}") for _, e in _effects(g, funcs, depth - 1)]
    return out


def census() -> list[tuple[str, str, int, list]]:
    """Each keyed handler: where it is, its first claim and the effects before that claim."""
    funcs = _functions()
    rows = []
    for path, fn in [x for v in funcs.values() for x in v]:
        claims = [c.lineno for c in _calls(fn) if _claims(c)]
        if claims:
            early = [e for e in _effects(fn, funcs) if e[0] < min(claims)]
            rows.append((f"{path.relative_to(ROOT)}:{fn.lineno}", fn.name, min(claims), early))
    return sorted(rows)


def test_census_finds_the_keyed_handlers():
    names = {r[1] for r in census()}
    assert {"ask", "import_statement", "resolve", "answer_question", "send_statements",
            "create_manual_journal_entry", "_run_confirmed_action"} <= names


def test_no_keyed_handler_commits_or_sends_before_its_claim():
    assert [r for r in census() if r[3]] == []


if __name__ == "__main__":
    print("| handler | where | first claim | effects before it |\n|---|---|---|---|")
    for where, name, claim, early in census():
        print(f"| {name} | {where} | {claim} | {', '.join(f'{e}@{l}' for l, e in early) or 'none'} |")
