"""Every keyed handler in a package that keeps a claim or replay store claims its key before it
commits or does anything another person can see (an email, a notice), so a second request
under the key waits on the claim or is refused before any effect, and a retry finds the key.

Run as a script for the census table: python tests/test_keyed_claim_first.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The calls that claim a key, each by the module that owns it: the bank replay store, the AI
# action claims and the manufacturing movement replay; any write that carries an idempotency
# key claims it through the key's unique index.
CLAIM_OWNERS = {
    "default_modules/celerp-accounting/celerp_accounting/bank_statements.py":
        {"_claimed", "_recorded", "_unit_recorded", "_replayed"},
    "celerp/ai/conversations.py": {"claim_tool_call", "claim_continuation"},
    "default_modules/celerp-manufacturing/celerp_manufacturing/movements.py": {"_replayed"},
}
CLAIMS = {n for names in CLAIM_OWNERS.values() for n in names}
# The effects another person can see, each by the module that owns it: a commit, an email,
# a notice. A name renamed or moved out of its module fails test_the_claim_and_boundary_names_exist.
BOUNDARY = {
    "celerp/services/email.py": {"send_email", "send_durably", "transmit"},
    "celerp/ai/jobs.py": {"send_step", "notify"},
    "celerp/notifications/service.py": {"notify_once", "notify_standing", "notify_every_company"},
    "celerp/ai/batch.py": {"notify_finished"},
}
EFFECTS = {"commit", *(n for names in BOUNDARY.values() for n in names)}


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _claims(call: ast.Call) -> bool:
    return _name(call) in CLAIMS or any(k.arg == "idempotency_key" for k in call.keywords)


def _effect(call: ast.Call) -> bool:
    return _name(call) in EFFECTS


def _defined(module: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse((ROOT / module).read_text()))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _files() -> list[Path]:
    """Every module of each package that owns a claim store or calls a public claim."""
    sources = [p for p in [*ROOT.glob("celerp/**/*.py"), *ROOT.glob("default_modules/*/celerp_*/**/*.py")]
               if "tests" not in p.parts]
    public = {n for n in CLAIMS if not n.startswith("_")}
    packages = {(ROOT / m).parent for m in CLAIM_OWNERS} | {
        p.parent for p in sources
        if any(isinstance(n, ast.Call) and _name(n) in public for n in ast.walk(ast.parse(p.read_text())))}
    return sorted(p for p in sources if p.parent in packages)


FILES = _files()


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
    assert {"ask", "import_statement", "resolve", "answer_question", "_send_statement",
            "create_manual_journal_entry", "_run_confirmed_action",
            "issue", "complete", "receive", "repair_output", "return_materials"} <= names
    assert {"celerp/ai", "default_modules/celerp-accounting/celerp_accounting",
            "default_modules/celerp-ai/celerp_ai",
            "default_modules/celerp-manufacturing/celerp_manufacturing"} <= {
        str(p.parent.relative_to(ROOT)) for p in FILES}


def test_the_claim_and_boundary_names_exist():
    for module, names in [*CLAIM_OWNERS.items(), *BOUNDARY.items()]:
        assert names <= _defined(module), (module, names - _defined(module))


def test_no_keyed_handler_commits_or_sends_before_its_claim():
    assert [r for r in census() if r[3]] == []


if __name__ == "__main__":
    print("| handler | where | first claim | effects before it |\n|---|---|---|---|")
    for where, name, claim, early in census():
        print(f"| {name} | {where} | {claim} | {', '.join(f'{e}@{l}' for l, e in early) or 'none'} |")
